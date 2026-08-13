"""Command-line entrypoint for the Cartographer price-only shadow observer."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from bhiksha.experiments.cartographer_shadow import (
    build_market_facts_from_mala,
    build_observation,
    write_json,
    zero_effects,
)


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def observe_one(
    batch_path: Path,
    *,
    output_root: Path,
    market_facts_path: Path | None = None,
    mala_data_root: Path | None = None,
    observed_at: str | None = None,
) -> dict[str, object]:
    batch = _load(batch_path)
    if market_facts_path is not None:
        facts = _load(market_facts_path)
    elif mala_data_root is not None:
        facts = build_market_facts_from_mala(batch, mala_data_root)
    else:
        raise ValueError("market facts or Mala data root is required")
    receipt = build_observation(
        batch,
        facts,
        observed_at=observed_at or datetime.now(timezone.utc).isoformat(),
    )
    batch_dir = output_root.expanduser().resolve() / str(batch["batch_hash"]).split(":", 1)[-1]
    path = write_json(receipt, batch_dir / f"{receipt['observation_hash'].split(':', 1)[-1]}.json")
    write_json(receipt, batch_dir / "latest.json")
    return {
        "schema": "bhiksha.cartographer_shadow_run_receipt.v1",
        "status": receipt["status"],
        "batch_hash": receipt["batch_hash"],
        "observation_hash": receipt["observation_hash"],
        "observation_path": str(path),
        "closed": sum(row["status"] == "closed" for row in receipt["observations"]),
        "pending": sum(
            row["status"] == "pending_market_data" for row in receipt["observations"]
        ),
        "effects": zero_effects(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m bhiksha.tools.cartographer_shadow")
    subparsers = parser.add_subparsers(dest="command", required=True)
    one = subparsers.add_parser("observe")
    one.add_argument("--batch", type=Path, required=True)
    one.add_argument("--market-facts", type=Path)
    one.add_argument("--mala-data-root", type=Path)
    one.add_argument("--output-root", type=Path, required=True)
    one.add_argument("--observed-at")
    root = subparsers.add_parser("observe-root")
    root.add_argument("--recommendation-root", type=Path, required=True)
    root.add_argument("--mala-data-root", type=Path, required=True)
    root.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "observe":
        if (args.market_facts is None) == (args.mala_data_root is None):
            raise SystemExit("provide exactly one of --market-facts or --mala-data-root")
        result: object = observe_one(
            args.batch,
            output_root=args.output_root,
            market_facts_path=args.market_facts,
            mala_data_root=args.mala_data_root,
            observed_at=args.observed_at,
        )
    else:
        batches = sorted(args.recommendation_root.glob("runs/*/*/recommendations.json"))
        runs = [
            observe_one(
                path,
                output_root=args.output_root,
                mala_data_root=args.mala_data_root,
            )
            for path in batches
        ]
        result = {
            "schema": "bhiksha.cartographer_shadow_collection.v1",
            "status": "succeeded",
            "batch_count": len(runs),
            "runs": runs,
            "effects": zero_effects(),
        }
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
