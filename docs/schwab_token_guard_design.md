# Schwab Token Guard Design

## Ownership

Bhiksha owns Schwab token state, the decision to renew, and the decision to
alert. Browser-agent is only the headed-browser adapter. OpenClaw launchd is
only the scheduler/evidence wrapper. Lathi Bus is the human notification/gate
transport.

## Repository Map

| Repo | Owns | Change |
| --- | --- | --- |
| `bhiksha` | Schwab token classification, safe token-endpoint refresh, alert decision, receipts, operator-facing CLI | Add `bhiksha.tools.schwab_token_guard`, `bhiksha.ops.alerts`, and `scripts/schwab_token_guard.sh`. |
| `browser-agent` | Headed Schwab OAuth browser work | Keep `scripts/schwab-auto-refresh.sh` as an invoked adapter; remove independent LaunchAgent schedule from `deploy/com.bhiksha.schwab-refresh.plist`. |
| `openclaw-core` | oldmac launchd wrapper and runtime evidence | Point `send_bhiksha_schwab_refresh.sh` at Bhiksha's guard CLI. Do not rely on OpenClaw for Schwab token failure alerts. |
| `lathi-bus` | Receipt/approval/Telegram transport | Provide `telegram-notify`; Bhiksha owns whether and when the alert is sent. |
| `lane-host` / `lathi` | Higher-level orchestration | No Schwab-token runtime change in this slice. |

## Browser-Agent Adoption Contract

Bhiksha does not import browser-agent as a Python package and does not consume it
through `uv`. The integration boundary is the stable executable adapter:

```text
/Users/sunny/code/browser-agent/scripts/schwab-auto-refresh.sh
```

If browser-agent improves in a later week, Bhiksha adopts those improvements
when `/Users/sunny/code/browser-agent` is pulled or synced on oldmac. No Bhiksha
code change is required as long as the adapter contract stays stable:

- exit `0` when the token is already healthy or the browser renewal succeeded;
- exit non-zero when it cannot make the token usable;
- honor `BROWSER_AGENT_NOTIFY=0` so Bhiksha owns alerting;
- do not print tokens, auth codes, callback secrets, or bearer values;
- let Bhiksha re-check the real Schwab token file after the adapter returns.

The long-term conformance test should live at this boundary: a browser-agent
contract mode that validates executable path, environment handling, redaction,
and exit-code semantics without opening a real Schwab OAuth session.

## Runtime Flow

1. Launchd wakes OpenClaw's `ai.openclaw.bhiksha-schwab-refresh`.
2. OpenClaw runs `scripts/lanes/send_bhiksha_schwab_refresh.sh`.
3. That wrapper runs Bhiksha:

   ```bash
   /Users/sunny/Documents/bhiksha/scripts/schwab_token_guard.sh \
     premarket \
     --browser-renewal-mode auto \
     --browser-renewal-cmd /Users/sunny/code/browser-agent/scripts/schwab-auto-refresh.sh \
     --alert-mode live \
     --alert-profile jarvis-northstar \
     --json
   ```

4. Bhiksha classifies token state:
   - `healthy`: no-op success.
   - `access_token_stale`: safe token-endpoint refresh.
   - `refresh_token_near_expiry`: safe token-endpoint refresh first.
   - `refresh_token_expired`, `token_file_missing`, or refresh failure:
     invoke browser-agent only when browser renewal mode is `auto` or `force`.
5. Bhiksha writes a redacted structured receipt under
   `artifacts/playbook/schwab_token_guard/`.
6. If the final token state is not usable, Bhiksha sends a Lathi Bus
   `telegram-notify` alert and exits non-zero.
7. OpenClaw records launchd evidence only; it is not the alert owner for this
   failure path.

## Safety Boundaries

- Bhiksha receipts never include access tokens, refresh tokens, auth codes, or
  bearer values.
- Browser-agent is not scheduled independently; it runs only when called by
  Bhiksha or manually by an operator.
- Bhiksha alerts use Lathi Bus and are redacted before dispatch.
- Failure alerts are intentionally loud in Telegram: siren/red-symbol title,
  `ACTION REQUIRED` first line, and explicit fail-closed language. Routine info
  receipts stay plain.
- In `live` alert mode, Bhiksha treats Lathi Bus success as real only when the
  Lathi receipt says `network_call_performed=true`. A spool-only packet is not
  counted as delivered.
- Bhiksha discovers Lathi Bus via `lathi-bus` on `PATH` or `~/code/lathi-bus`.
  On oldmac it can use existing lane-host Telegram secret-file paths through
  Lathi Bus environment overrides; it does not call OpenClaw to send the alert.
- Startup trading health may fail closed if Schwab auth is unusable; the
  browser adapter is premarket maintenance, not an in-trade surprise action.

## Launchd Direction

Bhiksha-owned LaunchAgents are documented in `docs/bhiksha_launchd.md`. The
new labels are `com.bhiksha.*`, not `ai.openclaw.*`, and they all run through
`scripts/launchd/run_bhiksha_job.sh`.

Proposed sequence:

1. Install `com.bhiksha.schwab-guard` as the new scheduler for Bhiksha's guard.
   Bhiksha owns alerting immediately.
2. Replace the browser-agent `com.bhiksha.schwab-refresh` LaunchAgent with no
   schedule. Browser-agent remains an adapter only.
3. Use `com.bhiksha.live-start`, `com.bhiksha.live-watchdog`, and
   `com.bhiksha.live-stop` for runtime control.
4. Replace EOD-only receipt with `com.bhiksha.session-report`, scheduled at
   09:10, 11:45, and 14:45 CT on trading days so the operator can still act
   manually when the report surfaces something odd.
5. After the above, OpenClaw becomes optional scheduling/evidence infrastructure
   for Bhiksha rather than a required dependency for trading health.
