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

Completed at 2026-09-20 12:49 UTC (07:49 CT):

- Source release `339d792968ba0138619bbfba999730455cf85cc5`, branch
  `codex/entry-exit-takeover`; 18 deployed file fingerprints verified.
- Local **1,279 passed**; oldmac **1,279 passed** using temporary test tools,
  without changing the production virtual environment.
- Atomic Sheet migration applied and read back: six explicit profile parameters,
  named management/comparison columns for all four still-disabled manual rows,
  and named defaults for future Cartographer hypotheses. Existing strategy
  primary selections were retained. Added controls were visually checked.
- Published `active_plan_2026-09-20` through the normal atomic sync. All 28
  deployments (5 LIVE, 23 SHADOW) carry **six** comparison policies, a matching
  frozen primary, and the corrected mechanics version. Coverage is release-safe:
  35 enabled rows = 28 deployments + 7 existing research KILL suppressions.
- Effective Rail B: enabled, window **20**, minimum **10**, cutoff
  `2026-09-19T00:00:00Z`; **0** qualifying closed live trades for each live lane.
  The existing **571-trade** ledger was not reset or rewritten.
- Automatic recovery opted-in lanes: **0**. Native order enabled lanes: **0**.
- The installed `com.bhiksha.live-start` points to the correct runner. Its
  `artifacts/playbook/runtime_flags/exit_edge_live_shadow.enabled` marker exists
  and resolves the scheduled recorder flag to **true**. The plain app.yaml
  default is false; launcher context is required for a truthful readback.
- Experiment store: **43 historical cohorts; 0 corrected-mechanics cohorts**.
  Natural collection begins with qualifying future fills; no exit winner is
  established by this cutover. No live/test order or new session was started.

[Local production readback](../artifacts/audits/2026-09-20-entry-exit-takeover/production-readback.json).
Oldmac rollback and migration receipts:
`/Users/sunny/Documents/bhiksha/artifacts/releases/entry-exit-339d792/`.
This contains the source manifest, `preimage.tar.gz`, Sheet preimages/exact
requests, and `readback.json`. The underlying Git HEAD remains `a327e220` with
pre-existing working-tree changes preserved; the release manifest, not HEAD
alone, identifies the installed source.

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

## Recovery activation and follow-up review — 2026-09-20 13:09 UTC

User authorized recovery activation. The Sheet now owns the full recovery object
in `active_strategy.execution` for AMD, IWM, QQQ, SMH and NVDA (rows 2, 3, 5, 6, 7).
Both alias columns existed; an initial write to `execution_overrides` was masked
by the later `execution` column. Readback caught this before completion; the
policy was moved into `execution`, preserving the existing settings and restoring
the other column to its original empty value. No compiler/source change was needed.

Normal publication produced `active_plan_2026-09-20` at
`2026-09-20T13:09:08.504694+00:00`: 28 deployments, exactly five recovery opt-ins,
zero research-shadow opt-ins. Each requires 20 fresh modeled trades, five ET
sessions, a 14-day maximum age, session-weighted net mean >=0.10R and no negative
session mean, after a configured $2 per-contract round-trip allowance. Each probe
is at most one contract and 20% of the row premium cap: currently $400. No trade
or session was manually started. P&L and existing Rail B settings were preserved.

Oldmac receipts: `artifacts/releases/entry-exit-339d792/recovery-activation-20260920T130858Z.json`
contains the exact Sheet preimage/write/readback; `recovery-readback.json` verifies
the published effective settings. Earlier activation receipts record the masked
intermediate writes. Activation is verified configuration, not natural recovery proof.

Review of the two pasted agent answers:

- Do not broaden experiment admission to assumed fills. Named shadow entries
  already wait for a fresh later ask at or below the original limit; qualifying
  modeled fills register with the recorder. Historical assumed fills cannot
  establish executable exit performance or qualify recovery.
- A larger premium cap removes some quantity-zero cases, not every affordability
  or selection failure. No further budget increase is justified by these answers.
- The selector supports a bounded later-expiry walk, but Cartographer
  `profile_bundle` does not currently propagate `dte_fallback_max`. Merely adding
  that Sheet key will not activate the proposed behavior. A small tested plumbing
  change is the appropriate next implementation; expiry bounds remain Sheet-owned.
- A short retry for transient liquidity failures is worth implementing in the
  existing entry flow, with fresh trigger/invalidation/window checks, one pending
  attempt and a fixed expiry. Do not retry permanent budget failures or bypass
  hard quote guards. The claimed later MS spread tightening needs quote evidence.
- `enabled=FALSE` means disabled, not necessarily successful session completion.
  Read status/reason: entry errors can also disable rows.
- The pasted historical P&L totals and automatic Cartographer challenger-promotion
  claim were not independently established by this bounded configuration review.
  Neither establishes readiness of the corrected six-exit experiment.

No DTE expansion, new retry behavior, assumed-fill admission or native-order
activation was applied as part of this recovery configuration change.

## Entry follow-ups implemented — 2026-09-20

Implemented and deployed source `afbf0fa`, completed by restart-latch fix
`6863e32`. The existing Sheet controls the behavior in
`Operator_Defaults_v1!A55:E57`, section `profile__trend_continuation`:

- `dte_fallback_max=21` (preferred DTE remains 3–7).
- `entry_liquidity_retry_seconds=600` (0 turns it off).
- `entry_liquidity_retry_interval_seconds=60`.

The profile/projector/compiler now preserve these controls. Only otherwise
eligible contracts rejected on spread can start the bounded retry. Each attempt
requires a fresh underlying observation, a still-true trigger and valid thesis,
entry window, lifecycle and normal risk checks. The retry never extends its
original deadline. A final guard blocks submission if cancellation/expiry occurs
during selection or preflight, and releases cash/risk reservations. Consumed
retry intents are restored from the existing attempt ledger on restart, even if
the process reads a cached active plan. No new scheduler or database was added.

Validation: **1,300 tests passed locally and 1,300 on oldmac**.
Oldmac readback verified all 14 released file fingerprints. A zero-write synthetic
Cartographer projection using the **actual Sheet defaults and exit profiles**
compiled to the three controls above, the named primary, and six comparisons.
Normal plan publication at `2026-09-20T13:27:43.142551+00:00` retained 28 scanner
deployments and all five LIVE recovery opt-ins. All four historical manual rows
remain disabled; new Cartographer projections inherit the new controls. The Sheet
was visually verified at 100% zoom, with all new keys, values and descriptions
readable. There was no running trading session and no manual/test order submitted.

Release/rollback and Sheet preimage receipts live on oldmac under
`artifacts/releases/entry-followup-afbf0fa/`, including `restart-completion/`.
[Local readback](../artifacts/audits/2026-09-20-entry-followup/readback.json).
Natural next-session retry/fill outcomes and accumulation of decision-grade exit
comparison evidence remain to be observed; this release does not claim an exit winner.
