from __future__ import annotations

import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from bhiksha.tools.chart_kernel_runtime import (
    capture_kernel_runtime,
    verify_kernel_runtime,
    write_runtime_record,
)


def _fixture_kernel(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "kernel"
    package = repo / "src" / "mala_bhiksha_kernel"
    package.mkdir(parents=True)
    module = package / "__init__.py"
    module.write_text("REVIEWED = True\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "reviewed kernel",
        ],
        check=True,
    )
    return repo / "src", module


def _module_at(path: Path) -> ModuleType:
    module = ModuleType("mala_bhiksha_kernel")
    module.__file__ = str(path)
    return module


def test_kernel_runtime_rejects_modified_copy_after_freeze(tmp_path: Path) -> None:
    kernel_src, module = _fixture_kernel(tmp_path)
    runtime = capture_kernel_runtime(kernel_src)
    record = write_runtime_record(tmp_path / "artifacts/kernel-runtime.json", runtime)
    verify_kernel_runtime(
        record_path=record,
        expected_hash=runtime["content_hash"],
        expected_src=kernel_src,
        imported_module=_module_at(module),
    )

    module.write_text("REVIEWED = False\nUNREVIEWED_DRIFT = True\n", encoding="utf-8")

    with pytest.raises(ValueError, match="not clean|source tree drift|module digest"):
        verify_kernel_runtime(
            record_path=record,
            expected_hash=runtime["content_hash"],
            expected_src=kernel_src,
            imported_module=_module_at(module),
        )


def test_kernel_runtime_rejects_symlinked_source_and_import_path(
    tmp_path: Path,
) -> None:
    kernel_src, module = _fixture_kernel(tmp_path)
    source_link = tmp_path / "kernel-src-link"
    source_link.symlink_to(kernel_src, target_is_directory=True)
    with pytest.raises(ValueError, match="non-symlink"):
        capture_kernel_runtime(source_link)

    runtime = capture_kernel_runtime(kernel_src)
    record = write_runtime_record(tmp_path / "artifacts/kernel-runtime.json", runtime)
    module_link = tmp_path / "kernel-module-link.py"
    module_link.symlink_to(module)
    with pytest.raises(ValueError, match="non-symlink"):
        verify_kernel_runtime(
            record_path=record,
            expected_hash=runtime["content_hash"],
            expected_src=kernel_src,
            imported_module=_module_at(module_link),
        )


def test_kernel_runtime_rejects_forged_record_hash(tmp_path: Path) -> None:
    kernel_src, module = _fixture_kernel(tmp_path)
    runtime = capture_kernel_runtime(kernel_src)
    record = write_runtime_record(tmp_path / "artifacts/kernel-runtime.json", runtime)

    with pytest.raises(ValueError, match="identity"):
        verify_kernel_runtime(
            record_path=record,
            expected_hash="f" * 64,
            expected_src=kernel_src,
            imported_module=_module_at(module),
        )
