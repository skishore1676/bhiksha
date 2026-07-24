---
title: Observational SQLite readback needs a separate bounded lock budget
type: perf
area: exit-edge observability
date: 2026-07-24
tags: [sqlite, concurrency, readback, latency, observability]
refs:
  - src/bhiksha/ops/exit_edge_lab.py:registration_summary
  - tests/test_exit_edge_live.py:test_registration_summary_waits_for_brief_schema_writer_lock
  - b0d33df
  - 8480c8a
---

# Observational SQLite Readback Needs a Separate Bounded Lock Budget

## What We Learned

A status reader and a latency-sensitive recorder should not share one SQLite
lock policy. The status path may wait briefly for schema creation or a short
writer transaction, while quote ingestion and all trading-adjacent writes must
remain effectively nonblocking and fail visibly.

## Context and Evidence

The oldmac full suite exposed two host-timing cases that Air did not: a status
read could arrive before the recorder created
`exit_edge_registration_attempts`, and a later run could collide with a brief
exclusive schema lock. Commit `b0d33df` treats only that missing-table startup
window as an empty denominator. Commit `8480c8a` gives only
`registration_summary()` a bounded 250 ms read timeout. All recorder, quote,
and write connections retain the 1 ms budget, and persistent lock errors still
propagate.

## When It Applies

Use this separation when an observational query reads a WAL-backed database
that another worker initializes or updates. Do not use it to hide corruption,
permission errors, persistent locks, or contention on a money-path write.

## Apply It Next Time

Pin three cases: pre-schema read returns an empty denominator; a brief
exclusive writer lock clears within the observational budget; and a persistent
lock remains a visible failure near the bound. Keep timeout overrides local to
the named read method instead of widening the repository default.

