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

## Stable operating model

```text
Google Sheet row -> compiled active plan -> ordinary live/shadow execution
                 -> Bhiksha facts/status -> read-only TradeLab analysis
```

Strategy experiments do not require a second mutable registry or a custom runtime
subsystem. `authorization_mode` controls live versus shadow through the same compiler
and risk rails for every strategy row. PDD's expired entry-canary machinery is retained
only in historical release receipts; it has no current compiler, runtime, risk-manager,
or evidence-binding authority. Bhiksha's plan sync reads the Sheet, validates coverage,
and atomically replaces the plan. It does not create Mala packets or advance a binding
registry as a side effect.

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
flattens the book — plus a final sized-entry headroom check, a correlation-cluster position cap,
and per-deployment auto-demote (rolling-10 negative expectancy -> forced shadow via a local
`DemotionStore`; re-promotion is a protected operator action that starts a fresh evidence
window). The runtime must be stopped and the command requires an explicit gate:

```bash
python -m bhiksha.tools.risk_demotion_admin repromote \
  --deployment-id DEPLOYMENT_ID \
  --reason "operator-approved fresh trial" \
  --approved-by OPERATOR \
  --confirm-live-state-change REPROMOTE
```

Knobs resolve
`env > Operator_Defaults_v1 sheet > default` (see `bhiksha.risk.risk_settings.resolve_risk_settings`),
validated at startup with warnings surfaced in the `risk_manager_startup` event. Every consult
emits a `risk_manager_decision` event (throttled to state-changes + heartbeat so the stream stays
readable). The daily session report's **Risk Rails** section renders the resolved thresholds
(pct and $, the $ figure computed against that day's usable budget), the demote window/min_n/
threshold, rail enabled flags, and any validation warnings.

### Operator-editable risk knobs (`Operator_Defaults_v1` sheet)

Env vars always win. To make a knob operator-editable without a deploy, add a row to the
`Operator_Defaults_v1` Google Sheet tab with `section=default` and one of these `key` values
(`value` is the raw knob value, same format as the env var):

| Sheet `key`                    | Env var                                     | Default |
|---------------------------------|----------------------------------------------|---------|
| `max_daily_drawdown_pct`        | `BHIKSHA_RISK_MAX_DAILY_DRAWDOWN_PCT`         | `2.0`   |
| `flatten_daily_drawdown_pct`    | `BHIKSHA_RISK_FLATTEN_DAILY_DRAWDOWN_PCT`     | `3.0`   |
| `demote_window`                 | `BHIKSHA_RISK_DEMOTE_WINDOW`                  | `10`    |
| `demote_min_n`                  | `BHIKSHA_RISK_DEMOTE_MIN_N`                   | `10`    |
| `demote_threshold_usd`          | `BHIKSHA_RISK_DEMOTE_THRESHOLD_USD`           | `0.0`   |
| `rail_a_enabled`                | `BHIKSHA_RISK_RAIL_A_ENABLED`                 | `true`  |
| `rail_b_enabled`                | `BHIKSHA_RISK_RAIL_B_ENABLED`                 | `true`  |
| `prospective_loss_enabled`      | `BHIKSHA_RISK_PROSPECTIVE_LOSS_ENABLED`       | `true`  |
| `max_open_positions_per_cluster` | `BHIKSHA_RISK_MAX_OPEN_POSITIONS_PER_CLUSTER` | `1`     |

These keys are exactly the env var name with the `BHIKSHA_RISK_` prefix stripped and
lowercased — see `bhiksha.risk.plan_operator_defaults_source` for the concrete `SettingsSource`
and the exact derivation it must match. The sheet is read once at plan-compile time (it is
carried on the compiled `active_plan.json` as `operator_defaults`, not re-read live), so a sheet
edit takes effect on the next plan sync/session start — same cadence as any other
`Operator_Defaults_v1` row. Sheet values pass through the same validation/clamping as env values;
an invalid value falls back to the default and is reported in `validation_warnings`.
Set `max_open_positions_per_cluster=0` to disable only the cluster cap. See
`docs/prospective_entry_risk.md` for the loss formula, confirmed cluster map, and event evidence.

## Where to look

- Daily session reports: `artifacts/playbook/reports/trade_session_report_YYYY-MM-DD.{md,json}`,
  delivered to Telegram via Lathi Bus at 09:10 / 11:45 / 14:45 CT.
- Runtime truth: `bhiksha.db` (`events`, `trade_sessions`); compiled plan:
  `artifacts/playbook/active_plan.json` (incl. `gate_override_key_warnings`, suppression reasons).
- Deploy/runbook: `docs/deploy_runbook.md`. Launchd contract: `docs/bhiksha_launchd.md`.
- Exit Engine V2 current runtime boundary: `docs/EXIT_ENGINE_V2_INCREMENT_2.md`
  (Increment 1 deployment record: `docs/EXIT_ENGINE_V2_INCREMENT_1.md`).
- Hard-won operating lessons: `docs/lessons/`.
- Cross-repo follow-on workplan: `mala_v2/docs/LIVE_LOOP_WORKPLAN.md`.
