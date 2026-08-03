#!/usr/bin/env python3
"""Project exact Mala packet identities into Bhiksha's observational registry."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bhiksha.evidence.bindings import build_registry_payload


def build(*, packet_root: Path, targets_path: Path) -> dict:
    targets = json.loads(targets_path.read_text(encoding="utf-8"))
    if targets.get("schema_version") != "bhiksha.evidence_binding_targets.v1":
        raise ValueError("unsupported evidence-binding target schema")
    bindings: list[dict] = []
    for target in targets.get("targets") or []:
        manifest_path = packet_root / target["packet_dir"] / "manifest.json"
        packet = json.loads(manifest_path.read_text(encoding="utf-8"))
        if packet.get("schema_version") != "mala.evidence_packet.v2":
            raise ValueError(f"{manifest_path} is not a v2 experiment packet")
        primary = next(
            (
                artifact
                for artifact in packet.get("artifacts") or []
                if artifact.get("role") == "provider_validation"
            ),
            None,
        )
        if primary is None:
            raise ValueError(f"{manifest_path} has no provider_validation artifact")
        cohort = packet["cohort_contract"]
        bindings.append(
            {
                "strategy_id": target["strategy_id"],
                "symbol": target["symbol"],
                "direction": target["direction"],
                "allowed_authorization_modes": target["allowed_authorization_modes"],
                "run_id": packet["run_id"],
                "evidence_packet_id": packet["evidence_packet_id"],
                "artifact_sha256": primary["sha256"],
                "artifact_uri": primary["artifact_uri"],
                "experiment_id": packet["experiment_id"],
                "cohort_id": cohort["cohort_id"],
                "cohort_contract_sha256": cohort["contract_sha256"],
                "declared_option_selection_contract": packet[
                    "declared_option_selection_contract"
                ],
            }
        )
    return build_registry_payload(bindings)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mala-packet-root", type=Path, required=True)
    parser.add_argument(
        "--targets", type=Path, default=Path("config/evidence_binding_targets_v1.json")
    )
    parser.add_argument(
        "--out", type=Path, default=Path("config/evidence_bindings_v1.json")
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    payload = build(packet_root=args.mala_packet_root, targets_path=args.targets)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.out)


if __name__ == "__main__":
    main()
