# Bhiksha entry and exit architecture

Updated 2026-09-20. This is the implementation contract, not a discussion draft.
Production cutover was explicitly authorized by Suman on September 20. See the
[cutover receipt](ENTRY_EXIT_TAKEOVER_STATUS.md) for verification and remaining limits.

## 1. One primary exit, independent comparisons

Every entry has one primary `management_exit`. That policy manages the position.
`compare_exits` is a comma-separated list of named alternatives from
`Exit_Profiles_v1`. It works for both LIVE and SHADOW entries. The compiler adds
the primary to the comparison set and removes duplicates. A blank comparison
list deliberately disables collection for that entry.

Each confirmed broker fill or fresh modeled ask-touch fill starts a cohort.
All arms share the contract, entry time, premium, quantity, and prospective
quote tape. Each arm has independent state, partial exits, and terminal status.
The quote collector continues after the primary exits until every arm is terminal
or the cohort is explicitly censored. Replays use a later fresh bid after the
configured latency; a touched threshold is not itself an executable fill.

Paper entries wait for a later fresh ask at or below their fixed entry limit.
An initial quote, stale quote, expired order, or assumed legacy fill is not
accepted as modeled-fill evidence. Unfilled paper entries are not cohorts.
Current pending paper orders are session-local; interrupted pending orders are
not reconstructed as historical fills. This is a collection limitation, not
permission to invent an execution.

Save the resolved policies and settings with the cohort and freeze the primary
policy with the trade. Existing trade IDs and internal configuration hashes are
sufficient. The operator does not create experiment IDs. Sheet edits apply to
new entries; open positions retain their frozen policy.

Reports separate actual and modeled entry fills and group by entry strategy
class/key, deployment, and frozen settings. Report complete pairs, incomplete or
censored pairs, independent session-symbol clusters, paired premium P&L, and
common-entry-risk R. Gross modeled profits alone do not establish an exit winner.
The sample must also survive cost assumptions, concentration, missing-data review,
and a prospective repeat. Time elapsed since requesting the experiment is not
its sample size.

## 2. Sheet-owned profile definitions

`Exit_Profiles_v1` owns both names and mechanical values. Unsupported mechanics,
unknown names, conflicting exit authorities, malformed booleans, and missing
family parameters fail compilation. The compiler never invents a catalog when a
Sheet read fails. A profile ID beginning with `#` is intentionally inactive.

| Family | Implemented behavior |
|---|---|
| `staged_r_ladder` | Premium stop, T1 partial, optional breakeven, T2 runner, giveback, time limits, hard flat |
| `time_fuse` | The same ladder with explicit no-progress/max-hold settings; no separate hidden algorithm |
| `profit_preservation_ratchet` | Ladder plus an explicit locked floor after a prior observed peak reaches the arm |
| `dynamic_envelope` | Ladder plus the quantized, non-loosening envelope derived from the prior observed peak |

Named management and named comparison replay use the same pure evaluator. The
legacy canary experiment retains its separate historical replay/authorization.
A named dynamic or profit-lock exit uses the existing application close path;
the protective stop remains at the broker. These virtual floors do not promise
broker-side execution while Bhiksha is unavailable.

Required common values: archetype, targets, T1 fraction, initial/disaster premium
stops, no-progress duration, giveback choice and applicable numeric settings,
breakeven choice, and same-day hard-flat time. Friendly Sheet columns
`target_1_fraction`, `no_progress_minutes`, and `max_hold_minutes` map to canonical
fraction/seconds fields. `no_progress_min_r` is retained, including an explicit 0.

Additional family fields:

- Profit lock: `profit_lock_arm_r`, `profit_lock_floor_r`, with floor below arm.
- Envelope: `risk_envelope_activation_r`, `risk_envelope_initial_floor_r`,
  `risk_envelope_curvature`, `risk_envelope_floor_at_t1_r`,
  `risk_envelope_ratchet_step_r`.

The September migration makes the previously hidden envelope values explicit:
activation 0.5R, initial floor -1R, curvature 1.5, floor at T1 0R, step 0.1R.
The profit-lock row explicitly locks 0.25R after a peak of 0.75R, as described by
the operator. Giveback remains an additional configured protection.

Structural/underlying-bar stops and overnight holding remain unsupported on
this named path. The current structural row is commented out; the range-expansion
profile is explicitly intraday. Do not label an unsupported strategy as an
implemented variation of the ladder.

## 3. Entry sources converge on the same compiler

| Operator surface | Entry source | Exit authority |
|---|---|---|
| `active_strategies` | Catalog strategy signal | Row `management_exit` / `compare_exits` |
| `manual_entry` | Human trigger | Row `management_exit` / `compare_exits` |
| `Chart_Scenarios_v2` | Generated explanation of Cartographer hypotheses | No direct execution authority |
| Cartographer-owned `manual_entry` rows | Projected executable hypothesis | Named defaults copied from `Operator_Defaults_v1` into row columns |

Cartographer defaults live in `section=profile__trend_continuation`, keys
`management_exit` and `compare_exits`. Projection freezes those selections into
the manual row. It retains existing entry, budget, invalidation, and ownership
provenance. An existing row's explicit exit selections, consumed flag, mode, and
Bhiksha writebacks survive retries. New defaults affect new hypotheses.

The projector recognizes the existing 22 headers or those headers plus the two
named-exit columns. It maps by header name, so the actual Sheet's reordered
`management_policy` column is supported. Unknown/missing/duplicate headers fail
before writes. Named rows clear the legacy `management_policy_spec` cell to avoid
dual exit authority. Legacy rows continue to work without forced migration.

Compilation accounts for every enabled row: either a deployment or an explicit
suppression. Research KILL/capability/safety gates are not bypassed to improve a
signal-capture number. Per-signal receipts distinguish fill, no-fill, position
block, risk block, budget block, selection failure, and expired entry window.
Market-data observation gaps remain separate from recorded positive signals.

Option selection uses actual listed expirations. Search the preferred DTE band,
then the explicitly bounded fallback dates in order. Apply the same hard guards
and affordable sizing to each selected contract. `price_seeking` may trade off
preferred OI/spread quality for a lower-than-midpoint limit, but never bypasses
hard liquidity, valid-quote, freshness, cash, or risk limits. Repricing cannot
cross the authorized entry ceiling. New entries stop before the primary hard flat.

## 4. Rail B: preserve history, distinguish recovery from authorization

The current operator settings are a 20-trade lookback, 10 priced-trade minimum,
and $0 mean-P&L floor. They are separate controls. The Sheet's existing
`rail_b_reset_at=2026-09-19T00:00:00Z` starts a new evidence window; it does not
remove trade history or P&L. Only trades entered at/after the cutoff qualify.
The implementation defaults remain unchanged for deployments without Sheet settings.

Rail B reads priced, closed LIVE trades for that deployment, including confirmed
partial fills. Other lanes' paper volume cannot displace the live history.
The live-loss veto remains latched for the session. A blocked live attempt may
produce a separately labeled paper observation. The Sheet's LIVE/SHADOW authority
is never rewritten by a risk outcome.

Automatic recovery is an optional per-lane `execution_overrides.rail_b_recovery`
object. Its absence means OFF. Every field is explicit:

```json
{
  "rail_b_recovery": {
    "min_shadow_trades": 20,
    "min_sessions": 5,
    "max_age_days": 14,
    "min_mean_net_r": 0.10,
    "round_trip_cost_per_contract_usd": 2.00,
    "premium_cap_fraction": 0.20
  }
}
```

This is a reviewable example, not an activated setting or calibrated fee estimate.
Before opting in, set a conservative round-trip cost/slippage allowance appropriate
to the lane. The schema requires at least 20 trades and five sessions, positive
net-R/cost limits, and a premium fraction no larger than 25%.

A recovery probe requires all of the following:

1. The lane is already explicitly LIVE and uses a frozen named primary policy.
2. Rail A and infrastructure checks allow entry; only Rail B refused it.
3. No live position/probe is still open in the lane.
4. The latest required shadow sample was entered after the most recent live close
   and any reset cutoff, within the configured age limit, under the unchanged
   primary policy, with proven modeled fills and complete uncensored comparisons.
5. At least the configured independent ET sessions are represented; no session
   has negative average net R, and the equally weighted session mean exceeds
   the configured floor after the cost allowance.

Only that attempt gets a one-contract, reduced-premium-cap manifest. Ordinary
sizing, cash, book risk, protective-stop, and position checks still run. If one
contract cannot fit, no trade is placed. A subsequent live close consumes the old
sample, so the next probe requires fresh observations even after a restart.
Full-size eligibility returns only when the ordinary live-P&L Rail B gate clears.
Research SHADOW lanes are never promoted automatically. Missing evidence fails
closed and emits a reason. Recovery is disabled on every lane at this cutover.

## 5. Native Public orders: execution choice, not an exit policy

Public supports BRACKET/OCO/OTO for options and equities. Placement is
asynchronous; its returned ID identifies the parent. Get-order exposes
`bracketId`; a transient 404 does not prove the order was rejected. Parent
replacement is prohibited; child replacement cannot change quantity/type/expiry.
[Placement](https://public.com/api/docs/resources/order-placement/place-order),
[status](https://public.com/api/docs/resources/order-placement/get-order),
[replacement](https://public.com/api/docs/resources/order-placement/replace-order).

Current decision: retain application-managed exits with broker protection for
all six configured staged/time/floor policies. Their partial quantities and
changing floors are not equivalent to one fixed full-position bracket. Native
submission flags fail closed; the application does not claim an OCO is active
because a payload builder exists.

A future fixed full-position exit is a BRACKET candidate; a fixed protection-only
entry is an OTO candidate. Enable either only after proving parent/child discovery,
partial-entry protection, sibling cancellation, fills during cancel, unknown
submission outcomes, and restart adoption without duplicate closing orders.
Reuse the existing order manager and durable trade state when that use case is
selected. Do not add an unused parallel executor now. Multi-leg trading changes
vehicle selection and risk, and is outside this single-long-option cutover.

## Release verification

- Local suite and oldmac suite pass before publishing the plan.
- Migration saves preimages, checks for concurrent edits, writes only named cells,
  and verifies readback. Do not re-enable consumed manual rows.
- Compile the actual workbook with complete row accounting and publish atomically.
- Read back effective primary/comparison policy hashes, Rail B settings, recovery
  opt-in count, and native-route count from oldmac.
- Source/Sheet/plan verification is not natural fill evidence. Confirm prospective
  registrations and independent terminal outcomes after market sessions; do not
  manufacture historical fills or infer success from a new plan alone.
