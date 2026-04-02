"""Legacy importer for pre-session-payload generated Bhiksha deployments."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import sys

from bhiksha.loop.importer import refresh_generated_deployments


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Legacy-only: import deployment candidates into generated shadow manifests."
    )
    parser.add_argument("--config-root", default="config", help="Bhiksha config root")
    parser.add_argument("--deployment-candidates", required=True, help="Path to deployment_candidates.json")
    parser.add_argument("--playbook-catalog", required=True, help="Path to playbook_catalog.json")
    parser.add_argument("--bias-inputs", default=None, help="Optional override path to bias_inputs.yaml")
    parser.add_argument("--max-export-age-hours", type=int, default=36, help="Maximum allowed age for exported artifacts")
    args = parser.parse_args(argv)

    print(
        "WARNING: import_playbook is a legacy path. In Bionic mode, prefer Mala's active_session compiler "
        "and start Bhiksha with --session-payload.",
        file=sys.stderr,
    )

    report = refresh_generated_deployments(
        config_root=args.config_root,
        deployment_candidates_path=args.deployment_candidates,
        playbook_catalog_path=args.playbook_catalog,
        bias_inputs_path=args.bias_inputs,
        max_export_age_hours=args.max_export_age_hours,
    )
    print(json.dumps(asdict(report), indent=2, sort_keys=True))
    return 0 if report.safe_for_live_review else 2


if __name__ == "__main__":
    raise SystemExit(main())
