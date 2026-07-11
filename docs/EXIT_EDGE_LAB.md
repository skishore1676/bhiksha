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

At entry, freeze a cohort containing `cohort_id`, immutable `trade_id`, contract,
actual entry fill, original quantity, both policy configs, and their SHA-256
hash. Each quote record must carry provider `quote_at`, local `received_at`,
monotonic `sequence`, provider/cache `source`, bid, ask, last, spread, and
derived freshness.

The tape must continue after the first actual or virtual exit until both virtual
arms terminate or the cohort is explicitly censored. The recorder consumes an
existing quote cache/feed or an isolated low-priority quota; it must never add
broker calls that compete with protection or exit traffic.

Fill model:

- a trigger at sequence N cannot fill at N;
- a long-option exit fills at the first later-sequence, fresh, non-crossed
  executable bid after configured latency;
- midpoint, last, ask fallback, and last-mark imputation are forbidden;
- modeled fills and real broker fills remain separate facts.

Missing bid, stale/crossed/out-of-order/duplicate quotes, sequence gaps, recorder
failures, or a tape ending before both arms fill make the case insufficient.

`ProspectiveQuoteTapeRepository` is a separate experiment store. Its `try_*`
methods swallow storage/serialization failures and return `False` so the
experiment is censored without changing live decision/dispatch timing. It has
no broker imports and never restores or mutates the real profile FSM/order
state. Cohort registration and quote appends are idempotent; conflicting reuse
of an identity or sequence fails closed for the experiment.

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
  --max-freshness-ms 2000 --fill-latency-ms 0 \
  --output-dir /tmp/exit-edge-paired
```

Fixture cases use `quotes` entries with `sequence`, `quote_at`, `received_at`,
`bid`, `ask`, and `last`. The report includes paired delta P&L, MFE/MAE on
executable bids, both arms' time in trade, sample count, explicit censor reasons,
and a conservative Wilson interval over positive paired deltas. Fewer than eight
complete pairs always reports `insufficient_sample`.
