---
title: Broker fill precision is corroboration, not trade identity
type: pattern
area: live exit-state recovery
date: 2026-07-28
tags: [exit-engine-v2, restart, identity, broker-fills, rounding]
refs:
  - src/bhiksha/execution/supervisor.py
  - tests/test_exit_state_recovery.py
  - tests/test_profile_exit.py
---

# Broker fill precision is corroboration, not trade identity

## Incident

The first natural IWM Safety Stack canary filled at a broker average of
`0.9798`. Its durable Exit Engine state correctly retained that precise fill,
while the reconciled trade/position ledger exposed the option-tick value
`0.98`. Restart hydration compared the two floats with exact equality and
incorrectly emitted `runtime_state_identity_mismatch`.

The proved broker STOP continued to protect the trade, but profile and native
exit authority remained closed. The canary therefore did not produce a valid
test of the Dynamic Risk Envelope.

## Contract

- `trade_id` is the primary identity across runtime, restart, and reporting.
- deployment id and option contract are exact identity coordinates.
- the precise confirmed broker fill remains the frozen economic seed for
  risk/R calculations; do not rewrite it to the rounded ledger value.
- the tracked entry premium is corroborating evidence. Missing, non-positive,
  or non-finite values fail closed.
- two valid premiums represent the same fill only within the half-cent envelope
  created when a sub-cent broker average is rounded to the nearest option cent.
  Wider differences fail closed.

This narrow threshold distinguishes normal broker/ledger precision from a new
low-premium fill. The separate in-memory ladder backstop retains its existing
greater-than-10% rule; this incident does not weaken it.

## Evidence interpretation

An observation affected by an implementation failure is infrastructure-invalid,
not negative strategy evidence. Do not use the 2026-07-28 IWM result to accept,
reject, or expand the Safety Stack. Continue the same bounded canary after the
repair and make later strategy decisions only from clean, comparable outcomes.

## Verification

Pin regressions for:

- the observed `0.9798` broker seed versus `0.98` tracked premium;
- gross price contradiction;
- a stale `$0.10` post-partial ladder versus a new `$0.34` fill;
- missing, zero, negative, NaN, and infinite tracked premiums;
- ordinary post-partial residual state; and
- recovery without mutating the precise durable seed.

Money-path release still requires the complete suite, fresh adversarial review,
same-auditor delta review, post-flat deployment, and oldmac restart/readback.
