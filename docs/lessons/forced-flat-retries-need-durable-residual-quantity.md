---
title: Forced-flat retries must wait for durable residual quantity
type: bug
area: exit execution and reconciliation
date: 2026-07-24
tags: [forced-flat, partial-fill, reconciliation, quantity, idempotency]
refs:
  - src/bhiksha/execution/supervisor.py:_prepare_position_for_forced_flat
  - tests/test_exit_state_recovery.py:test_hard_flat_keeps_blocking_after_fill_proof_until_quantity_refresh
  - 5ceac29
---

# Forced-Flat Retries Must Wait for Durable Residual Quantity

## What We Learned

A forced-flat sweep cannot treat a resolved partial-close intent as proof that
the tracked position quantity is current. After the broker confirms a partial
fill, durable exit state can know the banked quantity one sweep before the
position tracker reflects the residual. Every later sweep must keep blocking
until those two authorities agree.

## Context and Evidence

The first implementation blocked while a partial intent remained open, then
allowed the next hard-flat sweep after reconciliation resolved that intent.
The tracker still held the pre-fill quantity, so the retry could submit an
oversized close. Commit `5ceac29` made
`_prepare_position_for_forced_flat` compare the tracked quantity with
`seed_quantity - banked_quantity`, independent of whether the original intent
is still open. The two-sweep regression proves that both sweeps submit zero
closes while the tracker is stale.

## When It Applies

Use this invariant for hard-flat, halt-and-flatten, or any universal close that
can race with a partial exit. It stops applying only after the position
snapshot reaches the durable residual quantity, or broker truth proves the
position is already gone.

## Apply It Next Time

Test at least two consecutive flatten sweeps: the first establishes the fill,
and the second exercises the dangerous gap after intent resolution but before
quantity refresh. Assert zero close submissions, preserved banked quantity,
and an explicit reconciliation request on both sweeps.

