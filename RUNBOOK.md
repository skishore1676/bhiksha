# Bhiksha Runbook

Purpose: Bhiksha is the config-driven live execution runtime for Mala strategy
deployments — live trading against Schwab/Public broker accounts on oldmac
(user sunny, uid 501), driven by launchd jobs.

## launchd jobs

All five run via `scripts/launchd/run_bhiksha_job.sh`:

| Label | Role |
|---|---|
| `com.bhiksha.live-start` | starts the live trading session |
| `com.bhiksha.live-stop` | stops the live trading session |
| `com.bhiksha.live-watchdog` | watches the live session |
| `com.bhiksha.schwab-guard` | refreshes Schwab broker tokens |
| `com.bhiksha.session-report` | intraday session report + Telegram summary |

Restart pattern: `launchctl kickstart -k gui/501/<label>`.
WARNING: for on-demand jobs kickstart RUNS the job — it is an action, not a
restart. Kickstarting live-start / live-stop / schwab-guard is itself a
live-trading-adjacent action. Do not do it casually or automatically.

## Tests

```
cd /Users/sunny/Documents/bhiksha
PYTHONPATH=src .venv/bin/python -m pytest            # full suite
PYTHONPATH=src .venv/bin/python -m pytest tests/test_bhiksha_launchd.py tests/test_launchd_control_status.py
```

## Logs

- `artifacts/playbook/launchd/<label>.{out,err}.log` — per-job launchd logs
- `artifacts/playbook/launchd/latest_status.json` — last recorded job payloads
- `artifacts/playbook/reports/` — session reports
- `artifacts/playbook/schwab_token_guard/latest.json` — last token-guard result

## Health verification

1. `artifacts/playbook/launchd/latest_status.json` — recent `recorded_at`,
   per-job `status: ok`.
2. Status tool (read-only, budget-bounded, valid JSON even when probes
   time out): `.venv/bin/python -m bhiksha.tools.launchd_status --json`.
   Overall budget defaults to 15s (override: `BHIKSHA_STATUS_BUDGET_SECONDS`);
   degraded probe values: `timeout`, `error`, `not_checked`.
3. lathi Control Tower (on the same box) surfaces this repo as source
   `bhiksha` — BHK-0x units should show `last_run_status` ok/skipped
   (`skipped` on non-trading days is normal).

## DANGER ZONES (verbatim from the 2026-07-05 on-call audit)

DANGER: **live trading + broker auth** — `config/schwab_tokens.json`,
`config/public_account.json`, `public_session.json`,
`google-credentials.json`. live-start/stop control a live session.
schwab-guard refreshes broker tokens — automated fixer must never touch
these files or kickstart live-* jobs.

bhiksha: best test suite but the highest-stakes secrets (Schwab/Public
tokens). schwab-guard already auto-refreshes; a second fixer touching auth
would race it.

Operating rule: anything under schwab_auth / schwab token guard /
server_session internals / submit-manage-trade tools / config or session
json is DIAGNOSE-ONLY. A fixer (human or automated) must never modify these;
racing schwab-guard's refresh can corrupt or invalidate the broker session.
