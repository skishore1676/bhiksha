---
title: Reconciliation snapshots cannot regress newer order intent
type: bug
area: runtime reconciliation and exit accounting
date: 2026-07-13
tags: [reconciliation, exits, monotonic-state, fill-truth]
refs: [src/bhiksha/app/runtime.py:1426, src/bhiksha/app/runtime.py:1920, src/bhiksha/persistence/sqlite.py:363, src/bhiksha/ops/daily_report.py:682]
---

# Reconciliation Snapshots Cannot Regress Newer Order Intent

## Context
Reconciliation fetched a Public portfolio and durable trade rows before a live
strategy exit was submitted. It later committed that older view over the
tracker and trade row, erasing the accepted exit order ID. When the position
disappeared, the lifecycle sweep no longer knew which order to query for fill
truth.

## What We Learned
A broker snapshot is authoritative about position presence and quantity, but it
is not automatically newer than local order intent. If an exit was submitted
after the snapshot began, its order classification and identity must merge
monotonically instead of being replaced by the older observation.

Closed live trades without confirmed exit fill truth are not zero-P&L trades.
Reports must show their realized P&L as unknown and surface a data-quality
warning until enrichment succeeds.

## Why / When It Applies
Any broker read performed outside the execution critical section can overlap an
entry, exit, cancel, or reprice. This applies even when each individual write is
atomic: the stale read can still be internally valid and temporally obsolete.

## Specifics
- Preserve a newer pending exit's stop, target, and exit classification when a
  fetched position still exists (`runtime.py:_preserve_newer_pending_exit_state`).
- Prevent a generic stale open-state upsert from erasing a durable pending exit
  (`sqlite.py:_upsert_trade_sync`).
- If the position has already vanished, retain the exit ID so
  `sync_lifecycle()` can query the terminal order and persist price, quantity,
  timestamp, and status.
- Cover both timing branches: position still visible and position already
  absent. Also cover the case where a stale trade row misclassifies the newly
  visible limit order as a profit target.

## Apply It Next Time
When logs show `exit_pending` followed within milliseconds by
`open_protected` or `open_unprotected`, treat it as a stale-commit signal. Build
the regression with an in-flight portfolio read, then submit the exit before
the read commits. Assert that no duplicate close can be issued and that a fast
fill remains enrichable by its original order ID.

## Dead Ends
Do not repair this only in reporting. Reporting can expose missing truth, but it
cannot recover an order ID already erased by reconciliation.
