# Bhiksha Entry and Exit Refactor Architecture

**Status:** Source refactor candidate; not deployed · **Date:** 2026-09-19  
**Scope:** Bhiksha entry selection, exit management/comparison, and Public order execution.  
This is the canonical architecture for this refactor and supersedes the parallel Codex discussion draft. It does not authorize deployment, live-policy changes, or new trading vehicles.

## 1. Outcome and boundaries

The operator selects one exit to manage a position and any alternatives to compare. Bhiksha applies those choices consistently across strategy, manual, and chart-originated entries; records why signals do or do not become trades; and uses broker-native orders when they preserve the selected policy.

Extend the existing compiler, executor, recorder, and reports. Retain Google Sheets as the configuration surface, Bhiksha as the execution/evidence owner, and TradeLab as the read-only analysis consumer. PAT V3 and Kamandal retain their own positions and execution authority.

The refactor has three deliverables:

1. Sheet-defined exit management and comparison, including paper positions.
2. Bounded DTE fallback and liquidity-sensitive entry pricing.
3. A safe Public route boundary, with native linked-order activation held until its lifecycle is proved.

No operator experiment IDs, manual version management, separate experiment registry, new scheduler, or additional trading service. Existing trade/order IDs and automatically saved settings provide history and retry safety.

## 2. Operator Sheet contract

### Entry rows

Add the same two columns to active_strategy, manual_entry, and Bhiksha's existing chart-scenario input:

| Column | Required | Meaning |
|---|---|---|
| management_exit | Yes for migrated rows | One exit_profile_id controlling the live or reference paper position |
| compare_exits | No | Comma-separated exit_profile_id values evaluated without order authority |
| strategy_class | Optional | Entry setup class for reporting; absent this, use the existing strategy key, never infer class from the selected exit |

Cartographer carries suggested exit references through the existing Sheet handoff. It does not create live authorization. Preserve each adapter's existing mode, budget, and enablement fields.

Trim whitespace and deduplicate comparison names; the compiled list is authoritative. Unknown names or unsupported settings invalidate the affected new row. A nonempty comparison list automatically includes management_exit as its baseline. A blank list disables comparison.

Resolve the deployed chart tab and schema through the existing adapter during implementation. Do not create a second chart input merely because historical documents use different names.

### Exit_Profiles_v1

Create one catalog tab with one row per named exit configuration. Use the following logical fields; preserve existing internal field names through the compiler mapping where possible.

| Fields | Contract |
|---|---|
| exit_profile_id, description | Unique stable name and readable explanation |
| exit_family | Implemented evaluator family; no arbitrary code or natural-language execution |
| initial_stop_pct, disaster_stop_pct | Decimal fractions of option entry premium where applicable |
| trade_archetype | Advisory intended setup for the exit configuration; not the entry classification |
| target_1_r, target_1_quantity, target_2_r | Targets in R and fraction of original filled quantity to close |
| breakeven_after_t1 | Takes effect after the T1 fill fact, not its trigger |
| giveback_arm_r, giveback_retrace_fraction | Explicit activation and allowed retracement |
| risk_envelope_enabled and applicable envelope parameters | Map the complete implemented envelope configuration, including activation and floor settings |
| no_progress_seconds, max_hold_seconds | Time/progress exits in seconds; optional maximum hold may be blank |
| giveback_policy | OFF, STRICT, MODERATE, or LOOSE; non-OFF also requires explicit arm/retrace values |
| eod_flat, hard_flat_time_et | Session handling; cutoff uses America/New_York including daylight saving |

Required mechanical cells cannot be blank. Optional inactive features may be blank. The compiler validates required parameters for the selected family and exposes the resolved values. Where friendly presets exist, expand them into explicit values once; do not maintain conflicting preset and numeric authority.

The catalog may define any number of supported configurations. New parameter variants require a Sheet edit; new executable mechanics require code. Do not hardcode four or six comparison arms.

The supported named manager is the existing intraday staged-R ladder, including its time-fuse and giveback parameters. Dynamic-envelope profiles are comparison-only on this new path; existing explicitly authorized live canaries retain their separate authority. Structural stops, overnight managers, and a separately claimed ratchet family fail validation until implemented.

Strategy class is an entry property, not an exit algorithm. Use the existing strategy key/setup metadata for grouping. Manual/chart rows need a setup label only when their current metadata cannot supply one. Compatibility checks enforce available data, quantity, instrument, and holding-period support; they do not declare which exit is profitable.

Initial catalog values must reproduce existing effective settings. No new stop percentages, holding periods, OI floors, or DTE ranges are introduced by this document.

## 3. Compilation and settings history

All entry adapters resolve into active_plan.json through the existing compiler:

1. Resolve management_exit and compare_exits against the same Sheet snapshot.
2. Validate parameter types, supported mechanics, data requirements, entry/exit-time coherence, and holding-period support.
3. Emit the resolved management policy and comparison list with source row identity.
4. Display compilation errors and the effective settings in existing status surfaces.

Runtime reads the compiled plan, not Sheet cells independently. Publish a validated plan atomically. Preserve the last valid plan only within its existing validity and authorization limits; never use it indefinitely through a failed sync. Invalid new configuration does not abandon management of open positions.

Save resolved exit, entry-pricing, and vehicle settings automatically with each admitted trade. Save actual contract and fill facts as they arrive. New Sheet edits affect new entries; pending orders and open positions retain their saved configuration. Intentional changes to an open live position remain explicit operator actions.

Reuse existing snapshot storage and trade IDs. Internal hashes may identify material settings changes, but are not user-managed versions. Group comparisons by relevant settings and measurement method, excluding incidental IDs and timestamps. Existing export fields can reuse Sheet row identity; no separate authoring workflow is required.

During migration, a row uses either its legacy exit configuration or its catalog assignment. Conflicting dual authority fails validation. Remove obsolete overrides after cutover.

## 4. Entry selection and pricing

### Signal accounting

Persist one outcome for every evaluated positive signal using existing event storage. Distinguish pending execution, filled, no-fill, existing-position block, risk block, budget block, selection failure, and expired/invalidated signal. Record attempted contracts and rejection reasons.

Throttle operator alerts without dropping underlying outcome facts. Compilation suppression and market-data gaps are separate coverage counters: recorded signals alone cannot prove complete market observation.

### DTE fallback

At selection time, use the actual listed expiration calendar:

1. Search the configured preferred DTE band.
2. If no eligible contract is found, search the explicitly allowed fallback expirations in deterministic order.
3. Apply the same hard eligibility, risk, and pricing checks to each candidate.
4. Recalculate affordable quantity from the actual premium and maximum permitted entry price.

Keep preferred DTE, maximum extension, delta, and budget settings in existing defaults with explicit row/symbol overrides. Distinguish no listed expiration from rejection for delta, liquidity, or cost. Record preferred and actual DTE, fallback reason, and attempted expiration counts.

Do not hardcode weekly/monthly availability by ticker. Fallback does not implicitly relax delta, OI, or spread limits and cannot change the contract of an already-working order. It must remain compatible with the selected holding horizon.

### Liquidity-sensitive offers

Separate preferred liquidity thresholds from hard admissibility limits. Add an explicit price-seeking mode using the existing pricing configuration:

| Condition | Behavior |
|---|---|
| Normal valid liquidity | Existing configured entry pricing |
| OI below preferred threshold or spread above preferred threshold, within hard limits | Require a configured price improvement |
| Missing/stale/crossed/nonfinite quote, extreme width, invalid contract, or failed risk/protection check | Reject or wait for valid evidence |

Unknown OI and observed zero OI remain distinct. Reuse Kamandal's bounded improvement approach where applicable; do not copy its strategy-specific thresholds or create a new shared service.

For a long-option buy, midpoint M = (bid + ask) / 2 and initial limit = M − discount. Discount is a bounded function of spread and liquidity settings. Round to a valid instrument tick without exceeding the permitted debit. A below-mid buy is price-seeking and may not fill.

Freeze contract, quantity, maximum debit, and expiry before submission. Price-seeking uses a fixed resting limit in this release; its retry path cannot raise that price. Existing named entry ladders remain available for ordinary pricing. Reserve budget for the maximum allowed debit. Do not erase a required liquidity discount during retries. Cancel when the signal invalidates or the time allowance expires; reconcile partial or uncertain fills before another order.

Keep existing numeric limits until explicitly changed. Do not convert a rejected long option into a spread automatically.

### Paper fills

Use the same selection and pricing rules in live and shadow mode. A selected limit price is an offer, not a fill.

A minimal conservative paper buy model requires a later fresh valid ask at or below the submitted limit; fill at the limit and record the observation time and model. Apply the configured order lifetime. If the condition never occurs, record no-fill. This is simulated execution, not proof of queue position or size.

Historical assumed fills remain labeled under their old model. Do not rewrite them or pool them invisibly with the new model. No order-book simulator is required.

## 5. Exit management and comparison

### One management authority

Exactly one resolved policy controls normal exits. Eliminate native/profile “first to act” competition. Account emergency controls and broker fills remain explicit overrides owned by the same executor.

A profile-owned ladder retains exclusive normal profit-taking authority; no competing full-position target. Protective orders remain broker-resident where supported by the current authorized execution route.

Define quantity rounding, one-contract behavior, stop/target collision ordering, and fixed-time exits in the evaluator. A policy requiring a runner must reject an incompatible quantity or use an explicitly supported one-contract behavior. Stop ratchets never loosen protection. New entries cannot be admitted at or after their authoritative fixed exit cutoff.

### Comparison lifecycle

Register every broker fill and modeled paper fill whose row requests comparisons. Use the same entry contract, price, timestamp, quantity, and history for all alternatives, including a modeled baseline for management_exit. Keep actual broker outcomes separate from simulated baseline outcomes.

Partial broker entry fills must seed all alternatives identically through the proved fill sequence; never assume the unfilled requested quantity. Retain independent remaining quantity, peak, stop, and terminal state per alternative.

Use existing trade IDs plus exit references/saved settings. Comparison evaluators have no broker order access.

Share validated observations through the existing recorder. Option-price policies consume option quotes; structural/timeframe policies also require the corresponding timestamped underlying observations. Never substitute future-confirmed structure into past decisions.

Continue observations after the managed position closes until all alternatives terminate or their saved observation horizon expires. Reuse existing bounded continuation, storage, and provider budgets; do not create one poller per arm. Polling cadence is part of the measurement and does not guarantee capture of between-poll touches.

Missing quotes, dropped records, restart gaps, or unavailable required features produce explicit incomplete/censored outcomes. Resume after restart only when continuity can be established. Unsupported overnight collection invalidates an overnight comparison; it must not silently become an intraday exit.

Model long-option exits with later valid executable bid observations and configured latency. Current named-comparison P&L is gross premium P&L: fees and additional slippage must be applied before an economic decision. Do not infer favorable target/stop ordering from ambiguous bars. Freeze a common entry-risk denominator for paired R comparisons.

### Reporting

Extend existing daily/weekly reports:

- Intended comparison trades, registered trades, completed comparisons, and missing counts by row, strategy class, and mode.
- Paired gross dollars and common-risk R are available now; net costs, drawdown, giveback, holding time, and premature-exit measures are required for a final economic review where supported.
- Actual fills versus simulated fills; fill model and material settings change dates.
- Selection/no-fill outcomes and their reasons, separate from trading P&L.

Alert when intended new comparison trades have no registrations or when expected observations stop. Worker liveness alone is insufficient.

Do not present selected complete cases as a proven winner while hiding missing data. Keep older collection failures visible and evaluate later compatible periods separately. Economic selection considers independent trading days, costs, uncertainty, and prospective confirmation. Age alone is not readiness; no arbitrary universal sample threshold is imposed here.

## 6. Public broker execution routes

Use a small route decision inside the existing executor. The chosen route is saved per trade and visible in status.

| Entry and exit behavior | Route |
|---|---|
| Fixed entry with fixed full-position target and stop | Native bracket after required lifecycle verification |
| Entry with attached stop only | OTO only after exact option behavior is verified |
| Entry repricing ladder | Existing SIMPLE entry and broker protection |
| Partial targets, dynamic runner, structural or time exits | Application-managed exit with broker protection |
| Explicit future spread strategy | Separate package execution implementation; outside this refactor's initial release |

Public added BRACKET/OCO/OTO classes on September 10, 2026. Its order endpoint describes entry-triggered exits. Bracket entry orders cannot be replaced; closing legs permit price changes but require unchanged quantity, type, and expiration. These restrictions prevent blanket bracket adoption for repricing entries and staged exits. [Changelog](https://public.com/api/docs/changelog), [order placement](https://public.com/api/docs/resources/order-placement/place-order), [replacement](https://public.com/api/docs/resources/order-placement/replace-order).

Native routing must preserve policy semantics. Do not replace a staged exit with a full-position target or discard a repricing ladder to fit brackets. Fill-relative stops require a valid provisional level and proved post-fill adjustment; otherwise retain the existing fill-derived route.

### Native-route release requirements

Verify the following before enabling that route:

- Child activation and quantity on partial entry fills.
- Child discovery and restart recovery through group and order IDs.
- Sibling cancellation and remaining quantities after partial/full fills.
- Parent/child cancellation, external closes, and emergency-exit behavior.
- Recovery when entry fills but child creation fails.
- Required option stop semantics and any claimed post-fill OCO attachment.

The inspected documentation does not settle all these details. Resolve through official clarification or a supported test facility; any production verification needs explicit authorization. Entry preflight does not prove bracket lifecycle behavior. [Bracket guide](https://public.com/api/docs/templates/place-bracket-order).

Persist bracket/group and child IDs beside existing order records. Reconcile uncertain results before resubmission. Cancellation acknowledgements are asynchronous, not proof that exposure is gone. Do not run native and application protection writers concurrently. [Cancellation](https://public.com/api/docs/resources/order-placement/cancel-order).

Remove redundant application stop/target coordination only for routes whose native behavior has been proven equivalent. Preserve existing recovery for other routes.

### Multi-leg boundary

Public's separate multi-leg endpoint supports package orders but its displayed schema does not establish bracket support; its prose specifies LIMIT orders. Do not assume single-leg OCO semantics apply to spreads. [Multi-leg endpoint](https://public.com/api/docs/resources/order-placement/place-multileg-order).

Multi-leg trading is not required to repair current entry conversion or exit comparison. A future explicit spread strategy requires package quantities, ratios, debit/credit pricing, combined risk, package quotes, and closing behavior. Reuse Kamandal's applicable conventions then; do not duplicate its strategy engine or model legs as unrelated orders.

## 7. Implementation map

| Existing area | Refactor |
|---|---|
| integrations/google_sheets.py and existing input adapters | Read catalog and two exit columns; preserve source identity and operator mode |
| active_plan/compiler.py | Resolve/validate exits, entry windows, and settings; publish one effective configuration |
| options/selectors.py and execution/pricing.py | Bounded expiration fallback; preferred versus hard liquidity limits; priced offers |
| execution/supervisor.py and profile_exit modules | One management authority; shared live/paper comparison registration |
| ops/exit_edge_live.py and ops/exit_edge_lab.py | Configurable alternatives, saved settings, independent state, required observations, continuation |
| execution/order_manager.py and execution/brokers/public/ | Capability-aware native payloads, group/child readback, route-specific amendments |
| state/reconciliation.py and existing trade/order storage | Persist and recover ownership, fills, and native group IDs |
| ops/exit_edge_weekly.py and existing reports | Complete coverage and comparable economic outcomes |

These are changes to existing components, not mandates to create new layers. Extract small helpers only where needed to remove duplicate behavior.

## 8. Delivery and acceptance

### A. Exit configuration and collection

1. Populate Exit_Profiles_v1 from current effective settings and map migrated rows.
2. Validate assignments from every supported input; preserve legacy behavior for unmigrated rows.
3. Save resolved settings and register live/paper fills.
4. Complete independent comparison state, continuation, and reporting.

Acceptance: a configured natural shadow fill produces the requested alternatives without an operator experiment ID; reference closure does not stop the remaining comparisons; settings edits do not change open trades. Unsupported/incomplete cases remain visible.

### B. Entry conversion

1. Add complete signal/selection outcomes.
2. Implement deterministic listed-expiration fallback and fresh sizing.
3. Add explicit bounded price-seeking behavior and the conservative paper fill rule.

Acceptance: test preferred-band absence, in-band rejection, fallback cost expansion, below-mid no-fill, order expiry, partial fills, and stale quotes. Each signal has a traceable disposition. More fills are evaluated alongside economics, not treated as success by themselves.

### C. Native execution

1. Add native group representation, payload translation, and readback.
2. Complete the lifecycle requirements in section 6.
3. Enable only the simplest compatible route through an explicitly authorized release.

Acceptance: a compatible fixed-exit position has confirmed protection, correctly reconciled sibling orders, and restart recovery without duplicate orders. Dynamic/staged policies retain their correct existing route.

Deliver A and B without waiting for native-order availability. Where paper fill behavior changes, establish the new model before admitting those trades to the new comparison results. Retain old evidence under its original label.

Use focused tests for these behaviors plus required repository release checks. Deployment requires explicit authorization, green tests, and oldmac readback. Preserve existing open trades during cutover; do not retrofit them into native groups.

## 9. Evidence and non-goals

The prior review found that live-only registration excluded paper entries from comparison. Existing continuation already exists but has incomplete historical evidence. Neither collected age nor the reported +$27.74 selected-subset result establishes an exit winner.

This specification does not promise that all signals trade, prescribe new trading thresholds, guarantee fills/protection, or infer expiration schedules from symbol categories. It does not introduce overnight support, spread strategies, or another experiment framework.

References:

- [Prior signal/exit review](../artifacts/audits/2026-09-19-signal-exit-review/REVIEW.md)
- [Existing Exit Edge Lab](EXIT_EDGE_LAB.md)
- [Entry-window and exit-authority lesson](lessons/entry-window-and-exit-authority-coherence.md)
- TradeLab: docs/EXPERIMENT_SYSTEM_SIMPLIFICATION_REFACTOR.md
- Kamandal: src/kamandal_v2/liquidity.py, src/kamandal_v2/live/pricing.py, and the corresponding entry-pricing/shadow-liquidity lessons

Public capability statements above were checked against official documentation on September 19, 2026. Account-specific native execution behavior remains a release verification item.

## 10. Rail B: reset the evidence window, preserve the ledger

Rail B protects live capital. It measures the last configured N **priced, closed live trades** per deployment, including all partial exits. Paper winners never erase live losses or grant live authorization. Keep the existing default window/minimum of 10; changing either is a separate operator choice.

Use one timezone-aware `rail_b_reset_at` value in Operator_Defaults_v1 (`demote_reset_at` remains a compatibility alias). This is an evidence-start timestamp, not a mutable loss counter. Only live trades **entered at or after** that timestamp count. An older position closing later does not enter the fresh sample. Conflicting aliases or malformed/naive timestamps fail configuration rather than silently resetting risk.

At reset, the qualifying count is 0. It increases only when a new eligible live trade closes with complete economics. Below the configured minimum, Rail B reports insufficient evidence; Rail A, sizing, position limits, and Sheet authorization still apply. A reset therefore permits fresh live risk when the other gates allow it and belongs in the explicitly approved runtime cutover.

When Rail B blocks, retain the live `risk_block` receipt and optionally collect a distinct paper observation through the existing lane. The paper lifecycle and comparisons remain labeled shadow. The block stays latched for that session, with its actual count/mean retained in status. The next session recalculates the same live evidence; restart alone does not heal it. Reopening a persistently losing lane requires an explicit reset or a changed operator decision, not simulated profits.

Do not delete trades, rewrite P&L, demote Sheet rows automatically, set the minimum sample to zero, or introduce probation/promotion state machines. The source supports this reset; this review has not written a runtime cutoff or reset live risk state.

## 11. Takeover decision and implementation plan

**Decision: retain and refactor.** Keep the compiler integration, event receipts, and existing evaluator/tape. Replace the unsafe behavior identified in review:

| Handoff defect | Source change |
|---|---|
| Missing Sheet catalog silently seeded invented profiles | Strict Sheet-owned catalog; lazy read for migrated rows; read/validation failures stop compilation |
| Different family names all compiled to one ladder | Explicit supported mechanics; unsupported families rejected |
| Row overrides changed management after hashing | One named authority; conflicting legacy overrides rejected; canonical management snapshot is the comparison baseline |
| Paper limit treated as a fill immediately | Pending limit on the existing monitor loop; later proved fresh ask required; fixed limit and bounded lifetime; no registration on no-fill |
| Recorder ignored compare_exits and always ran six fixed arms | Named frozen policies replay independently on the same tape; historical six-arm cohorts retain their original protocol |
| Native order submission lacked child reconciliation | Remove submission shortcuts; BRACKET/OTO flags fail closed before broker work |
| Rail B included paper profits and changed default N to 20 | Live-only economics, original defaults, explicit fresh-entry cutoff |

The compiled `management_exit` enables the existing profile manager consistently for authorized live rows and reference paper positions. Application-owned profit taking excludes a competing full-position target. Named paper positions retain their entry-time configuration during the session. Comparison snapshots are persisted in the existing SQLite cohort record; restart gaps stay censored.

Named-comparison summaries are included in the existing lab report and weekly evidence, grouped by entry strategy class/key, deployment, actual fill kind, and frozen configuration. They show registrations, complete/incomplete counts, independent session-symbol clusters, paired gross P&L and common R. They are descriptive evidence, not automatic promotion or a claim that the sample is sufficient.

### Release sequence

1. **Local source validation:** regression checks for strict compilation, independent comparison completion, paper no-fill/fill/expiry, bounded DTE fallback, price limits, and live-only Rail B. Run the repository suite. Preserve unrelated work.
2. **Operator migration preview:** export actual effective settings into explicit catalog rows; review strategy/manual/Cartographer assignments. Named Cartographer rows must clear the legacy management-spec cell while preserving the existing entry/budget/invalidation provenance. No synthetic preset migration.
3. **Authorized oldmac cutover:** deploy the reviewed source, publish the validated Sheet/plan changes, and apply the chosen Rail B timestamp once. Confirm count 0, unchanged history, enabled risk gates, and the actual deployed revision. No runtime action is authorized merely by this document.
4. **Natural proof:** verify a real modeled fill registers every requested policy; observe continuation after baseline close; inspect pending/no-fill and missing-data receipts and the weekly artifact. A passing test is not deployed collection proof.
5. **Native groups, separately:** obtain broker lifecycle evidence, implement group/child persistence and reconciliation, then pass section 6 before any route activation. Multi-leg trading remains a separate explicit vehicle change.

Known measurement limits: paper fills assume executable size at the quoted ask and exits at a later bid; there is no queue or depth model. Legacy unmigrated paper lanes retain their old assumed-fill behavior and do not enter the new comparison sample. Pending paper offers are session-local, with durable pending/terminal events; an abrupt restart can leave an unresolved pending receipt and must not be imputed as a fill. Current reports require a separate cost/uncertainty review before selecting an exit winner.

### Verification receipt (2026-09-19)

- Full repository suite: **1,259 passed**, including the localhost HTTP test after permitting its local socket bind.
- Additional independent-envelope and Cartographer migration cases: **8 focused refactor tests passed** (includes the six cases present in the full-suite run plus two new cases).
- `git diff --check`: clean.
- No Sheet write, deployment, live order, or runtime Rail B reset was performed. Source tests establish implementation behavior, not natural production collection.
