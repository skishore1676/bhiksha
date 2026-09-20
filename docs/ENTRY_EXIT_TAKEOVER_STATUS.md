# Entry/exit takeover — September 20, 2026

Decision: take over commit `66a669b`, preserve its useful work, and correct the
remaining semantics and operator wiring. Suman authorized production cutover.

| Point | Review finding | Implemented decision | Acceptance |
|---|---|---|---|
| 1. Compare exits | Confirmed/model-fill cohorts and independent arms existed; floor families did not share manager semantics | Keep collection; use one named evaluator; preserve old evidence and censor incompatible historical floor definitions | Persistence, post-primary continuation, actual/model separation, incomplete tape and family regressions |
| 2. Exit profiles | Loader silently supplied envelope fields; ratchet name had no distinct floor | Explicit six new numeric cells; real profit-lock/envelope evaluation; strict booleans/anchors; immutable canonical policies | Real workbook compile plus behavior tests |
| 3. Entry sources | Actual manual header order broke strict projector; named chart defaults were not projected | Map by header name; append primary/compare columns; copy defaults on new hypotheses and preserve existing selections on retry | Projector-to-compiler test with reordered real headers; workbook readback |
| 4. Rail B | 20/10 and UTC cutoff already in Sheet; global history truncation could hide live trades | Lane-specific closed history; retain P&L; add explicit opt-in reduced-risk recovery from fresh cost-adjusted, complete shadow observations | Live-only/reset regressions; stale/changed/missing/censored evidence rejection; cap and authorization tests |
| 5. Public native orders | Native flags were blocked, not a completed group lifecycle | Keep broker protection plus application-owned staged exits; do not enable native groups for these six policies | Existing flags fail closed; official API lifecycle restrictions reviewed |

## Production facts before this takeover

Oldmac Git HEAD was `a327e220`, with the previous agent's candidate copied as
uncommitted working-tree changes. Its September 20 plan already contained 28
named-exit deployments. Thus HEAD alone did not describe the production source.
The takeover preserves unrelated changes and records exact file fingerprints.

The real workbook contains 35 enabled strategy rows: 28 compile, seven remain
suppressed by existing research KILL gates. There is no unexplained compile loss.
The four manual rows are disabled. Existing active-row primary selections are
29 trend-continuation and seven flash-reversal profiles, including the disabled row.

The operator's existing Rail B settings are retained: lookback 20, minimum 10,
mean floor $0, evidence cutoff September 19 at 00:00 UTC. No P&L is deleted.
Automatic recovery remains unconfigured/off. Native flags remain off.

## Cutover evidence

Pending final oldmac test, atomic Sheet migration, plan publication, and readback.
This section is replaced with the completed receipt after those actions.

## Limits

- A successful deployment/compile does not establish that the new six-profile
  experiment has accumulated weeks of usable observations. Historical labels
  lacking the implemented floor semantics are not reinterpreted as new evidence.
- Recovery is a bounded trial rule, not a claim of statistical alpha. To opt in,
  supply the full per-lane object documented in the architecture, including a
  conservative cost allowance. No research-only lane gains live authority.
- Native OCO/OTO is not activated. Proper child discovery, protection, cancellation,
  and restart adoption remain required before enabling a suitable fixed-exit use
  case. No native order was submitted during this cutover.
