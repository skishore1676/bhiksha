"""Export Bhiksha-owned strategy capabilities without touching runtime state."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Sequence

from bhiksha.strategy.capabilities import (
    default_capability_manifest_path,
    load_capability_manifest,
)


REPO = Path(__file__).resolve().parents[3]


def _head() -> str | None:
    process = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    return process.stdout.strip() if process.returncode == 0 else None


def _effects() -> dict[str, bool]:
    return {
        "broker_accessed": False,
        "account_accessed": False,
        "auth_accessed": False,
        "sheet_read": False,
        "sheet_written": False,
        "runtime_state_read": False,
        "runtime_state_mutated": False,
        "order_api_accessed": False,
    }


def build_manifest(
    path: str | Path | None = None,
    *,
    runtime_readback_commit: str | None = None,
) -> dict:
    native_path = Path(path) if path is not None else default_capability_manifest_path()
    native = load_capability_manifest(native_path)
    source_commit = _head()
    deployed = bool(
        runtime_readback_commit
        and source_commit
        and runtime_readback_commit == source_commit
    )
    capabilities = []
    for strategy_key, strategy in sorted(native["strategies"].items()):
        for variant, definition in sorted((strategy.get("variants") or {}).items()):
            supported = str(definition.get("status") or "").lower() == "supported"
            capabilities.append(
                {
                    "capability_id": f"bhiksha.strategy.{strategy_key}.{variant}",
                    "operation_class": "execution",
                    "supported_instruments": ["single-leg option execution"],
                    "supported_structures": [strategy_key, variant],
                    "parameter_constraints": {
                        "required_params": list(definition.get("required_params") or []),
                        "optional_params": list(definition.get("optional_params") or []),
                        "supported_thesis_exit_policies": list(
                            native.get("supported_thesis_exit_policies") or []
                        ),
                    },
                    "required_inputs": ["operator-authorized strategy row"],
                    "available_outputs": [
                        "entry receipt",
                        "exit receipt",
                        "forward observation",
                    ],
                    "declared_in_code": supported,
                    "verified": supported,
                    "deployed": supported and deployed,
                    "operationally_available": False,
                    "limitations": (
                        []
                        if supported
                        else [str(definition.get("reason") or "unsupported by native manifest")]
                    )
                    + [
                        "Manifest export does not inspect active plans, accounts, quotes, or broker state."
                    ],
                }
            )
    payload = {
        "schema": "bhiksha.strategy_capabilities.tradelab.v1",
        "manifest_id": f"bhiksha.strategy-capabilities.v{native['version']}",
        "version": str(native["version"]),
        "source_commit": source_commit,
        "producer_receipt": str(native_path),
        "capabilities": capabilities,
        "limitations": [
            "Code support is not operator authorization, deployment health, or live readiness.",
            "Runtime operational availability remains false without a separate app-owned health receipt.",
        ],
        "source_references": [
            str(native_path),
            "src/bhiksha/strategy/capabilities.py",
        ],
        "protected_effects_performed": _effects(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["manifest_hash"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest")
    parser.add_argument("--runtime-readback-commit")
    args = parser.parse_args(argv)
    print(
        json.dumps(
            build_manifest(
                args.manifest,
                runtime_readback_commit=args.runtime_readback_commit,
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
