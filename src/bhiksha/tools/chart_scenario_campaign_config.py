"""Freeze the exact Bhiksha coordinator config from canonical campaign artifacts."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mala_bhiksha_kernel import canonical_sha256

from bhiksha.chart_scenarios.paths import require_experiment_path
from bhiksha.ops.chart_scenario_sheet import SPREADSHEET_ID
from bhiksha.tools.chart_scenario_coordinator import (
    _RUNTIME_RECORD_SCHEMA,
    _capture_installed_environment_identity,
    _file_sha256,
    _read_content_addressed,
    _source_tree_sha256,
    _validate_campaign_config,
    _write_atomic,
)

_ROLE_LAYOUT = {
    "birdclaw": ("src/cli.mjs", "src"),
    "market_cartographer": (
        "src/market_cartographer/cli.py",
        "src/market_cartographer",
    ),
    "tradelab": ("scripts/market_context/__main__.py", "scripts/market_context"),
    "agent_broker": ("agent_broker/cli.py", "agent_broker"),
}


def _git_identity(checkout: Path) -> str:
    status = subprocess.run(
        ["git", "-C", str(checkout), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    if status.stdout.strip():
        raise ValueError(f"campaign tool checkout is dirty: {checkout}")
    return subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _file_identity(path: Path) -> dict[str, Any]:
    requested = path.expanduser()
    if not requested.is_absolute() or not requested.is_file():
        raise ValueError(f"campaign runtime file is invalid: {requested}")
    resolved = requested.resolve(strict=True)
    return {
        "path": str(requested),
        "sha256": _file_sha256(requested),
        "realpath": str(resolved),
        "realpath_sha256": _file_sha256(resolved),
        "symlink_target": os.readlink(requested) if requested.is_symlink() else None,
    }


def _dependency_identity(checkout: Path, import_root: Path) -> dict[str, Any]:
    for name in ("uv.lock", "package-lock.json", "requirements.txt"):
        lock = checkout / name
        if lock.is_file() and not lock.is_symlink():
            return {"mode": "lockfile", "path": str(lock), "sha256": _file_sha256(lock)}
    return {
        "mode": "source_tree_only",
        "path": None,
        "sha256": _source_tree_sha256(import_root),
    }


def capture_runtime_record(
    *,
    role: str,
    checkout: str | Path,
    launcher: str | Path,
    interpreter: str | Path,
    record_path: str | Path,
    captured_at: str,
) -> dict[str, Any]:
    """Capture one clean, path-stable tool runtime in the validator's exact shape."""

    if role not in _ROLE_LAYOUT:
        raise ValueError(f"unsupported campaign tool role: {role}")
    root = Path(checkout).expanduser()
    if root.is_symlink() or not root.is_absolute() or not root.is_dir():
        raise ValueError(f"campaign checkout is invalid: {root}")
    root = root.resolve()
    entry_rel, import_rel = _ROLE_LAYOUT[role]
    entrypoint = (root / entry_rel).resolve(strict=True)
    import_root = (root / import_rel).resolve(strict=True)
    if not entrypoint.is_relative_to(root) or not import_root.is_relative_to(root):
        raise ValueError(f"campaign runtime escaped checkout: {role}")
    launcher_id = _file_identity(Path(launcher))
    interpreter_id = _file_identity(Path(interpreter))
    version = subprocess.run(
        [interpreter_id["path"], "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    timestamp = datetime.fromisoformat(captured_at)
    if timestamp.tzinfo is None:
        raise ValueError("captured_at must be timezone-aware")
    output = Path(record_path).expanduser()
    if output.is_symlink() or not output.is_absolute():
        raise ValueError("runtime record path must be an absolute non-symlink path")
    output = require_experiment_path(output, role=f"{role} runtime record")
    body: dict[str, Any] = {
        "schema": _RUNTIME_RECORD_SCHEMA,
        "role": role,
        "checkout": str(root),
        "commit": _git_identity(root),
        "clean": True,
        "launcher": launcher_id["path"],
        "launcher_sha256": launcher_id["sha256"],
        "launcher_realpath": launcher_id["realpath"],
        "launcher_realpath_sha256": launcher_id["realpath_sha256"],
        "launcher_symlink_target": launcher_id["symlink_target"],
        "interpreter": interpreter_id["path"],
        "interpreter_realpath": interpreter_id["realpath"],
        "interpreter_sha256": interpreter_id["sha256"],
        "interpreter_symlink_target": interpreter_id["symlink_target"],
        "runtime_version": (version.stdout or version.stderr).strip(),
        "entrypoint": str(entrypoint),
        "entrypoint_sha256": _file_sha256(entrypoint),
        "import_root": str(import_root),
        "import_root_sha256": _source_tree_sha256(import_root),
        "import_map": {
            {
                "birdclaw": "birdclaw.cli",
                "market_cartographer": "market_cartographer.cli",
                "tradelab": "scripts.market_context.__main__",
                "agent_broker": "agent_broker.cli",
            }[role]: {"path": str(entrypoint), "sha256": _file_sha256(entrypoint)}
        },
        "dependency_identity": _dependency_identity(root, import_root),
        "installed_environment_identity": _capture_installed_environment_identity(
            Path(interpreter_id["path"]), role=role, checkout=root
        ),
        "argv_prefix": {
            "birdclaw": [launcher_id["path"], str(entrypoint)],
            "market_cartographer": [launcher_id["path"], "-m", "market_cartographer.cli"],
            "tradelab": [launcher_id["path"], "-m", "scripts.market_context"],
            "agent_broker": [launcher_id["path"]],
        }[role],
        "captured_at": timestamp.astimezone(UTC).isoformat(),
        "record_path": str(output),
    }
    record = {**body, "content_hash": canonical_sha256(body)}
    _write_atomic(output, record)
    return record


def build_campaign_config(
    *,
    experiment_root: str | Path,
    campaign_id: str,
    birdclaw_checkout: str | Path,
    birdclaw_db: str | Path,
    market_cartographer_checkout: str | Path,
    tradelab_checkout: str | Path,
    agent_broker_checkout: str | Path,
    agent_broker: str | Path,
    kernel_src: str | Path,
    cartographer_provider: str,
    cartographer_data_root: str | Path | None,
    symbols: list[str],
    toolchain: dict[str, dict[str, Any]],
    output: str | Path,
) -> dict[str, Any]:
    """Bind canonical TradeLab freeze artifacts to the sole Bhiksha clock."""

    root = Path(experiment_root).expanduser().resolve()
    campaign_root = root / "campaigns" / campaign_id
    campaign = _read_content_addressed(
        campaign_root / "campaign.json", schema="tradelab.market_context_campaign.v2"
    )
    protocol = _read_content_addressed(
        campaign_root / "campaign-protocol.json",
        schema="tradelab.market_context_campaign_protocol.v1",
    )
    freeze = _read_content_addressed(
        campaign_root / "campaign-freeze-receipt.json",
        schema="tradelab.market_context_campaign_freeze_receipt.v1",
    )
    calendar = protocol["session_calendar"]
    normalized_symbols = sorted(set(symbols))
    body: dict[str, Any] = {
        "schema": "bhiksha.chart-scenario-campaign-config.v1",
        "campaign_id": campaign_id,
        "birdclaw_checkout": str(Path(birdclaw_checkout).expanduser().resolve()),
        "birdclaw_db": str(Path(birdclaw_db).expanduser().resolve()),
        "market_cartographer_checkout": str(
            Path(market_cartographer_checkout).expanduser().resolve()
        ),
        "tradelab_checkout": str(Path(tradelab_checkout).expanduser().resolve()),
        "tradelab_experiment_root": str(root),
        "agent_broker": str(Path(agent_broker).expanduser()),
        "agent_broker_checkout": str(Path(agent_broker_checkout).expanduser().resolve()),
        "spreadsheet_id": SPREADSHEET_ID,
        "kernel_src": str(Path(kernel_src).expanduser().resolve()),
        "cartographer_provider": cartographer_provider,
        "cartographer_data_root": (
            str(Path(cartographer_data_root).expanduser().resolve())
            if cartographer_data_root
            else None
        ),
        "symbols": normalized_symbols,
        "campaign_manifest_hash": campaign["content_hash"],
        "campaign_protocol_hash": protocol["content_hash"],
        "campaign_freeze_receipt_hash": freeze["content_hash"],
        "treatment_manifest_hash": protocol["treatment_manifest_hash"],
        "universe_hash": protocol["universe_hash"],
        "session_calendar_hash": protocol["session_calendar_hash"],
        "session_calendar_id": calendar["calendar_id"],
        "session_calendar_version": calendar["calendar_version"],
        "toolchain": toolchain,
        "starts_on": protocol["starts_on"],
        "checkpoint_after_sessions": protocol["checkpoint_after_sessions"],
        "max_sessions": protocol["max_sessions"],
        "ends_on": protocol["ends_on"],
    }
    payload = {**body, "content_hash": canonical_sha256(body)}
    validated = _validate_campaign_config(payload)
    persisted = {key: value for key, value in validated.items() if not key.startswith("_")}
    target = require_experiment_path(output, role="campaign config")
    _write_atomic(target, persisted)
    return persisted


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--experiment-root", required=True)
    value.add_argument("--campaign-id", required=True)
    value.add_argument("--birdclaw-checkout", required=True)
    value.add_argument("--birdclaw-db", required=True)
    value.add_argument("--birdclaw-node", required=True)
    value.add_argument("--cartographer-checkout", required=True)
    value.add_argument("--cartographer-python", required=True)
    value.add_argument("--tradelab-checkout", required=True)
    value.add_argument("--tradelab-python", required=True)
    value.add_argument("--agent-broker-checkout", required=True)
    value.add_argument("--agent-broker", required=True)
    value.add_argument("--kernel-src", required=True)
    value.add_argument("--cartographer-provider", choices=("mala", "fixture"), required=True)
    value.add_argument("--cartographer-data-root")
    value.add_argument("--symbols", required=True)
    value.add_argument("--runtime-dir", required=True)
    value.add_argument("--output", required=True)
    value.add_argument("--captured-at", default=datetime.now(UTC).isoformat())
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    runtimes = {
        "birdclaw": (args.birdclaw_checkout, args.birdclaw_node, args.birdclaw_node),
        "market_cartographer": (
            args.cartographer_checkout,
            args.cartographer_python,
            args.cartographer_python,
        ),
        "tradelab": (args.tradelab_checkout, args.tradelab_python, args.tradelab_python),
        "agent_broker": (
            args.agent_broker_checkout,
            args.agent_broker,
            Path(args.agent_broker).resolve().parent / "python",
        ),
    }
    toolchain = {
        role: capture_runtime_record(
            role=role,
            checkout=checkout,
            launcher=launcher,
            interpreter=interpreter,
            record_path=runtime_dir / f"{role}.json",
            captured_at=args.captured_at,
        )
        for role, (checkout, launcher, interpreter) in runtimes.items()
    }
    payload = build_campaign_config(
        experiment_root=args.experiment_root,
        campaign_id=args.campaign_id,
        birdclaw_checkout=args.birdclaw_checkout,
        birdclaw_db=args.birdclaw_db,
        market_cartographer_checkout=args.cartographer_checkout,
        tradelab_checkout=args.tradelab_checkout,
        agent_broker_checkout=args.agent_broker_checkout,
        agent_broker=args.agent_broker,
        kernel_src=args.kernel_src,
        cartographer_provider=args.cartographer_provider,
        cartographer_data_root=args.cartographer_data_root,
        symbols=[item.strip().upper() for item in args.symbols.split(",") if item.strip()],
        toolchain=toolchain,
        output=args.output,
    )
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
