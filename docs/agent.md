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

### 2026-03-30 (Public Virtual Target Orchestration)

Completed:

- Added manifest-driven `target_approach_offset_pct` and `target_pullback_restore_progress_pct` exit config fields.
- Implemented Public virtual-target activation inside `manage_open_position(...)`:
  the runtime can now arm a target price, cancel the catastrophe stop as price approaches target, and submit the target order.
- Preserved the older production nuance where Bhiksha may still attempt target submission when Public cancel confirmation is ambiguous.
- Implemented pullback-based stop restoration:
  if the target is active and price fades back below the configured progress threshold, Bhiksha cancels the target and restores catastrophe-stop protection.
- Added unit coverage for virtual target activation, ambiguous stop-cancel fallback, and target-pullback stop restoration.

Verification:

- `32` tests passing.
- `python3 -m compileall src tests` passes cleanly.

Open items:

- The active QQQ/SPY manifests still keep `use_profit_target: false`; the new orchestration is implemented but not yet enabled in production manifests.
- Reconciliation still trusts broker state as the main source of truth, so transient broker-lag around just-submitted target orders can still be hardened further.
- Partial-fill-aware target/stop restoration is still a next step.

Next steps:

1. Decide whether to enable the new target orchestration for any deployment or keep Day 1 as stop-plus-algorithmic-exit only.
2. Add replay coverage for target activation, target fill, pullback restore, and restart recovery with a target already live.
3. Harden reconciliation so recently submitted virtual-target transitions survive broker reporting lag more gracefully.

### 2026-03-30 (Virtual Target Replay Coverage)

Completed:

- Added deterministic lifecycle replay tests for Public virtual-target behavior.
- Covered the bar-by-bar sequence of:
  preserve stop before target approach,
  activate target as price approaches,
  restore stop after pullback,
  and restart recovery when a live target is already on the broker.
- Verified that a reconciled target order does not get duplicated after restart.

Verification:

- `35` tests passing.
- `python3 -m compileall src tests` passes cleanly.

Open items:

- Target-fill replay is still indirect because the broker fill itself remains mocked at the order-manager boundary.
- Production manifests still keep virtual-target orchestration disabled until we explicitly choose to enable it.

Next steps:

1. Decide whether to enable the new target policy for `QQQ`, `SPY`, or neither.
2. If enabling, start with one deployment and conservative quantities.
3. Add a session-summary/report command so live runs make target/stop transitions easier to audit.

### 2026-03-30 (Execution Hardening And Minimal Lifecycle Store)

Completed:

- Added a minimal `TradeLifecycleStore` in `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/state/lifecycle.py`.
- Wired the execution supervisor to block duplicate entries when a deployment is already in an active lifecycle state.
- Synced lifecycle state from broker-reconciled positions so restart behavior is explicit rather than inferred only from position counts.
- Added exit-side increment correction in `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/execution/order_manager.py` so stop and target orders can snap to broker-supported increments using cached or preflight-derived tick sizes.
- Added tests for lifecycle gating, reconciliation-to-lifecycle sync, and exit-side price snapping.

Verification:

- `39` tests passing.
- `python3 -m compileall src tests` passes cleanly.

Open items:

- The true `DataIngestionDaemon` and runtime-owned heartbeat loop are still not implemented; the current polling loop remains CLI-driven.
- Lifecycle is intentionally minimal and does not yet publish transitions onto an internal event bus.

Next steps:

1. Build the in-memory `EventBus`.
2. Promote the current polling loop into a runtime-owned `DataIngestionDaemon`.
3. Publish `BarClosedEvent` and lifecycle transition events from the daemon/runtime path.

### 2026-03-30 (Runtime Event Bus And Heartbeat Daemon)

Completed:

- Added an in-memory event bus in `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/app/event_bus.py`.
- Added a runtime-owned `DataIngestionDaemon` in `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/market_data/daemon.py`.
- Extended the bar-source interface so providers can fetch the latest completed 1-minute bar directly.
- Upgraded `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/app/runtime.py` so the runtime now owns the trading session loop:
  warm start,
  reconciliation,
  heartbeat-driven `BarClosedEvent` publication,
  and bar-by-bar execution handling.
- Reduced `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/tools/dry_run_live_loop.py` to a thin wrapper over the runtime-owned session path.
- Added direct tests for the event bus and heartbeat daemon.

Verification:

- `42` tests passing.
- `python3 -m compileall src tests` passes cleanly.

Open items:

- `BhikshaRuntime.start()` still acts as a simple started-flag boundary; the full session entrypoint currently lives in `run_session(...)`.
- Lifecycle transitions are now runtime-managed, but are not yet emitted as their own typed domain events.
- The daemon currently fetches bars on the exact heartbeat and publishes in-process only; external bus support is still future work.

Next steps:

1. Emit typed lifecycle transition events onto the event bus.
2. Add session-summary and operator-audit reporting on top of the event stream.
3. Decide whether to collapse `run_session(...)` into a richer async `start(...)` API or keep the current split.
