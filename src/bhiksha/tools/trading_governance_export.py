"""Export current read-only trading governance evidence as JSON."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path

from bhiksha.config.models import ActivePlan
from bhiksha.ops.trading_governance_evidence import build_trading_governance_evidence
from bhiksha.ops.weekly_scorecard import build_weekly_scorecard


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument(
        "--active-plan",
        type=Path,
        default=Path("artifacts/playbook/active_plan.json"),
    )
    parser.add_argument("--demotion-store", type=Path)
    parser.add_argument("--through", default=date.today().isoformat())
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    plan = ActivePlan.model_validate_json(args.active_plan.read_text(encoding="utf-8"))
    scorecard = build_weekly_scorecard(
        args.db,
        week_end=args.through,
        deployments=plan.deployments,
    )
    evidence = build_trading_governance_evidence(
        scorecard,
        through=args.through,
        deployments=plan.deployments,
        demotion_store_path=args.demotion_store,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(args.output)
    print(
        json.dumps(
            {
                "status": "ok",
                "output": str(args.output),
                "receipt": evidence["receipt"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
