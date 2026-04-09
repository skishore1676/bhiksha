# Bhiksha Deploy Runbook

Bhiksha now runs from a Google Sheets control plane.

The live authority is the compiled active plan at [active_plan.json](/Users/suman/kg_env/projects/bhiksha/artifacts/playbook/active_plan.json). Bhiksha builds that file from your Google Sheet, not from `config/deployments/` and not from a daily Mala compile.

## Control Plane

Use these 3 sheet tabs:

- `Strategy_Catalog`
  - maintained by Mala or by occasional manual promotion work
  - rows are promotable when `lifecycle_status=active` and `bionic_ready=true`
- `active_strategy`
  - turns approved catalog strategies on or off for the current session
  - `strategy` should contain the `catalog_key`
- `manual_entry`
  - defines operator-authored manual setups like breakout triggers

Bhiksha allows multiple same-symbol lanes, so `SPY` can have multiple active strategy rows and manual rows at the same time.

## Time Conventions

Enter sheet times in ET.

- `active_strategy.start`
  - earliest entry time for that strategy lane
- `active_strategy.end`
  - optional latest entry time
- `manual_entry.after`
  - earliest trigger activation time

Google Sheets may convert `09:30` to `9:30`. Bhiksha now normalizes that automatically, so both formats are accepted.

## Required Env

These should be present on the server:

- `GOOGLE_SHEET_ID`
- `GOOGLE_API_CREDENTIALS_PATH`
- `STRATEGY_CATALOG_SHEET_NAME`
- `ACTIVE_STRATEGIES_SHEET_NAME`
- `MANUAL_ENTRY_SHEET_NAME`

Optional:

- `BHIKSHA_ACTIVE_PLAN_PATH`
  - defaults to [active_plan.json](/Users/suman/kg_env/projects/bhiksha/artifacts/playbook/active_plan.json)
- `BHIKSHA_STRATEGY_CATALOG_PATH`
  - defaults to [strategy_catalog](/Users/suman/kg_env/projects/bhiksha/config/strategy_catalog)
- `BHIKSHA_ACTIVE_PLAN_LOG_DIR`
  - defaults to [logs](/Users/suman/kg_env/projects/bhiksha/artifacts/playbook/logs)
- `BHIKSHA_ACTIVE_PLAN_SYNC_MINUTES`
  - optional polling interval for repeated sync

## Morning Workflow

Preferred one-command flow:

- dry restart:
  - `PYTHONPATH=src .venv/bin/python -m bhiksha.tools.server_session restart`
- live restart:
  - `PYTHONPATH=src .venv/bin/python -m bhiksha.tools.server_session restart --live`

That command:

1. syncs the Google Sheet into the active plan
2. stops any currently running Bhiksha server process
3. starts Bhiksha again against the refreshed active plan

Manual equivalents if needed:

1. Update the Google Sheet.
2. Sync the active plan:
   - `PYTHONPATH=src .venv/bin/python -m bhiksha.tools.sync_active_plan`
3. Dry startup:
   - `PYTHONPATH=src .venv/bin/python -m bhiksha.tools.trade_session --active-plan artifacts/playbook/active_plan.json --max-bars 0`
4. Start live only when the plan looks correct:
   - `PYTHONPATH=src .venv/bin/python -m bhiksha.tools.trade_session --active-plan artifacts/playbook/active_plan.json --live`

## Intraday Reload

If you change your mind during the day:

1. update the Google Sheet
2. rerun:
   - `PYTHONPATH=src .venv/bin/python -m bhiksha.tools.server_session restart`
   - or `... restart --live` if you are already in live mode

Hot-reloading inside the same runtime is not the primary path yet. The intended operating model is sync plus restart.

## Server Process Commands

- status:
  - `PYTHONPATH=src .venv/bin/python -m bhiksha.tools.server_session status`
- stop:
  - `PYTHONPATH=src .venv/bin/python -m bhiksha.tools.server_session stop`
- start in dry mode:
  - `PYTHONPATH=src .venv/bin/python -m bhiksha.tools.server_session start`
- start in live mode:
  - `PYTHONPATH=src .venv/bin/python -m bhiksha.tools.server_session start --live`

## Logging And Bad Rows

Each sync appends a JSON log entry to:

- [active_plan_sync_2026-04-09.jsonl](/Users/suman/kg_env/projects/bhiksha/artifacts/playbook/logs/active_plan_sync_2026-04-09.jsonl)

The dated file records:

- sync status
- deployment summary
- suppressed row count
- suppressed row details with sheet name, row id, row index, and reason

Bad rows no longer fail the whole sync. Bhiksha keeps valid rows, suppresses invalid rows, and records the issue in the sync log.

## Notes

- `config/strategy_catalog/google_promoted/` is generated from eligible `Strategy_Catalog` rows.
- Manual breakout rows currently compile through the `manual_trigger` runtime path.
- Post-close review still exports feedback bundles and can optionally copy them back to `mala_v1`.
