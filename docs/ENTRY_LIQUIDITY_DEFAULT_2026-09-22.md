# Single-leg entry intent and implementation contract

Updated October 8, 2026. This supersedes the September 22 pricing algorithm and
original-opening-bid chase reference. The core policy was deployed October 8 at
ce4fd043; its tests and loaded-owner/Sheet readback are recorded in
`artifacts/observations/midpoint_entry_cutover_2026-10-08.json`. Natural broker
conversion remains a separate acceptance check. The comparison-registration
restart repair has its own receipt; the initial cutover receipt is preserved.

## Trader intent

An authorized, still-valid signal deserves a practical attempt to enter at a
reasonable price. Optimizing entry cost must not silently turn a momentum
strategy into a buy-only-on-a-pullback strategy. A favorable opening bid is an
execution choice, not the maximum acceptable price. A no-fill remains legitimate
when the market will not meet the permitted price before the opportunity ends.

## One small pricing policy

Use the existing eligible expiry/delta cohort and at most five candidates.
Within that set prefer a fresh, reasonably narrow market over a wide alternative;
retain deterministic delta/DTE ranking as the tie-breaker. Do not widen DTE or
change strategy direction to manufacture an entry. All candidates still need
positive OI, a valid two-sided quote and existing hard market-sanity checks.
The Sheet's preferred spread threshold separates normal from wide markets;
low OI alone is descriptive and does not demand an extra discount or veto.

Normal market: begin at midpoint. At the existing profile's first reprice
checkpoint, make at most one successful upward replacement toward the current
ask, clipped to the frozen maximum entry price. Retain the existing final
cancel deadline. Transient failures may retry the same step within that deadline;
they must not burn the step as though a replacement had succeeded.

Wide market: first seek a narrower eligible contract. If none is available,
start at bid + 25% of the spread; allow one move toward midpoint within the
frozen ceiling. This deliberately patient path may finish unfilled. Remove the
stacked nonlinear discount/OI-scaling formulas from this path. No per-ticker code.

Freeze the price ceiling from the first usable quote, before selecting quantity:
- Normal: original midpoint * (1 + existing Sheet/profile max-chase fraction).
- Wide: min(original midpoint + $0.10, the normal ceiling).
- Any explicit operator price ceiling is an additional upper bound.
The quote is a pricing reference, not an assertion of fair value. Neither later
quotes nor a retry reset this ceiling. A candidate contract gets its own quote
reference; no contract switch while an earlier order remains unresolved.

Every actual BUY price is capped at the current ask and the frozen ceiling,
then floored to the contract's valid broker tick. An explicit operator limit is
never silently converted into permission to pay more. A proposed step above the
ceiling is clipped to a valid permitted price, rather than discarded wholesale.

Size initially for the maximum permitted entry price, using existing premium,
settled-cash, prospective-loss and broker-cost limits. Preflight the actual
submitted price/quantity as well. Never increase quantity during repricing.
Capacity changes require fresh checks; if resizing a replacement is necessary,
first reconcile cancellation and all partial fills. An uncertain order outcome
retains its client identity and reservations; never send a second independent BUY.

## Recovery and sequencing

Reuse the existing supervisor, planner, order manager, lifecycle and ledgers.
One opportunity retains its original signal identity and original expiry.
The working deadline is the earliest applicable signal expiry, authorized entry
window end, primary exit boundary and existing order-patience deadline. An
explicit zero recovery setting remains disabled. No new scheduler or database.

Sequence: valid authorized signal -> eligible contract/current quote -> frozen
price ceiling and affordable quantity -> exact-price preflight/reservation ->
submit/reconcile -> one bounded reprice or temporary recovery -> terminal result.

Temporary quote/preflight failures are retryable within remaining time both
before initial submission and during repricing. Never replace using an invalid
quote. Whether an existing order can safely rest or must be cancelled depends
on current invalidation/risk/order state; cancellation does not itself authorize
resubmission until broker outcome is known. Terminal reasons are invalidation,
authorization withdrawal, exhausted window, no affordable contract, or a proved
unrecoverable failure. No unlimited bargain hunting.

Preserve current source-specific confirmation/invalidation rules: ordinary
completed-bar requalification, manual current-trigger checks, and Cartographer's
existing durable arm/author clock. Do not invent strategy-level validity rules
or require a new crossing merely as an execution retry. If these requirements
conflict for a strategy, report the concrete case rather than changing its alpha
semantics as part of this release. Cartographer signal generation is out of scope.

LIVE and SHADOW use the same pure price/ceiling/quantity/tick decisions and
recovery classification. Broker I/O remains LIVE-only. Shadow fills still need
a later fresh ask at/below the resting limit; no assumed midpoint fills. Obtain
instrument tick metadata through an existing read-only broker adapter, never
place a real shadow order. Missing metadata is explicit, not an invented tick.

## Operator surface and release

Keep the existing Sheet profiles, spread preference, chase fraction, premium
caps, recovery duration/spacing and authorization controls. The price ceiling
now references the original midpoint, not the discounted opening bid. Update
existing control descriptions and compiled readback together. Retire or clearly
mark superseded nonlinear-discount and second-reprice controls; never silently
ignore a populated operator override. Snapshot affected Sheet cells before any
scoped migration. Five LIVE authorizations and all capital/risk limits stay fixed.

Existing opportunity reporting should show starting quote, frozen ceiling,
quantity, initial/final limit, retries, broker/model fill basis and terminal
reason. Fix positive-decision double counting in evaluation totals if still
present. Keep historical evidence and the closed September 25 IWM exclusion.

Acceptance scenarios: (1) RBLX 0.35/0.89 cannot be trapped by a ceiling derived
from its opening bargain bid; (2) SMH 3.64 respects a 0.05 tick; (3) normal entry
works midpoint then at most one permitted move; (4) sizing funds that full move;
(5) transient initial/reprice failures recover without sliding deadlines;
(6) genuine price/risk/invalidation limits still stop; (7) cancel races, partial
fills and uncertain submissions cannot duplicate BUYs; (8) LIVE/shadow pricing
matches while their fill evidence remains distinct; (9) source confirmations,
zero-retry settings and restarts retain authority. Test the actual production
bootstrap path. Prefer composed trader scenarios over tests mirroring helpers.

One implementation owner delivers the cohesive change, green appropriate/full
suite, scoped oldmac deployment, loaded-owner and Sheet/plan readback. Record
local tests, deployed behavior and natural market proof separately. Use existing
scheduled checks for the next relevant sessions; no manufactured trades and no
daily policy tuning from one no-fill. Do not rewrite exits or rebuild Bhiksha.
