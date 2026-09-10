---
title: Concurrent status writers need a transaction lock
type: bug
area: launchd status evidence
date: 2026-09-10
tags: [launchd, concurrency, evidence]
refs: [src/bhiksha/ops/launchd_status_store.py:23, tests/test_launchd_control_status.py:100]
---

# Concurrent Status Writers Need a Transaction Lock

## What We Learned

Atomic file replacement prevents partial JSON, but it does not prevent a lost
update when two scheduled jobs read the same snapshot, merge different records,
and replace the file concurrently. Serialize the complete read, merge, and
replace transaction with a stable sibling lock file.

## Context and Evidence

`live-watchdog` and `reconciliation-supervisor` share a ten-minute schedule and
both write `artifacts/playbook/launchd/latest_status.json`. An interleaving where
both read before either replaces allowed the last writer to erase the first
writer's fresh record. The cross-process regression test holds the lock, starts
a waiting writer, adds the other job record, then proves the waiting writer
rereads and preserves both records.

The lock wait is bounded and status persistence remains observational: lock or
write failure must not turn a successful domain job into a failed launchd job.
The import-failure fallback in `scripts/launchd/run_bhiksha_job.sh` must use the
same lock and unique temporary-file convention.

## When It Applies

Use this pattern whenever independent processes update different keys in one
JSON snapshot. A unique temporary file plus atomic replace is sufficient only
for single-writer output or whole-document last-writer-wins semantics.

## Apply It Next Time

If a shared snapshot intermittently loses one of two valid records fired at the
same time, test the read/merge/replace interleaving first. Lock a sibling path
that is never replaced, reread only after acquiring it, and cover the behavior
with a real child process.
