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

### 2026-03-30 (Lifecycle Events And Session Summary)

Completed:

- Added typed `TradeLifecycleTransitionEvent` publication on lifecycle state changes.
- Persisted lifecycle transitions into the SQLite event log as `lifecycle_transition` events.
- Wired the runtime-owned supervisor to publish those lifecycle events on the shared event bus.
- Added session-summary helpers in `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/ops/summary.py`.
- Added an operator-facing summary command in `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/tools/session_summary.py`.
- Added tests for lifecycle event publication and session summary aggregation.

Verification:

- `44` tests passing.
- `python3 -m compileall src tests` passes cleanly.

Open items:

- Session summary currently reads from persisted SQLite events, not directly from a live streaming subscriber.
- Lifecycle transitions are now typed events, but signal and exit evaluation events are not yet consumed by any reporting pipeline.

Next steps:

1. Decide whether to emit signal and exit evaluation events into the same operator-summary layer.
2. Add richer per-deployment PnL / broker-action summaries once fill and portfolio snapshots are easier to normalize.
3. Consider promoting `run_session(...)` into the primary async runtime API and keeping CLI tools as thin wrappers only.

### 2026-03-30 (Signal And Exit Reporting Extension)

Completed:

- Published typed `SignalEvaluatedEvent` and `ExitEvaluatedEvent` events on the runtime event bus.
- Changed signal handling so signal evaluations are persisted even when lifecycle gating blocks entry.
- Extended the session summary to count positive signal evaluations and positive exit evaluations by deployment.
- Added richer recent-event details for `signal_decision` and `exit_decision` rows.
- Added tests for signal-event publication, exit-event publication, and signal/exit-aware session summaries.

Verification:

- `46` tests passing.
- `python3 -m compileall src tests` passes cleanly.

Open items:

- The session summary still focuses on decisions and lifecycle, not realized/unrealized PnL.
- Event-bus subscribers are still internal only; there is no external observer or dashboard process yet.

Next steps:

1. Add per-deployment broker-action summaries such as stop submissions, target activations, and square-offs.
2. Add optional fill and portfolio snapshots so session summaries can speak to realized outcomes, not just decisions.
3. Consider a lightweight TUI or HTML status page on top of the same summary/event model.

### 2026-03-30 (Per-Symbol Workers And Token Daemons)

Completed:

- Changed the runtime session path to dispatch `BarClosedEvent` objects onto per-symbol worker queues so symbol processing can proceed independently.
- Added an entry gate plus symbol-scoped lifecycle locks in the execution supervisor to reduce cross-symbol blocking while still protecting total-entry sequencing.
- Scoped hard-flat handling to the current symbol inside the worker path to avoid duplicate close attempts across parallel workers.
- Added background Public and Schwab token refresh daemons in `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/app/token_daemon.py`.
- Kept the existing lazy token-refresh path in place as an emergency fallback, while background refresh becomes the normal path.
- Added timing tests for both token daemons.

Verification:

- `48` tests passing.
- `python3 -m compileall src tests` passes cleanly.

Open items:

- Broker portfolio synchronization is still serialized, so a slow portfolio call can still delay worker progress even though order negotiation no longer has to block all symbols.
- Execution is not yet split into a dedicated broker-action queue, so the current concurrency model is improved but not the final actor-style design.

Next steps:

1. Add per-deployment broker-action summaries such as stop submissions, target activations, and square-offs.
2. Consider moving broker actions onto a dedicated execution queue if portfolio sync or order submission latency still proves material.
3. Add lightweight metrics around heartbeat lag, queue depth, and token refresh health.

### 2026-03-30 (Execution Dispatcher)

Completed:

- Added a per-symbol execution dispatcher in `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/app/execution_dispatcher.py`.
- Moved broker-affecting work for entry, exit, hard-flat, and open-position management off the bar-processing path and into dispatcher workers.
- Added per-symbol dedupe keys so repeated submissions do not stack while a previous action is queued or running.
- Added dispatcher recovery logging so a failed execution task does not kill the worker for that symbol.
- Added dispatcher tests for dedupe, symbol independence, and worker recovery.

Verification:

- `51` tests passing.
- `python3 -m compileall src tests` passes cleanly.

Open items:

- Broker portfolio synchronization is still serialized before evaluation, so the runtime is not yet fully actorized end to end.
- Dispatcher work is currently in-memory only; queue depth and task latency are not yet surfaced as operator metrics.
- Entry and exit evaluation still recompute Newton enrichment separately on the same bar; we should cache and reuse one enriched frame per symbol bar.

Next steps:

1. Reuse one enriched frame per symbol bar for both exit and entry evaluation.
2. Add queue-depth / heartbeat-lag / execution-latency metrics to the operator summary.
3. Reduce or compartmentalize broker portfolio sync so reconciliation is less of a shared choke point.

### 2026-03-30 (Durable Trade Identity And Reconciliation)

Completed:

- Added durable trade sessions in SQLite with local `trade_id` ownership.
- Threaded `trade_id` through `TradePlan`, `ExitPlan`, and `TrackedPosition`.
- Changed entry planning so the Public entry `orderId` is the same UUID as the Bhiksha `trade_id`.
- Updated the execution supervisor to persist trade-session state across pending entry, protected open, target-active, and closed transitions.
- Updated restart reconciliation to prefer durable known trade ownership over symbol-only mapping.
- Added ambiguity protection so same-contract multi-deployment ownership is skipped instead of being auto-attached incorrectly.
- Added tests covering durable known-trade reconciliation and ambiguous same-contract rejection.

Verification:

- `53` tests passing.
- `python3 -m compileall src tests` passes cleanly.

Open items:

- Existing older live positions that predate the durable trade journal still rely on the old symbol-level fallback until they are cycled through the new runtime.
- Public account positions are still aggregated by option symbol, so Bhiksha can support multiple strategies on the same underlying only if they do not converge onto the exact same option contract.
- We still do not persist a full historical order-lineage table; the current implementation stores the active entry/stop/target ownership chain for open trades.

Next steps:

1. Reduce or compartmentalize broker portfolio sync so reconciliation is less of a shared choke point.
2. Decide whether we want to hard-block new entries when another deployment already owns the same underlying, or only when it owns the same exact option contract.

### 2026-03-30 (Shared Enriched Frame Reuse)

Completed:

- Changed `ReplaySignalEvaluator` so the runtime can prepare enriched frames once per symbol bar and reuse them across both exit and entry evaluation.
- Grouped deployments by strategy key and required feature set so identical feature requests share one Newton enrichment pass.
- Updated `PositionMonitor` to consume pre-enriched frames when the runtime has already prepared them.
- Updated the runtime bar handler to prepare enriched frames once per symbol bar and feed both exit and entry paths from that shared map.
- Added a replay-evaluator test proving same-feature deployments share a single feature-enrichment call.

Verification:

- `54` tests passing.
- `python3 -m compileall src tests` passes cleanly.

Open items:

- The currently running live tmux session was started before this optimization, so the reduced Newton-pass behavior will only appear after the next restart.
- The optimization currently groups by strategy key and required-feature set, which is correct for the current plugin model but should be revisited if future strategies require feature enrichment with hidden non-feature-side configuration effects.

Next steps:

1. Reduce or compartmentalize broker portfolio sync so reconciliation is less of a shared choke point.
2. Decide whether we want to hard-block new entries when another deployment already owns the same underlying, or only when it owns the same exact option contract.

### 2026-03-30 (Runtime Metrics)

Completed:

- Added runtime metric events for heartbeat lag, portfolio sync latency, feature-prep latency, execution queue depth, execution pending count, execution wait latency, execution run latency, and total per-bar processing time.
- Extended the per-symbol execution dispatcher with queue-depth and pending-count accessors.
- Extended the operator session summary so it now reports latest and average runtime metrics.
- Added summary tests covering runtime metric aggregation.

Verification:

- `55` tests passing.
- `python3 -m compileall src tests` passes cleanly.

Open items:

- The currently running live tmux session was started before this metrics patch, so the new runtime metrics will not appear in `session_summary` until the next restart.
- Metrics are persisted as event rows, not yet exposed through a live rolling dashboard or alert threshold system.
- We still need to decide which latency thresholds should trigger an operator warning.

Next steps:

1. Reduce or compartmentalize broker portfolio sync so reconciliation is less of a shared choke point.
2. Decide whether we want to hard-block new entries when another deployment already owns the same underlying, or only when it owns the same exact option contract.
3. Add thresholded warnings or alert markers when heartbeat lag / sync latency / execution latency drift beyond acceptable bounds.

### 2026-03-30 (Exit Policy Notes)

Notes recorded for follow-up:

- Startup config visibility should improve with a compiled operator snapshot so the bot prints exactly what it sees after YAML merge and env resolution before trading begins.
- For option-based exits, `stop_to_breakeven_after_r_multiple` currently looks like the cleanest next protection step because it respects the option-premium stop model without forcing a noisy trailing-stop policy too early.
- A possible future policy ladder is:
  `pure_strategy`,
  `strategy_plus_breakeven_after_r`,
  `strategy_plus_virtual_target_then_trail`,
  `strategy_plus_underlying_trailing_exit`.
- We are not yet locking a global target policy because the right choice still depends on comparing realized Bhiksha outcomes to Mala’s holdout expectations.
