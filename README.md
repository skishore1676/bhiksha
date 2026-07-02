# Bhiksha — live options execution runtime

Bhiksha executes options strategies live on the Public.com API. It consumes validated strategy
evidence from the Mala research engine via the `Mala_Evidence_v1` Google Sheet, compiles the
operator's `active_strategies` rows into per-lane deployments each session, and runs entries,
exits, protection, reconciliation, and risk rails against real (or paper/shadow) fills.

- Production runs on oldmac (`~/Documents/bhiksha`) via launchd (see `docs/bhiksha_launchd.md`);
  this repo is the dev checkout. **Merged ≠ deployed** — deploy is an explicit `git pull` on the box.
- The Google Sheet is the operator's control surface: `mode=live|shadow`, arming cells, premium
  caps. Gate-affecting cells are honored and audit-surfaced, never silently stripped
  (`docs/lessons/sheet-is-the-operator-control-surface.md`).

## Exit authority rule (operator rule, 2026-07-02 — not overridable by config)

**When the profile-exit route is armed for a deployment (live, `profile_exit_drives_live` +
`runtime_mode=live_approval_gated`, not shadow-only), the profile owns ALL profit-taking.**
The runtime must not arm any full-size profit target for that deployment — neither a resting
broker target order nor the virtual-target machinery. Entry and exit are fully planned by the
profile contract: bank a partial at T1, stop to breakeven, ride the runner to T2 with high-water
giveback, no-progress time exit, hard flat at EOD.

The one thing that always rests at the broker regardless: the **protective stop**. If the runtime
dies mid-position, the stop and the EOD flat are the backstops; the cost of a crash is a missed
partial, never an unprotected position.

Why this rule exists: a full-size resting target at +35% fills broker-side before the ladder can
bank its partial — on 2026-07-02 (flip day 1) NVDA and AMD exited 100% at 1R and the T2 runner
could never happen. Enforced in code at the single profit-target gate in
`src/bhiksha/execution/supervisor.py` (`_profit_target_configured`), deliberately with no config
override.

## Risk rails (always on for live lanes)

Two-tier daily drawdown on realized live P&L vs usable budget — tier-1 halts new entries, tier-2
flattens the book — plus per-deployment auto-demote (rolling-10 negative expectancy → forced
shadow via a local `DemotionStore`; re-promotion is a deliberate operator edit). Knobs are env
vars (`BHIKSHA_RISK_*`), validated at startup with warnings surfaced in the `risk_manager_startup`
event. Every consult emits a `risk_manager_decision` event (throttled to state-changes + heartbeat
so the stream stays readable).

## Where to look

- Daily session reports: `artifacts/playbook/reports/trade_session_report_YYYY-MM-DD.{md,json}`,
  delivered to Telegram via Lathi Bus at 09:10 / 11:45 / 14:45 CT.
- Runtime truth: `bhiksha.db` (`events`, `trade_sessions`); compiled plan:
  `artifacts/playbook/active_plan.json` (incl. `gate_override_key_warnings`, suppression reasons).
- Deploy/runbook: `docs/deploy_runbook.md`. Launchd contract: `docs/bhiksha_launchd.md`.
- Hard-won operating lessons: `docs/lessons/`.
- Cross-repo follow-on workplan: `mala_v2/docs/LIVE_LOOP_WORKPLAN.md`.
