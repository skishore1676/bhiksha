# Cartographer weekly plans → Bhiksha execution

Intent and implementation contract, 2026-09-27. Deployment authorized by Suman;
initial execution mode is SHADOW. Cartographer owns plans, Bhiksha owns execution,
and the existing Google Sheet owns operator controls. No writes to producer Signals.

## Flow and authority

1. Read Cartographer's published weekly book and successful, hash-bound receipt.
   Do not consume drafts, prose-only levels, or the old daily deterministic batch.
2. Compile explicit directional what-if branches into frozen Bhiksha deployments.
   Keep publication, scenario, branch, trigger, tactical/structural invalidation,
   expiry and evidence identities. Never translate an unsupported condition into
   an easier trigger. Show unsupported scenarios with a reason.
3. Evaluate completed regular-session bars at the source confirmation timeframe.
   Support 5m, 39m and daily close_above/close_below, including consecutive-bar
   confirmation. Daily confirmations can enter only next session using fresh
   price/quotes; an expired or invalidated plan never catches up retrospectively.
   Prerequisite/retest and neutral/range branches remain visible but unsupported
   until their explicit state machine is implemented.
4. On a valid trigger, use existing option selection, bounded pricing, risk gates,
   shadow ask-touch fills, protection and named exits. OI/spread are price-seeking
   preferences using Sheet defaults, within hard quote, premium and risk safeguards. Preserve one filled trade
   per scenario per entry arm across sessions/restarts; sibling branches within an arm are mutually exclusive.
   Temporary price/quote failures may retry while the setup remains valid.
5. Primary management is the Sheet mapping from the author's management profile;
   completed-bar tactical invalidation also exits. Structural invalidation is
   retained for explanation. Broker/disaster protection always wins. Freeze policy
   after fill; subsequent publications must not rewrite open trades.
6. Keep collecting existing alternative-exit evidence after primary close. Publish
   scenario status and outcomes to Bhiksha-owned Cartographer_Status, and pass
   source lineage through the existing Chart Workbench evidence outbox.

## Operator surface

Operator_Defaults_v1 section cartographer_weekly: mode OFF/SHADOW (LIVE unsupported
until separately armed), max_contracts, max_trade_premium_usd, max_open_positions,
entry_execution_profile, max_entry_distance_pct, dte_min/dte_max,
dte_fallback_max, compare_exits, and four explicit management-profile mappings.
Reuse Exit_Profiles_v1. Default one contract, $400 premium, two concurrent positions per arm,
balanced entry, 1% maximum distance beyond trigger, 7–21 DTE with bounded 28 fallback.
Range-expansion stays a three-session comparison initially; multi-day primary
execution requires verified overnight position ownership/protection and is not
silently enabled by choosing a comparison profile.

Control edits become effective through the existing compile/reload lifecycle;
report the loaded mode and timestamp. OFF stops new admissions; existing positions
continue their frozen management. Do not advertise instantaneous cancellation
without a runtime refresh path. Existing scanners/manual entries remain separate.

Cartographer_Status is generated, not another control queue: source/scenario,
symbol/direction, trigger and confirmation, invalidation, valid-through, primary,
mode, state, last evaluation, entry outcome/reason and source identity. Preserve
immutable history in the existing ledger, not unlimited Sheet rows.

## Lifecycle rules

- Same publication: idempotent import. Use stable source identities, no user IDs.
- Revised admitted scenario: block new entries and surface the revision for explicit
  readmission; no revision grants a second fill. Filled scenarios retain frozen policy.
- Multiple branches: first fill consumes the scenario within that arm; no opposite-side sibling
  may be pending concurrently. No automatic re-entry after a completed fill.
- Gap beyond entry-distance limit: wait/expire, never chase. Before-entry tactical
  invalidation retires that branch; a pending sibling reservation releases only after cancellation. Expiry stops entries, not position management.
- Missing/failed source: admit nothing new; retain open-position management and
  report failure. Restart restores consumed/invalidated scenarios from ledger. Unfilled paper intents
  cancel on process loss; confirmed fills win reconciliation.
- Old active scenarios retain their stated validity when a new book arrives;
  do not erase an unexpired plan solely because it is absent from the latest book.

## Experiment and retirement decision

Replace the existing Bhiksha morning projection owner with the weekly importer;
retire its old daily Manual Entry admissions and duplicate control/challenger test.
Preserve historical receipts. Keep the independent daily research producer and
producer-owned Sheet projection unchanged: they are outside this execution cutover,
and their downstream retirement has not been established. No second scheduler.

The replacement experiment is useful directly: author's weekly setups, prospectively
executed in SHADOW, segmented by setup type and confirmation timeframe, with
existing paired exit comparisons. Track proposals→supported→evaluated→triggered→
filled/blocked/expired and net/R results. Do not add another selector, optimizer or
A/B platform. No automatic live promotion.

## Acceptance

Tests: receipt integrity, unsupported conditions, completed bars/early closes,
no prepublication signals, next-session daily confirmation, expiry/invalidation,
restart/idempotency/sibling exclusion, risk/mode controls and unchanged scanners.
Deploy only after green tests; verify oldmac plan/owner and actual Sheet readback.
Workbench is read-only: publish plan plus execution provenance, never commands.
Natural session results remain pending until markets reopen; no synthetic fills
may be reported as runtime proof.

## Confirmation timing (operator clarification)

Preserve the author's explicit confirmation rule. A 39-minute close can delay entry
by up to almost 39 minutes; do not reinterpret it as an intraminute crossing.
Cartographer should specify analysis timeframe separately from entry confirmation
and tactical invalidation timeframe. A 39-minute thesis may deliberately use a
one-minute entry, but that must be authored explicitly and supported before use.

## Paired entry timing — implemented

`Operator_Defaults_v1 → cartographer_weekly → entry_timing_comparison` controls
OFF/PAIRED. PAIRED admits a second, shadow-only arm alongside the baseline:

| Arm | Entry confirmation |
| --- | --- |
| baseline | Exact author rule, timeframe and completed-bar count |
| early_1m | First completed regular-session one-minute close beyond the same price |

This is a close beyond the level, not an intraminute touch or a requirement to
observe a crossing from the other side. Each arm must still be beyond the level
at entry, within the distance limit, and satisfy the normal entry window, quote,
option selection, premium and bounded retry rules. A daily baseline enters on
its next eligible session; its early arm can enter sooner.

Admission of the early arm is timestamped once by the existing morning importer.
No bars starting before that admission count. Restart/import does not move the
boundary or grant another trade. OFF stops early entries on the next compile;
admitted rows and frozen open-position management remain. Baseline IDs stay intact.
Each arm has its own two-position capacity and scenario reservation, preventing
an early fill from suppressing baseline evidence. No live capacity is increased.

Keep the same author invalidation and primary/alternative exit policies in both
arms. Select an executable option independently at each entry time and retain its
contract, quote, fill, underlying price and timestamps in the existing ledger.
Arm and source identities travel into exit cohorts and Chart Workbench; exit
scorecards separate strategy classes by arm rather than pooling different entries.

Cartographer_Status shows both arms, including untriggered and blocked rows. Join
by publication/scenario/branch, not by ticker or matching option contract. Analyze
both-filled, early-only, baseline-only and neither-filled cases; missing execution
is not zero P&L. Compare timing/underlying movement separately from option results,
and explain no-fill reasons before interpreting profitability. Existing quote-gap
quality rules still govern clean exit comparisons. No automatic winner selection
or live promotion; natural market evidence begins after this deployment.

## Exit observer recovery

The independent quote-only owner has a 45-second deadline for the complete quote
request and a 180-second progress watchdog covering startup and synchronous work.
A timed-out request is recorded and retried on the next poll. A stalled owner or
stopped writer exits; its existing launchd KeepAlive restarts it. The watchdog
writes the last stage to `exit_edge_live_status.watchdog.json` before exiting.
No trading executor restart or order action is part of observer recovery.

Recovery resumes existing cohorts. Late first quotes and gaps between quotes stay
marked in the evidence and excluded from clean comparisons; no missing prices are
invented. Process existence alone is not acceptance: verify advancing status,
quote timestamps/counts, registration coverage and recorded gaps in the SQLite
store. A disabled marker still produces heartbeats.

The quote observer uses Standard process priority and normal I/O in the shared
launchd registry. It must not be background-throttled while collecting a
65-second-gap-bounded tape. Other jobs retain their own priority settings.
