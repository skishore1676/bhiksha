"""Report legacy Mala-promoted strategy wires that must re-earn promotion."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml


LEGACY_ORIGINS = {"mala", "mala_bias_translator_v1"}
LEGACY_TAGS = {"mala_promoted"}


@dataclass(frozen=True, slots=True)
class LegacyWire:
    path: str
    surface: str
    identifier: str
    enabled: bool
    approved: bool
    strategy_key: str
    symbol: str
    reasons: list[str]

    @property
    def active(self) -> bool:
        return self.enabled and self.approved


def scan_legacy_wires(
    *,
    deployments_dir: Path,
    strategy_catalog_dir: Path,
) -> list[LegacyWire]:
    wires: list[LegacyWire] = []
    wires.extend(_scan_yaml_tree(deployments_dir, surface="deployment"))
    wires.extend(_scan_yaml_tree(strategy_catalog_dir, surface="strategy_catalog"))
    return [wire for wire in wires if wire.reasons]


def _scan_yaml_tree(root: Path, *, surface: str) -> list[LegacyWire]:
    if not root.exists():
        return []
    wires: list[LegacyWire] = []
    for path in sorted(root.rglob("*.yaml")):
        payload = _read_yaml(path)
        reasons = _legacy_reasons(payload)
        if not reasons:
            continue
        wires.append(
            LegacyWire(
                path=str(path),
                surface=surface,
                identifier=str(payload.get("deployment_id") or payload.get("strategy_id") or path.stem),
                enabled=bool(payload.get("enabled", True)),
                approved=str(payload.get("approval_status", "approved")) == "approved",
                strategy_key=str(payload.get("strategy", {}).get("key", "")),
                symbol=str(payload.get("symbol", "")),
                reasons=reasons,
            )
        )
    return wires


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _legacy_reasons(payload: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    tags = {str(tag) for tag in payload.get("tags", []) if str(tag)}
    legacy_tags = sorted(tags & LEGACY_TAGS)
    if legacy_tags:
        reasons.append("legacy_tag:" + ",".join(legacy_tags))
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    origin = str(source.get("origin", ""))
    if origin in LEGACY_ORIGINS:
        reasons.append(f"legacy_source_origin:{origin}")
    artifact = str(source.get("artifact", "")).lower()
    if "m5" in artifact or "execution_mapping" in artifact:
        reasons.append("legacy_m5_artifact")
    metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    if metadata.get("promoted_from"):
        reasons.append("legacy_promoted_from")
    return reasons


def build_report(wires: list[LegacyWire]) -> dict[str, Any]:
    active = [wire for wire in wires if wire.active]
    return {
        "status": "blocked" if active else "clear",
        "legacy_wire_count": len(wires),
        "active_legacy_wire_count": len(active),
        "active_legacy_ids": [wire.identifier for wire in active],
        "wires": [asdict(wire) | {"active": wire.active} for wire in wires],
        "next_action": (
            "retire_or_repromote_active_legacy_wires"
            if active
            else "continue_new_packet_path"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployments-dir", type=Path, default=Path("config/deployments"))
    parser.add_argument("--strategy-catalog-dir", type=Path, default=Path("config/strategy_catalog"))
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    report = build_report(
        scan_legacy_wires(
            deployments_dir=args.deployments_dir,
            strategy_catalog_dir=args.strategy_catalog_dir,
        )
    )
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 2 if report["active_legacy_wire_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
