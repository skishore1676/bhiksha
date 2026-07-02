---
title: Armed config is not live behavior — a fail-closed gate needs per-decision input telemetry
type: bug
area: execution/profile-exit dispatch, state/reconciliation
date: 2026-07-02
tags: [fail-closed, reconciliation, dispatch-gate, telemetry, deploy-gap]
refs: [src/bhiksha/state/reconciliation.py:65 (pre-fix), src/bhiksha/execution/profile_exit.py:711, src/bhiksha/execution/profile_exit_shadow.py (gate_inputs), 2e7afb9, d0fe4b0]
---

# Armed config is not live behavior — a fail-closed gate needs per-decision input telemetry

## Context
The profile-exit dispatch gate (`profile_exit_dispatch_allowed`) was armed in the
operator's Sheet (rows 2–7: `profile_exit_drives_live=true`,
`runtime_mode=live_approval_gated`) since ≤2026-06-23, and the armed dispatch
route was merged 2026-06-14 — yet not one profile exit ever dispatched through
2026-07-01. Nobody noticed for weeks.

## What we learned
Two independent silent failures stacked, and neither produced any signal:

1. **Merged ≠ deployed.** oldmac pulled on Jun 9 and then not again until Jun 30
   — the armed route sat unmerged-on-the-box for 16 days while everyone reasoned
   about "the code" from the repo. There is no auto-deploy; `live-start` does
   not pull.
2. **A fail-closed gate consuming a runtime-assigned field that ANOTHER
   subsystem rewrites fails closed forever, silently.** The ~15s reconciliation
   sweep REPLACES tracker positions wholesale and labeled every matched
   position `source="broker_sync"`; the allowlist only opens for
   `live_open`/`live_pending`. Every live position lost dispatch authority
   within seconds of entry. The armed tests all used `source="live_open"`
   fixtures — the exact H-1 audit warning ("live real-order path untested").

The generalizable fix: **every gate decision event must record its inputs**
(`gate_inputs: {live, deployment_shadow_only, position_source, runtime_mode}` in
each `profile_exit_shadow` event). "Armed but `dispatch_allowed=false`" then
becomes a queryable, alertable condition instead of an invisible one.

## Why / when it applies
Any fail-closed allowlist whose inputs include runtime-assigned state (position
source, mode, ownership). Fail-closed is the right default for money paths, but
it converts wiring bugs into *silent no-ops* — the failure mode is "nothing
happens", which looks identical to "nothing was supposed to happen".

## Specifics
- Root cause fix (`2e7afb9`): reconciliation keeps `live_open` for a broker
  position matched to a durable OPEN trade record with a real broker entry
  order id; paper entries, missing ids, matched-CLOSED records, and unmatched
  (`broker_recovered`) stay excluded.
- Diagnosis was only conclusive because we shipped `gate_inputs` telemetry
  first (`d0fe4b0`) and compared an offline gate evaluation (opens with
  `live_open`) against production records (`dispatch_allowed=false` from the
  first tick).
- First real dispatch after the fix: 2026-07-02 09:22 CT, SMH `no_progress` →
  broker order `5606c725` FILLED — proof triple = routed event (`dry_run:false`)
  + `mode=live_dispatch` record + broker payload.

## Apply it next time
Arming anything live? Demand a **positive** proof artifact on day one (an event
that says the gate OPENED with these inputs), not the absence of errors. If a
feature is "on" and produces zero dispatches/orders/effects, treat that as an
incident, not a quiet day. And check `git log` on the runtime box, not the repo.

## Dead ends
- Suspecting the allowlist itself (it was correct all along — it accepts
  `live_open`/`live_pending`; the INPUT was wrong).
- The MU "index-like quote scaling" warning — a false positive (MU legitimately
  trades >$1k post-2026-05); fixed via allowlist + retained ratio check.
