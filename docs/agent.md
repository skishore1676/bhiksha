# Bhiksha Agent Log

Purpose: keep a simple running record of what we changed, what we decided, and what is next so future sessions can resume quickly.

## Ground Rules

- Log concrete decisions, not long essays.
- Record unresolved questions clearly.
- Keep dates in `YYYY-MM-DD`.
- Update this file whenever architecture, scaffold, or trading logic materially changes.

## Session Log

### 2026-03-29

Completed:

- Reviewed the initial draft in `/Users/suman/kg_env/projects/bhiksha/initial_project_draft.md`.
- Audited reusable salvage modules in `/Users/suman/kg_env/projects/bhiksha/tmp/`.
- Reviewed relevant Mala strategy references and execution mapping artifacts.
- Wrote the refined architecture document in `/Users/suman/kg_env/projects/bhiksha/docs/architecture.md`.
- Created the initial repo scaffold under `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/`.
- Added typed config models, config loader, Market Impulse strategy plugin scaffold, and initial deployment manifests for `QQQ` and `SPY`.
- Created a local `.venv`, installed the scaffold dependencies, and verified bootstrap plus the first config-loader test.
- Ported the Newton feature pipeline into `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/market_data/newton/`.
- Added a replay-first signal evaluator and a `.env.example` template for provider and broker credentials.
- Built the first single-leg option selection layer with normalized contract snapshots and selector tests.
- Added env loading plus a new Public broker auth/client boundary under `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/execution/brokers/public/`.
- Ported the first Public account, broker adapter, and order-manager building blocks into the Bhiksha package layout.
- Added a Polygon warm-start adapter and live-safe health check helpers for Public auth and Polygon data.
- Added a Schwab OAuth/client scaffold plus a Schwab warm-start adapter and setup checks, using the locally discovered Schwab auth flow as reference.
- Added a manual Schwab auth helper command and updated the local callback setting to the approved `https://127.0.0.1:8080` URL.
- Completed Schwab OAuth successfully, stored tokens in `/Users/suman/kg_env/projects/bhiksha/config/schwab_tokens.json`, confirmed linked accounts, and verified historical `QQQ` 1-minute candles for 2026-03-27.
- Added a dry-run health command and the first Schwab polling scaffold for closed 1-minute bars.
- Added a dry-run runtime command that warms real bars and evaluates enabled deployments without placing orders.
- Added a real trade-planning layer, SQLite event logging, and guarded `--live` flags for the runtime commands.

Key decisions:

- Day 1 scope is `QQQ` and `SPY` Market Impulse only.
- Bhiksha should still be built as a multi-strategy execution runtime.
- Day 1 vehicle is single-leg options, mostly `0-7` DTE.
- Mala does not yet choose the trading vehicle, so Bhiksha will own a local `Vehicle Resolver`.
- Strategy onboarding should use manifest-style deployment config rather than direct runtime imports from Mala.
- Start as a single-process monolith with clean module boundaries.
- Start with SQLite persistence and strong audit logging.
- Preferred market-data direction is Schwab for live bars, Polygon for backfill/research, Public for execution.

Open items:

- Validate Schwab live 1-minute OHLCV capability hands-on during scaffold work.
- Decide the first exact option-selection defaults for delta, spread width, and premium budget.
- Confirm which broker-native stop behaviors are supported for Day 1 option trades.

Next steps:

1. Port Newton modules into Bhiksha-native paths.
2. Connect planned trades to richer order supervision, especially fill polling and protective stop placement.
3. Add a persistence-backed replay runner so saved bar files can be used as regression tests.
4. Extend continuous evaluation with better session-aware shutdown and summary reporting.

### 2026-03-30

Completed:

- Switched the Public runtime path to production API URLs and validated live-safe account access.
- Verified Public account metadata shows `LEVEL_2` options approval and current options buying power.
- Verified Public real-time option quotes for `QQQ260330P00558000`.
- Verified Public single-leg preflight for the same contract, including tick increment and buying-power requirement.
- Tightened the execution planner to price off Public quotes instead of Schwab-only chain snapshots.
- Added Public preflight enforcement before live order submission.
- Changed Day 1 options orders to use `DAY` time-in-force, matching the official Public single-leg examples.
- Added broker-state reconciliation so the live loop imports existing Public option positions on startup and on every new bar.
- Added hard-flat close handling in the execution supervisor.
- Added a friendlier production entrypoint at `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/tools/trade_session.py`.
- Added a Monday runbook in `/Users/suman/kg_env/projects/bhiksha/docs/deploy_runbook.md`.
- Added tests for quote-aware planning, reconciliation, hard-flat submission, and reservation cleanup.

Key decisions:

- Use Schwab for option discovery and underlying bars, but use Public quotes and Public preflight as the execution source of truth.
- Treat Public portfolio sync as the first Day 1 resume mechanism to avoid duplicate entries after restart.
- Keep Day 1 exits simple: protective stop after fill plus hard-flat market close order at the configured ET cutoff.

Open items:

- First live order submission itself is still untested because we intentionally avoided placing any live orders overnight.
- Stop-order support is inferred from the older project implementation and may need a market-hours confirmation with a tiny live trade.

Next steps:

1. Run the dry-run session before the open on Monday, 2026-03-30.
2. If dry-run looks healthy, switch to `--live` for the session.
3. Watch the first actual fill and confirm the protective-stop behavior in Public.

### 2026-03-30 (Live Validation Follow-Up)

Completed:

- Validated the end-to-end live pipeline with a single-contract QQQ manual probe.
- Confirmed Public quote, preflight, live entry fill, and broker-native catastrophe stop submission.
- Confirmed the running live loop reconciles the open QQQ position back from the broker after the manual fill.
- Updated the architecture document to promote exit management from an implicit concern to a first-class runtime subsystem.

Key decisions:

- Bhiksha now needs a dedicated position lifecycle engine, not just signal evaluation plus entry execution.
- Strategy plugins should expose both entry and exit evaluation.
- `risk` and `exit` need to be separate config domains.
- The scalable design should center on `PositionMonitor`, `ExitPlanner`, and `ProtectionManager`.

Open items:

- VMA reclaim exits are not yet wired into the live runtime.
- Profit-target and profit-lock logic are not yet wired into the live runtime.
- Stop cancellation/replacement on algorithmic exit is not yet wired.

Next steps:

1. Add typed `exit` config to deployment manifests.
2. Refactor strategy plugins to support `evaluate_exit(...)`.
3. Build the open-position monitoring loop and exit planner after the current live session is stable or complete.

### 2026-03-30 (Exit Engine Build)

Completed:

- Added typed `exit` config to deployment manifests and config models.
- Split the strategy contract into `evaluate_entry(...)` and `evaluate_exit(...)`.
- Implemented Market Impulse exit evaluation for VMA reclaim / thesis invalidation.
- Added `ExitDecision` and `ExitPlan` domain models.
- Added `PositionMonitor` to evaluate open positions on completed bars.
- Extended reconciliation so broker-synced positions can inherit known open stop-order IDs.
- Extended the execution supervisor to cancel known protection orders and submit square-off exits from strategy exit decisions.
- Updated the continuous live loop so exits are evaluated before new entries and same-bar re-entry is skipped after an exit.
- Added tests for strategy exits, position monitoring, stop-order reconciliation, and algorithmic exit handling.

Verification:

- `25` tests passing.
- Structural refactor is implemented in code but requires a runtime restart before the live session can use the new exit logic.

Open items:

- Profit-target and breakeven-promotion behaviors are still config modeled but not yet implemented.
- Exit fills are submitted and logged, but advanced target/replace orchestration is still a next step.

Next steps:

1. Restart the runtime when ready to pick up the new exit-monitoring code.
2. Implement target-order and profit-lock policies behind the new `exit` config.
3. Add replay tests that validate full entry-to-exit parity for Market Impulse.

### 2026-03-30 (Target And Breakeven Build)

Completed:

- Extended the execution supervisor to place optional profit-target orders after live fills when enabled by deployment config.
- Added position metadata for entry price, stop price, target price, and target order ID so open positions can be managed coherently after restart.
- Extended broker reconciliation to recover both stop and target protection orders from Public broker state.
- Added open-position maintenance logic to promote the catastrophe stop to breakeven after a configured `R` threshold.
- Updated the live loop to maintain open positions before evaluating new exits or entries on each completed bar.
- Added tests for profit-target placement, breakeven stop promotion, and target-order recovery.

Verification:

- `28` tests passing.
- `python3 -m compileall src tests` passes cleanly.

Open items:

- Profit-target fills are recoverable through reconciliation, but target cancel/replace policies are still basic.
- Replay coverage still focuses on entry and strategy exits; full entry-to-target lifecycle parity is still a next step.

Next steps:

1. Decide whether Market Impulse should enable `use_profit_target` in the active deployment manifests or stay stop-plus-algorithmic-exit only.
2. Add replay cases for target hits, breakeven promotions, and mixed broker-restart recovery.
3. Add session-summary tooling so operator review is easier after a live run.

### 2026-03-30 (Public Exit Constraint Hardening)

Completed:

- Reviewed the older `public_api_trading_v3` runtime for broker-specific lessons around stop cancellation ambiguity and target orchestration.
- Kept the reusable ideas but did not port the old FSM directly into Bhiksha.
- Added an explicit broker capability flag so Public is treated as a `single_resting_exit_order` broker.
- Changed target handling so Public arms a virtual target price instead of placing a broker-side target while a stop is already live.
- Preserved the more general path for future brokers that can support concurrent resting stop and target orders.

Verification:

- `29` tests passing.
- Public-target behavior is now covered in unit tests for both single-exit-order and concurrent-exit-order brokers.

Open items:

- Virtual target hits are still managed conservatively; the more advanced anticipatory target and pullback restore flow from the older project is not ported yet.
- Partial-fill-aware cancel/replace behavior under Public remains a next hardening step.

Next steps:

1. Add broker-aware virtual-target execution flow for Public when a target price is reached.
2. Port the old-project anticipatory target and pullback-restore ideas into manifest-driven config rather than FSM states.
3. Add reconciliation and replay tests for target-pending and stop-restore scenarios.
