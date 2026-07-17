---
title: Public cancel acknowledgement is not cancellation confirmation
type: bug
area: live order execution
date: 2026-07-16
tags: [entry, exit, cancel-race, partial-fill, reconciliation, protection, public]
refs:
  - https://public.com/api/docs/resources/order-placement/cancel-order
  - https://public.com/api/docs/resources/order-placement/get-order
  - src/bhiksha/execution/order_manager.py
  - src/bhiksha/execution/supervisor.py
  - src/bhiksha/state/reconciliation.py
  - tests/test_execution_supervisor.py
---

# Public Cancel Acknowledgement Is Not Cancellation Confirmation

## Context
Profile-based repricing increased the number of entry and exit cancel-replace
transitions. Public documents DELETE as an asynchronous request: HTTP 200 says
the request was accepted, while GET order is the required confirmation surface.
Public can also return a terminal `CANCELLED` order with a positive
`filledQuantity`.

## What We Learned
A cancel acknowledgement is not a broker-state transition, and a canceled
order is not necessarily unfilled. Resolve full fill, terminal partial fill,
confirmed dead zero-fill, and ambiguous state before replacing any entry or
exit order or closing local tracking.

## Why / When It Applies
This applies anywhere an order is canceled before replacement, protection
handoff, flattening, or timeout cleanup. Replacing the original quantity after
an entry partial fill can overbuy; replacing a sell after an exit partial fill
can oversell. Dropping a `PENDING_CANCEL` stop from portfolio reconciliation can
also create duplicate protection.

## Specifics
- A terminal partial fill becomes the smaller actual position and receives
  protection immediately. Bhiksha does not negotiate the residual while the
  acquired contracts are unprotected.
- Replacement and no-fill cleanup require a terminal dead status with zero
  reported fill.
- Public working states include `PARTIALLY_FILLED`, `PENDING_CANCEL`,
  `PENDING_REPLACE`, and `QUEUED_CANCELLED`; while any appears in Portfolio v2,
  the resting order remains attached to the position.
- A partially filled entry keeps its original submitted quantity in durable
  hold state. Portfolio quantity is the currently acquired position, not the
  denominator for validating a later terminal `filledQuantity`.
- Protection reconciliation compares the position with the resting order's
  remaining quantity (`quantity - filledQuantity`) and confirms cancellation
  before resizing.
- GET order may temporarily return HTTP 404 before indexing even when the order
  is already active. A 404 is unknown/pending, never proof of absence.
- Timeout, active status, unparsable quantity, negative quantity, a quantity
  larger than the submitted order, or a `FILLED` quantity inconsistent with the
  submitted order all enter reconciliation hold.
- Protective quantity is bounded by broker truth that is consistent with the
  submitted order; corrupt readback can never create an oversized close order.

## Apply It Next Time
When adding a cancel-and-replace path, model cancel request acceptance and order
state as separate facts. Test HTTP 200 plus `PENDING_CANCEL`, temporary 404,
terminal partial fills, and malformed quantities at both the replacement
checkpoint and the final timeout. Do not adopt Public's replace endpoint until
replacement-order identity and replay are modeled durably.
