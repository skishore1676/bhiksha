# Prospective Entry Risk

Bhiksha performs this live-only gate after option selection, quote pricing,
quantity sizing, and broker preflight, immediately before cash reservation and
order submission. Shadow and dry-run lanes remain observational.

## Loss headroom

The proposed entry is allowed only when:

```text
max(0, -realized_live_pnl_today)
+ open_position_planned_stop_loss
+ pending_entry_reserved_stop_loss
+ proposed_entry_planned_stop_loss
<= abs(Rail A tier-1 halt threshold)
```

Realized gains do not create extra risk capacity. Protected positions use
`(entry_price - stop_price) * quantity * 100`, floored at zero. A pending or
unprotected position with no usable stop counts its full premium. Missing
budget, trade-book, or proposed-stop truth blocks the live entry.

Concurrent signal runners share an atomic, SQLite-backed reservation. A
reservation is made before broker submission, updated after every
confirmed-cancel reprice, released after a confirmed no-fill or submission
failure, and committed only after the filled/open trade has been persisted.
This prevents simultaneous signals or a runtime restart from spending the same
remaining loss headroom. An orphan with no durable trade row expires after 30
minutes; a reservation tied to a non-closed trade remains represented by that
trade's own risk after commit, while closed-trade reservations are excluded.

## Correlation clusters

`bhiksha.risk.clusters` is the single app-owned map used by both live entry risk
and the family-risk exporter. Confirmed clusters currently include broad-market
ETFs, semiconductors, and volatility products. Unmapped symbols are not guessed:
they remain eligible and emit `cluster_mapped=false` in gate evidence.

The default cap is one open or reserved position per mapped cluster. Set the
`Operator_Defaults_v1` key `max_open_positions_per_cluster` to another integer;
`0` disables this cap. The headroom gate remains independently controlled by
`prospective_loss_enabled`.

## Evidence

Every consult writes `risk_manager_sized_entry_decision` with the proposed,
open, reserved, and realized loss inputs; daily budget and remaining headroom;
cluster name, count, and cap; and the final allow/block reason. Reprice-specific
blocks also write `entry_reprice_sized_risk_blocked`.
