"""Capture and revalidate the exact kernel used by chart-scenario scheduling.

This module intentionally does not import :mod:`mala_bhiksha_kernel`.  The
launchd wrapper uses it to authenticate the kernel tree before starting any
Python process that can import the kernel package.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

RUNTIME_SCHEMA = "bhiksha.chart-scenario-kernel-runtime.v1"
RUNTIME_RECORD_ENV = "BHIKSHA_CHART_KERNEL_RUNTIME_RECORD"
RUNTIME_HASH_ENV = "BHIKSHA_CHART_KERNEL_RUNTIME_HASH"
KERNEL_SRC_ENV = "BHIKSHA_KERNEL_SRC"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_RECORD_FIELDS = {
    "schema",
    "repo_root",
    "commit",
    "clean",
    "src",
    "src_sha256",
    "import_map",
    "captured_at",
    "content_hash",
}


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_tree_sha256(root: Path) -> str:
    entries: list[dict[str, str]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise ValueError(f"kernel source tree contains symlink: {relative}")
        if not path.is_file():
            continue
        if (
            "__pycache__" in relative.parts
            or path.suffix in {".pyc", ".pyo"}
            or path.name == ".DS_Store"
        ):
            continue
        entries.append({"path": relative.as_posix(), "sha256": file_sha256(path)})
    if not entries:
        raise ValueError("kernel source tree is empty")
    return canonical_sha256(entries)


def capture_kernel_runtime(kernel_src: str | Path) -> dict[str, Any]:
    src = _verified_real_directory(Path(kernel_src), label="kernel src")
    package = _verified_real_directory(
        src / "mala_bhiksha_kernel", label="kernel import package"
    )
    module = _verified_real_file(package / "__init__.py", label="kernel module")
    repo = _git_output(src, "rev-parse", "--show-toplevel")
    repo_root = _verified_real_directory(Path(repo), label="kernel Git checkout")
    if not src.is_relative_to(repo_root):
        raise ValueError("kernel src escaped its Git checkout")
    status = _git_output(repo_root, "status", "--porcelain", "--untracked-files=all")
    if status:
        raise ValueError("kernel Git checkout is not clean")
    commit = _git_output(repo_root, "rev-parse", "HEAD")
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("kernel Git commit is invalid")
    body = {
        "schema": RUNTIME_SCHEMA,
        "repo_root": str(repo_root),
        "commit": commit,
        "clean": True,
        "src": str(src),
        "src_sha256": source_tree_sha256(src),
        "import_map": {
            "mala_bhiksha_kernel": {
                "path": str(module),
                "sha256": file_sha256(module),
            }
        },
        "captured_at": datetime.now(UTC).isoformat(),
    }
    return {**body, "content_hash": canonical_sha256(body)}


def write_runtime_record(path: str | Path, record: Mapping[str, Any]) -> Path:
    target = Path(path).expanduser()
    if not target.is_absolute() or target.is_symlink():
        raise ValueError("kernel runtime record path must be an absolute real path")
    for parent in target.parents:
        if parent.exists() and parent.is_symlink():
            raise ValueError("kernel runtime record parent cannot be a symlink")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(record), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return target


def verify_kernel_runtime(
    *,
    record_path: str | Path,
    expected_hash: str,
    expected_src: str | Path,
    imported_module: ModuleType | None = None,
) -> dict[str, Any]:
    requested_record = Path(record_path).expanduser()
    record_file = _verified_real_file(requested_record, label="kernel runtime record")
    value = json.loads(record_file.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != _RECORD_FIELDS:
        raise ValueError("kernel runtime record fields are invalid")
    body = {key: item for key, item in value.items() if key != "content_hash"}
    computed = canonical_sha256(body)
    if (
        value.get("schema") != RUNTIME_SCHEMA
        or value.get("clean") is not True
        or value.get("content_hash") != computed
        or expected_hash != computed
        or _SHA256_RE.fullmatch(expected_hash) is None
    ):
        raise ValueError("kernel runtime record identity is invalid")
    src = _verified_real_directory(Path(str(value["src"])), label="kernel src")
    configured_src = _verified_real_directory(
        Path(expected_src), label="configured kernel src"
    )
    if src != configured_src:
        raise ValueError("configured kernel src differs from frozen runtime")
    repo_root = _verified_real_directory(
        Path(str(value["repo_root"])), label="kernel Git checkout"
    )
    if not src.is_relative_to(repo_root):
        raise ValueError("kernel src escaped its frozen Git checkout")
    if _git_output(repo_root, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("kernel Git checkout is not clean")
    if _git_output(repo_root, "rev-parse", "HEAD") != value["commit"]:
        raise ValueError("kernel Git commit drift")
    if source_tree_sha256(src) != value["src_sha256"]:
        raise ValueError("kernel source tree drift")
    import_map = value.get("import_map")
    if not isinstance(import_map, Mapping) or set(import_map) != {
        "mala_bhiksha_kernel"
    }:
        raise ValueError("kernel import map is invalid")
    entry = import_map["mala_bhiksha_kernel"]
    if not isinstance(entry, Mapping) or set(entry) != {"path", "sha256"}:
        raise ValueError("kernel module identity is invalid")
    module_path = _verified_real_file(Path(str(entry["path"])), label="kernel module")
    if (
        not module_path.is_relative_to(src)
        or file_sha256(module_path) != entry["sha256"]
    ):
        raise ValueError("kernel module digest drift")
    origin = _module_origin(imported_module)
    if origin != module_path:
        raise ValueError("loaded kernel module differs from frozen import map")
    return value


def verify_kernel_runtime_from_env(
    *, imported_module: ModuleType | None = None
) -> dict[str, Any]:
    record_path = os.getenv(RUNTIME_RECORD_ENV, "")
    expected_hash = os.getenv(RUNTIME_HASH_ENV, "")
    expected_src = os.getenv(KERNEL_SRC_ENV, "")
    if not record_path or not expected_hash or not expected_src:
        raise ValueError("complete frozen kernel runtime environment is required")
    return verify_kernel_runtime(
        record_path=record_path,
        expected_hash=expected_hash,
        expected_src=expected_src,
        imported_module=imported_module,
    )


def _module_origin(imported_module: ModuleType | None) -> Path:
    if imported_module is not None:
        value = getattr(imported_module, "__file__", None)
    else:
        spec = importlib.util.find_spec("mala_bhiksha_kernel")
        value = spec.origin if spec is not None else None
    if not value:
        raise ValueError("kernel module origin is unavailable")
    return _verified_real_file(Path(str(value)), label="loaded kernel module")


def _verified_real_directory(path: Path, *, label: str) -> Path:
    requested = path.expanduser()
    if not requested.is_absolute() or requested.is_symlink() or not requested.is_dir():
        raise ValueError(f"{label} must be an absolute non-symlink directory")
    resolved = requested.resolve(strict=True)
    if resolved != requested:
        raise ValueError(f"{label} path contains a symlink")
    return resolved


def _verified_real_file(path: Path, *, label: str) -> Path:
    requested = path.expanduser()
    if not requested.is_absolute() or requested.is_symlink() or not requested.is_file():
        raise ValueError(f"{label} must be an absolute non-symlink file")
    resolved = requested.resolve(strict=True)
    if resolved != requested:
        raise ValueError(f"{label} path contains a symlink")
    return resolved


def _git_output(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["/usr/bin/git", "-C", str(repo), *args],
        check=False,
        text=True,
        capture_output=True,
        env={"PATH": "/usr/bin:/bin", "LANG": "C"},
    )
    if completed.returncode != 0:
        raise ValueError("kernel Git runtime probe failed")
    return completed.stdout.strip()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["verify"])
    args = parser.parse_args(argv)
    if args.action == "verify":
        verify_kernel_runtime_from_env()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
