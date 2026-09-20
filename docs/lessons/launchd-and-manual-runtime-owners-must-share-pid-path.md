---
title: Launchd and manual runtime owners must share one PID path
type: gotcha
area: runtime ownership
date: 2026-08-12
tags: [launchd, server-session, pid-file, runtime-safety]
refs: [src/bhiksha/tools/server_session.py, src/bhiksha/tools/launchd_job.py, scripts/launchd/run_bhiksha_job.sh]
---

# Launchd and Manual Runtime Owners Must Share One PID Path

## What We Learned

A Bhiksha live process is not singularly owned unless manual
`server_session` commands and launchd resolve the same PID metadata path.
Starting with an absolute external path while launchd defaults to the
repo-local `artifacts/playbook/runtime/bhiksha.pid` lets the watchdog treat
the repo-local file as stale and start a duplicate process.

## Context and Evidence

The 2026-08-12 chart-experiment-simplification evaluator found two identical
`--live` processes: PID 99223 owned through an external PID path and PID 1028
owned through the repo-local launchd path. After the external owner was
stopped and the canonical repo-local owner was started, the next natural
`com.bhiksha.live-watchdog` run completed successfully and retained one
process, PID 10051.

## When It Applies

Before any controlled start, stop, or restart, resolve the owner path from
the actual launchd working directory and runner. Confirm the direct owner
status and exact process count; a stale status projection is not owner truth.

## Apply It Next Time

Use the repo-local absolute PID path for a launchd-owned session:
`/Users/sunny/Documents/bhiksha/artifacts/playbook/runtime/bhiksha.pid`.
If an external PID path is deliberate, change the launchd environment or
configuration under a separate protected approval so both owners share it.
