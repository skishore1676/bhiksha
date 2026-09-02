# Bhiksha Deploy Runbook

Bhiksha now runs from a Google Sheets control plane.

The live authority is the compiled active plan at [active_plan.json](/Users/suman/kg_env/projects/bhiksha/artifacts/playbook/active_plan.json). Bhiksha builds that file from your Google Sheet, not from `config/deployments/` and not from a daily Mala compile.

## Control Plane

Use these 3 sheet tabs:

- `Strategy_Catalog`
  - maintained by Mala or by occasional manual promotion work
  - rows are importable when `bhiksha_ready=true` and `lifecycle_status` is `active` or `candidate`
  - `candidate` rows can still run if you explicitly enable them in `active_strategy`
- `active_strategy`
  - turns approved catalog strategies on or off for the current session
  - `strategy` should contain the `catalog_key`
  - `mode` controls execution authorization: `live` submits real orders, `shadow` does not
- `manual_entry`
  - defines operator-authored manual setups like breakout triggers

`operator_status_override` in `Strategy_Catalog` is retained as review metadata.
It does not override `active_strategy.mode`; use the active sheet when deciding
what is live for today's session.

Bhiksha allows multiple same-symbol lanes, so `SPY` can have multiple active strategy rows and manual rows at the same time.

All strategy rows share this contract. A row does not gain a new compiler or risk
subsystem because it began as an experiment: `mode=shadow` observes it without broker
orders and `mode=live` routes it through the existing live execution and risk rails.
Historical PDD canary metadata is provenance only. The sync does not write Mala evidence
packets or a mutable evidence-binding registry.

Runtime safety rails may refuse an individual live entry. They do not persistently change
the row's mode or override a future compile: the next plan again follows the Sheet. Rail B
records a session-scoped refusal and tells the operator to change the Sheet when a lane
should remain shadow across sessions.

For manual rows, Bhiksha now writes back execution status into the same `manual_entry` tab.

- new status columns are added automatically when needed:
  - `bhiksha_status`
  - `bhiksha_last_event_at`
  - `bhiksha_last_note`
  - `bhiksha_last_trade_id`
- when a manual trigger fires, Bhiksha also flips `enabled` to `FALSE`
  - this makes the row one-shot by default, so yesterday's triggered manual setup does not silently re-arm tomorrow
  - re-enable the row manually if you want to reuse it

Cartographer-owned rows use a narrower operator-facing contract. Bhiksha writes the
immediate one-shot trigger latch and then the final terminal M:P status; intermediate
entry-planned and exit-pending states remain in Bhiksha's event/trade stores. Every
Cartographer terminal write repeats `enabled=FALSE`, so it can repair a failed trigger
latch. One process-local lock serializes the shared Google client across rows, while
the existing one bounded transport retry and failure event remain unchanged. Human
manual rows retain their prior intermediate writebacks.

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
- `BHIKSHA_CASH_GUARD_MODE`
  - defaults to `auto`
  - `auto` enables the guard for Public `CASH` accounts and leaves it off for `MARGIN`
  - `on` always enforces the guard
  - `off` disables the guard
- `BHIKSHA_CASH_GUARD_BUFFER_PCT`
  - defaults to `0.05`
  - Bhiksha seeds a daily usable cash budget from Public `cashOnlyBuyingPower`, applies this haircut, and does not recycle same-day sale proceeds back into that budget

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

The Sheet control-plane sync absorbs transient Google failures with four retries after the
initial request using randomized exponential backoff. A failure reaches Beacon
only after that budget is exhausted. Scheduled watchdog recovery also syncs a
fresh plan before starting a stopped runtime; it does not fall back to a stale
plan.

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

Diagnostic compilation keeps valid rows, suppresses invalid rows, and records
the issue in the sync log. Canonical sync is stricter: it atomically replaces
the installed plan only when every enabled row is accounted for. Named policy
gates such as `triage_verdict=KILL`, `m7_status=block`, retired, or
not-runtime-ready are safe suppressions. Invalid input, duplicate row identity,
unexplained compiler loss, or a shadow observation-binding mismatch preserves
the previous active plan instead of publishing a partial candidate.

The runtime log now also emits clearer operator-facing lines such as:

- `SIGNAL_TRUE`
- `ENTRY_PLANNED`
- `ENTRY_SUBMITTED`
- `ENTRY_BLOCKED`
- `ENTRY_SELECTOR_EMPTY`
- `EXIT_TRIGGERED`
- `EXIT_SUBMITTED`
- `RECONCILIATION_DEGRADED`

## Notes

- `config/strategy_catalog/google_promoted/` is generated from eligible `Strategy_Catalog` rows.
- Manual breakout rows currently compile through the `manual_trigger` runtime path.
- Post-close review still exports feedback bundles and can optionally copy them back to `mala_v1`.
