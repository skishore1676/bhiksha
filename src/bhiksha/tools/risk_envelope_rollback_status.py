"""Read-only inspection of durable Dynamic Risk Envelope rollback latches."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bhiksha.persistence.exit_state import (
    inspect_risk_envelope_rollback_latches,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("bhiksha.db"),
        help="Bhiksha SQLite database; opened read-only.",
    )
    parser.add_argument(
        "--deployment-id",
        action="append",
        help="Optional deployment filter; repeatable.",
    )
    args = parser.parse_args(argv)
    deployment_ids = (
        set(args.deployment_id)
        if args.deployment_id
        else None
    )
    latches = inspect_risk_envelope_rollback_latches(
        args.db,
        deployment_ids=deployment_ids,
    )
    print(
        json.dumps(
            {
                "schema": "bhiksha.risk_envelope_rollback_status.v1",
                "database": str(args.db.resolve()),
                "read_only": True,
                "rollback_latched_count": len(latches),
                "latches": latches,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
