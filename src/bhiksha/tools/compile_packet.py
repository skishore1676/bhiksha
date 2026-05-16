"""Validate and compile a shared-kernel packet for Bhiksha runtime use."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bhiksha.packets.runtime_compile import (
    compile_packet_for_runtime,
    load_legacy_retirement_report,
)
from bhiksha.shared_kernel import ensure_kernel_on_path

ensure_kernel_on_path()
from mala_bhiksha_kernel import CapabilityManifest  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--capability-manifest", type=Path)
    parser.add_argument("--legacy-retirement-report", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    capability_manifest = None
    if args.capability_manifest:
        capability_manifest = CapabilityManifest.model_validate_json(
            args.capability_manifest.read_text(encoding="utf-8")
        )
    legacy_retirement_report = None
    if args.legacy_retirement_report:
        legacy_retirement_report = load_legacy_retirement_report(args.legacy_retirement_report)
    result = compile_packet_for_runtime(
        args.packet,
        capability_manifest=capability_manifest,
        legacy_retirement_report=legacy_retirement_report,
    )
    payload = {
        "packet_id": result.packet_id,
        "version": result.version,
        "kind": result.kind,
        "status": result.status,
        "decision": result.decision,
        "eligibility": result.eligibility,
        "executable": result.executable,
        "block_reasons": result.block_reasons,
        "feature_contract_id": result.feature_contract_id,
        "runtime_mode": result.runtime_mode,
        "management_policy_ids": result.management_policy_ids or [],
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0 if result.executable else 2


if __name__ == "__main__":
    raise SystemExit(main())
