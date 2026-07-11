# Paired Exit Edge Lab

The lab has two deliberately separate modes.

1. Historical mode audits whether existing persisted data can support a paired
   comparison. It never estimates paired outcomes. The current event history is
   generally right-censored at the authoritative exit, and legacy/profile
   buckets in the weekly scorecard contain different trades and are confounded
   by entry, contract, time, and lane selection.
2. Prospective mode replays the current profile and legacy mechanics from one
   immutable actual entry and one append-only executable quote tape. It admits a
   pair only after both virtual arms have terminal modeled fills.

This is observational counterfactual evidence, not causal proof. Per tradelab
ADR-011 it cannot discover or validate other exit profiles.

## Prospective evidence contract

At entry, freeze a cohort containing `cohort_id`, immutable `trade_id`,
`cluster_id`, deployment, symbol, contract, entry timestamp, actual entry fill,
original quantity, both policy configs, evaluator/fill-model versions, analysis
knobs, and quote source/feed lineage. One SHA-256 hash covers that entire
experiment spec. Each quote record must carry provider `quote_at`, local
`received_at`, monotonic `sequence`, provider/cache `source` and `feed`, bid,
ask, last, spread, and derived freshness.

Prospective fixtures must carry that explicit precomputed hash. The loader never
auto-signs an unhashed fixture, because doing so after a settings edit would make
mutable analysis settings look frozen.

The tape must continue after the first actual or virtual exit until both virtual
arms terminate or the cohort is explicitly censored. The recorder consumes an
existing quote cache/feed or an isolated low-priority quota; it must never add
broker calls that compete with protection or exit traffic.

Fill model:

- a trigger at sequence N cannot fill at N;
- a long-option exit fills at the first later-sequence, fresh, non-crossed
  executable bid after configured latency;
- this is a modeled natural-bid fill with no size, queue-position, or slippage
  guarantee;
- midpoint, last, ask fallback, and last-mark imputation are forbidden;
- modeled fills and real broker fills remain separate facts.

Missing bid, stale/crossed/out-of-order/duplicate quotes, sequence gaps, recorder
failures, or a tape ending before both arms fill make the case insufficient.

`ProspectiveQuoteTapeRepository` is a separate experiment store using a 1ms
SQLite busy timeout. Its `try_*` methods swallow storage/serialization failures
and return `False` so the
experiment is censored without changing live decision/dispatch timing. It has
no broker imports and never restores or mutates the real profile FSM/order
state. Production integration must still enqueue writes off the money-path
thread. Cohort registration and quote appends are idempotent; conflicting reuse
of an identity or sequence fails closed for the experiment. Orphan quotes and
source/feed transitions are rejected. Censor reasons persist and the repository
can reconstruct a replay case after restart.

## Commands

Historical eligibility audit against a read-only snapshot:

```bash
PYTHONPATH=src python -m bhiksha.tools.exit_edge_lab \
  --db-path /path/to/bhiksha.snapshot.db \
  --start 2026-07-02 --end 2026-07-31 \
  --output-dir /tmp/exit-edge-history
```

Prospective fixture replay:

```bash
PYTHONPATH=src python -m bhiksha.tools.exit_edge_lab \
  --fixture-json /path/to/paired-tape.json \
  --output-dir /tmp/exit-edge-paired
```

Fixture cases freeze an `experiment` object and use `quotes` entries with
`sequence`, `source`, `feed`, `quote_at`, `received_at`, `bid`, `ask`, and
`last`. The report includes paired delta P&L, holding-window MFE/MAE on
executable bids, both arms' time in trade, sample count, cluster labels, and
explicit censor reasons. Win rate and its Wilson interval are descriptive only.
Directional uplift requires at least eight labeled clusters, positive total and
mean paired P&L, and a positive distribution-free one-sided 95% lower bound on
median cluster uplift.

Residual governance risk: `cluster_id` is immutable once registered, but its
upstream derivation/provenance is not yet standardized. Before using cluster
inference for promotion, define and version the clustering rule (for example,
trading session plus correlated-underlying family) so relabeling cannot change
the inference unit after results are visible.
