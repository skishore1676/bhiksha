---
title: Entry cancel races need fill accounting before replacement or cleanup
type: bug
area: live entry execution
date: 2026-07-15
tags: [entry, cancel-race, partial-fill, reconciliation, protection]
refs:
  - src/bhiksha/execution/supervisor.py
  - tests/test_execution_supervisor.py
---

# Entry Cancel Races Need Fill Accounting Before Replacement Or Cleanup

## Context
Profile-based repricing increased the number of entry cancels. The old entry
path treated `cancel_ok` plus any status other than `FILLED` as an unfilled
order, even though Public can return a terminal `CANCELED` order with a positive
`filledQuantity`.

## What We Learned
A canceled entry is not necessarily an unfilled entry. Resolve full fill,
terminal partial fill, confirmed dead zero-fill, and ambiguous state before
replacing the order or closing local tracking.

## Why / When It Applies
This applies anywhere a buy order is canceled before replacement or timeout
cleanup. Replacing the original quantity after a partial fill can overbuy;
closing tracking after the same partial fill leaves a real position unprotected.

## Specifics
- A terminal partial fill becomes the smaller actual position and receives
  protection immediately. Bhiksha does not negotiate the residual while the
  acquired contracts are unprotected.
- Replacement and no-fill cleanup require a terminal dead status with zero
  reported fill.
- Timeout, active status, unparsable quantity, negative quantity, a quantity
  larger than the submitted order, or a `FILLED` quantity inconsistent with the
  submitted order all enter reconciliation hold.
- Protective quantity is bounded by broker truth that is consistent with the
  submitted order; corrupt readback can never create an oversized close order.

## Apply It Next Time
When adding a cancel-and-replace path, model cancel acknowledgement and order
state as separate facts. Test terminal partial fills and malformed quantities at
both the replacement checkpoint and the final timeout.
