---
title: Protective stop ratchets need broker-proved handoffs
type: pattern
area: live exit supervision
date: 2026-07-25
tags: [protective-stop, reconciliation, restart, idempotency]
refs:
  - src/bhiksha/execution/supervisor.py
  - src/bhiksha/persistence/exit_state.py
  - tests/test_exit_state_recovery.py
---

# Protective stop ratchets need proved handoffs

## What We Learned

A broker POST acknowledgement is not proof that a replacement protective stop
exists. Canceling the old stop and immediately committing the new local floor
can leave the runtime believing a trade is protected while the replacement is
rejected, delayed, misidentified, or absent. Blindly restoring after a timeout
is also unsafe because the unindexed replacement may already be live, creating
two competing SELL/CLOSE orders.

After recovery adopts a later-working stop into the position tracker, the same
manage tick must re-read that tracker. Continuing with the pre-recovery local
position can otherwise trigger the generic missing-protection path and submit a
second STOP even though reconciliation succeeded.

## Context and Evidence

Treat a stop ratchet as a recoverable handoff:

1. persist intent, requested floor, requested stop, and prior stop identity;
2. canonicalize the requested stop to the broker's actual SELL tick before
   persisting it;
3. prove the prior stop terminal with an explicit zero fill;
4. submit the replacement with a deterministic client identity;
5. classify readback as working, filled, explicit dead-zero-fill, or ambiguous;
6. commit only an exact working order, and close/reconcile after any fill;
7. restore the prior stop only after explicit dead-zero-fill, persisting the
   deterministic restore identity before POST; and
8. leave pending cancels, missing fill quantities, 404s, timeouts, and identity
   mismatches open and degraded for stage-aware restart reconciliation.
9. latch the deployment against new entries and new ratchets after a handoff
   safety violation, while continuing exact-order reconciliation for the
   already-open trade; and
10. adopt a later-proved replacement or restore into tracker, trade state, and
    lifecycle before resolving the intent or clearing degraded state.

The floor is a derived desire. The last broker-proved working STOP is protection
truth. Durable state must never advance from a calculation or POST response
alone.

## When It Applies

Use this pattern whenever protection is replaced with cancel-and-replace and
broker reads may be eventually consistent. It applies to normal supervision,
restart recovery, and hard-flat coordination. It does not justify leaving a
position unprotected: unresolved handoffs stay degraded and reconcile by exact
order identity while new treatment is rollback-latched.

## Apply It Next Time

- Can any crash occur between broker acceptance and the local order bind?
- Does restart query the deterministic identity before submitting anything?
- Are side, CLOSE indicator, contract, type, remaining quantity, price, and
  working status all checked?
- Does explicit rejection restore protection once?
- Can any partial/full fill accidentally enter the restore branch?
- Does a pending/unknown old-stop cancel remain open without claiming the old
  stop is still protection?
- Does restart inspect the prior stop before a replacement and resume the exact
  prior/replacement/restore stage?
- Are the persisted, submitted, and proved prices the same broker-tick value?
- Does ambiguous 404/timeout avoid duplicate restores and new ratchets?
- Does a process restart reload the rollback latch before accepting a new
  entry?
- Can hard flat prove that prior/replacement/restore stages cannot overlap its
  SELL/CLOSE order?
- Can an open intent block the generic missing-protection path?
- Do telemetry and the weekly/session manifest distinguish configured,
  authorized, requested, and broker-proved state?

Run the full manage-loop regressions in `tests/test_exit_state_recovery.py`, not
only the lower-level hydration helper. The fastest tell is more than one broker
working SELL/CLOSE STOP for a one-contract position, or a tracker stop id that
does not match the broker-proved identity.

## Dead Ends

- Treating POST acknowledgement as protection proof.
- Blindly restoring the prior stop after a timeout or 404.
- Testing hydration alone while skipping the remainder of the manage tick.
- Clearing degraded state before tracker, trade record, and lifecycle all
  adopt the same broker-proved stop.
