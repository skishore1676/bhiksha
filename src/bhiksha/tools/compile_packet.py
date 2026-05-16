"""Validate and compile a shared-kernel packet for Bhiksha runtime use."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bhiksha.packets.runtime_compile import compile_packet_for_runtime


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    result = compile_packet_for_runtime(args.packet)
    payload = {
        "packet_id": result.packet_id,
        "version": result.version,
        "kind": result.kind,
        "status": result.status,
        "decision": result.decision,
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
