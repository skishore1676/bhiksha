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

## Jobs

| Label | Schedule | Purpose |
| --- | --- | --- |
| `com.bhiksha.live-start` | Weekdays 08:20 CT | Restart Bhiksha live runtime from the active plan. Skips non-trading days. |
| `com.bhiksha.live-watchdog` | Weekdays every 10 minutes from 08:30 through 15:00 CT | Ensure the live runtime is still running. Skips non-trading days. |
| `com.bhiksha.live-stop` | Weekdays 15:10 CT | Stop the live runtime. It does not skip non-trading days, so stale processes can still be cleaned up. |
| `com.bhiksha.schwab-guard` | Weekdays 07:10 CT | Run the Schwab token guard; direct refresh first, browser-agent renewal only when needed. Skips non-trading days. |
| `com.bhiksha.session-report` | Weekdays 09:10, 11:45, and 14:45 CT | Send an intraday session report with open positions, realized P&L, protection state, provider/runtime issues, and recent trades early enough for manual action. Skips non-trading days. |

Launchd cannot natively express market holidays, so the runner performs the
trading-day check before doing work.

## Manual Runs

All labels use one runner:

```bash
scripts/launchd/run_bhiksha_job.sh live-start
scripts/launchd/run_bhiksha_job.sh live-watchdog
scripts/launchd/run_bhiksha_job.sh live-stop
scripts/launchd/run_bhiksha_job.sh schwab-refresh
scripts/launchd/run_bhiksha_job.sh session-report --report-label manual
```

Use `--force` to bypass the trading-day skip during testing:

```bash
scripts/launchd/run_bhiksha_job.sh session-report --force --report-label test
```

## Cutover Verification

1. Install or reinstall Bhiksha-owned launchd jobs.
2. Read back `launchctl list | grep bhiksha` and verify the five `com.bhiksha.*`
   labels are loaded.
3. Kickstart or manually run `com.bhiksha.session-report` and verify the Telegram
   report arrives through Lathi Bus.
4. Kickstart or manually run `com.bhiksha.schwab-guard` and verify a healthy
   token receipt or an actionable Lathi Bus alert.
5. Confirm live runtime status through `scripts/launchd/run_bhiksha_job.sh
   live-watchdog --force` or `python -m bhiksha.tools.server_session status`.
6. Verify old OpenClaw/browser-agent Bhiksha labels are not loaded.

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
com.bhiksha.live-stop
com.bhiksha.schwab-guard
com.bhiksha.session-report
```
