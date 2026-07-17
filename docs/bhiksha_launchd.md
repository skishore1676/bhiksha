# Bhiksha Launchd Jobs

Bhiksha-owned launchd labels do not use OpenClaw names. OpenClaw may remain
available elsewhere, but these jobs are owned, installed, and debugged from the
Bhiksha repo.

## Install

From the Bhiksha repo on the runtime Mac:

```bash
scripts/launchd/install_bhiksha_launchd.sh install
```

To remove the Bhiksha-owned jobs:

```bash
scripts/launchd/install_bhiksha_launchd.sh uninstall
```

The installer writes plists into `~/Library/LaunchAgents` and logs under:

```text
artifacts/playbook/launchd/
```

The active launchd registry lives in code at:

```text
src/bhiksha/ops/launchd_registry.py
```

The installer reads that registry, so labels, schedules, risk classes, and
manual Control Tower actions do not need to be duplicated in shell.

## Jobs

| Label | Schedule | Purpose |
| --- | --- | --- |
| `com.bhiksha.live-start` | Weekdays 08:20 CT | Restart Bhiksha live runtime from the active plan. Skips non-trading days. |
| `com.bhiksha.live-watchdog` | Weekdays every 10 minutes from 08:30 through 15:00 CT | Ensure the live runtime is still running. Skips non-trading days. |
| `com.bhiksha.reconciliation-supervisor` | Weekdays every 10 minutes from 08:30 through 15:00 CT | Verify entry holds self-heal, record receipts, and escalate only unresolved ambiguity older than five minutes. |
| `com.bhiksha.live-stop` | Weekdays 15:10 CT | Stop the live runtime. It does not skip non-trading days, so stale processes can still be cleaned up. |
| `com.bhiksha.schwab-guard` | Trading days 07:10 and 15:20 CT | Verify premarket auth and, after close, renew whenever the token will not survive the next full trading session. Skips non-trading days. |
| `com.bhiksha.session-report` | Weekdays 09:10, 11:45, and 14:45 CT | Send an intraday session report with open positions, realized P&L, protection state, provider/runtime issues, and recent trades early enough for manual action. Skips non-trading days. |
| `com.bhiksha.weekly-trading-decisions` | Fridays 16:00 CT (after close) | Refresh the canonical Trading Decision Ledger and publish one Obsidian review for performance, promotion decisions, and fixes. Shadow-EV and weekly-scorecard calculations are internal inputs; no Telegram send. |

Launchd cannot natively express market holidays, so the runner performs the
trading-day check before doing work.

The session report intentionally uses Lathi Bus's Telegram `status` template.
Telegram gets a compact operator card: quick read, open positions, watch items,
and a pointer to the full markdown report. The markdown artifact remains the
long-form source for full strategy evidence, raw relaxed-gate details, lifecycle
events, and risk-rail audit lines.

## Manual Runs

All labels use one runner:

```bash
scripts/launchd/run_bhiksha_job.sh live-start
scripts/launchd/run_bhiksha_job.sh live-watchdog
scripts/launchd/run_bhiksha_job.sh reconciliation-supervisor
scripts/launchd/run_bhiksha_job.sh live-stop
scripts/launchd/run_bhiksha_job.sh schwab-refresh
scripts/launchd/run_bhiksha_job.sh session-report --report-label manual
scripts/launchd/run_bhiksha_job.sh weekly-trading-decisions --weekly-review-mode off
```

The weekly decision job writes normalized facts, refreshes the canonical Excel
ledger, and publishes one stable week-keyed card through Lathi Bus's Obsidian
profile. It never sends Telegram. A failed workbook refresh prevents publication
so stale math is not presented as a current review.

Use `--force` to bypass the trading-day skip during testing:

```bash
scripts/launchd/run_bhiksha_job.sh session-report --force --report-label test
```

The runner accepts `--action-id` so a Control Tower action journal entry can be
correlated with the Bhiksha receipt:

```bash
scripts/launchd/run_bhiksha_job.sh session-report --force --report-label manual --action-id manual-test-001
```

Every runner invocation attempts to update:

```text
artifacts/playbook/launchd/latest_status.json
```

That file is observational. If it cannot be written, the scheduled trading job
must not fail only because the status breadcrumb failed.

## Status And Control

External observers such as Lathi Control Tower should use the read-only status
command instead of scraping logs directly:

```bash
python -m bhiksha.tools.launchd_status --json
```

The status payload uses schema `bhiksha.launchd.status.v1` and includes:

- all seven active `com.bhiksha.*` jobs;
- launchd loaded/exit state where available;
- latest `BHIKSHA_LAUNCHD_JOB` payloads;
- report and Schwab guard summaries;
- live runtime status;
- transport health separate from trading-domain health.

Manual actions for Control Tower go through:

```bash
python -m bhiksha.tools.launchd_control live-status --json
python -m bhiksha.tools.launchd_control session-report-now --json
python -m bhiksha.tools.launchd_control schwab-guard-now --json
python -m bhiksha.tools.launchd_control renew-schwab-access --confirm --json
python -m bhiksha.tools.launchd_control ensure-live-runtime --json
```

`launchd_control` emits schema `bhiksha.launchd.control_result.v1`, creates or
accepts `--action-id`, and uses per-action lock files under
`artifacts/playbook/launchd/control_locks/` so duplicate manual actions do not
run concurrently.

`ensure-live-runtime` is trading-adjacent. It refuses without `--confirm` when
the market is open or when it would start a stopped live runtime:

```bash
python -m bhiksha.tools.launchd_control ensure-live-runtime --confirm --json
```

`renew-schwab-access` always requires `--confirm`. It forces the headed browser
OAuth path, requires a newly-issued refresh token, then verifies linked-account
access plus QQQ/IWM quotes and option chains. It never places orders.
`schwab-guard-now` is deliberately probe/direct-refresh only and never starts
browser OAuth; a failed probe points the operator to the confirmed renewal
action. Live startup uses the same session-aware token classification and stays
blocked when authentication cannot remain trusted through the full session.

## Cutover Verification

1. Install or reinstall Bhiksha-owned launchd jobs.
2. Read back `launchctl list | grep bhiksha` and verify the seven `com.bhiksha.*`
   labels are loaded.
3. Run `python -m bhiksha.tools.launchd_status --json` and verify the six
   jobs appear with schema `bhiksha.launchd.status.v1`.
4. Kickstart or manually run `com.bhiksha.session-report` and verify the Telegram
   report arrives through Lathi Bus.
5. Kickstart or manually run `com.bhiksha.schwab-guard` and verify a healthy
   token receipt or an actionable Lathi Bus alert.
6. Confirm live runtime status through `python -m bhiksha.tools.launchd_control
   live-status --json`.
7. Confirm `ensure-live-runtime` requires confirmation when it would start a
   stopped live runtime.
8. Verify old OpenClaw/browser-agent Bhiksha labels are not loaded.

## Archived Legacy Labels

These labels were replaced by the Bhiksha-owned launchd jobs and should remain
unloaded/archived:

```text
ai.openclaw.bhiksha-live-start
ai.openclaw.bhiksha-live-watchdog
ai.openclaw.bhiksha-live-stop
ai.openclaw.bhiksha-eod-receipt
ai.openclaw.bhiksha-schwab-refresh
com.bhiksha.schwab-refresh
```

The active labels are:

```text
com.bhiksha.live-start
com.bhiksha.live-watchdog
com.bhiksha.reconciliation-supervisor
com.bhiksha.live-stop
com.bhiksha.schwab-guard
com.bhiksha.session-report
com.bhiksha.weekly-trading-decisions
```
