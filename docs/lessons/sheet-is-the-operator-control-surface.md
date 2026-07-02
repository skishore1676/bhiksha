---
title: The active_strategies Sheet IS the operator control surface — honor+surface gate keys; partition evidence gates from safety gates
type: decision
area: active_plan/compiler
date: 2026-07-02
tags: [google-sheet, control-plane, deny-list, shadow, evidence-gates]
refs: [src/bhiksha/active_plan/compiler.py (_detect_gate_override_keys, _validate_google_catalog_alignment), 276cda5, aa9eeae]
---

# The active_strategies Sheet IS the operator control surface — honor+surface gate keys; partition evidence gates from safety gates

## Context
Two compiler policy decisions from the 2026-07-02 cycle, both reversing an
earlier "harden by blocking" instinct.

## What we learned
**1. Deny-listing Sheet-set dispatch-gate keys was security theater that would
have disarmed the operator.** The Sheet already decides `mode=live` (real order
submission) — strictly more power than `profile_exit_drives_live`. A hostile
Sheet means you have already lost; the realistic actor writing those cells is
the OPERATOR arming a feature. Stripping the keys (the original carry-forward
"hardening") would have silently disarmed the operator's pre-staged live flip
on the next 08:20 sync. Correct posture: **honor + surface** — every gate key a
row sets appears in `plan.summary["gate_override_key_warnings"]` so arming is
always visible in the compiled-plan audit trail, never blocked.

**2. Evidence gates and safety gates are different species.** The row compiler
enforced live-grade activation evidence (`mala_evidence_ready`,
`activation_candidate` M7 provider concordance, `option_trade_ready`) for ALL
rows — but shadow lanes are the instrument that GATHERS that evidence; blocking
shadow on missing activation evidence is circular. Partition:
- **Evidence-quality gates** → relax for non-live rows, stamped into
  `source.metadata.evidence_gates_relaxed` (visible, and live promotion re-runs
  the full gate).
- **Safety/integrity gates** → never relax, any mode: runtime capability,
  `bhiksha_ready`, explicit `m7_status=block`, `triage_verdict=KILL`, retired,
  symbol/strategy_key mismatch. A KILL verdict (e.g. `option_not_tradeable`) is
  a verdict, not missing evidence — paper-trading an untradeable option teaches
  nothing.

## Why / when it applies
Whenever adding a "hardening" that filters operator-writable config: ask what
the SAME surface can already do. If it can already do worse, filtering is not a
boundary — it's a foot-gun against legitimate operator intent. And whenever a
gate blocks the very mechanism that would produce the evidence the gate
demands, split the gate.

## Specifics
- 2026-07-02 result: 19 lanes compile (5 live full-gated + 14 shadow, 13 with
  relaxed-evidence stamps); 7 rows stay suppressed on triage KILL; live lanes
  provably unchanged (`LANE_CONFIG_CHANGE_COUNT=0`, plus a defense-in-depth
  raise if a live row ever carries relaxations).
- Column aliases matter: `exit` → `exit_overrides`, `execution` →
  `execution_overrides` (compiler `_COLUMN_ALIASES`) — the operator's arming
  cells ride the free-form override channel.

## Apply it next time
New lane won't compile? Check `plan.suppressed` reasons first — then decide
whether the failing gate is evidence (relaxable for shadow, visibly) or safety
(fix the capability/verdict upstream, never bypass).
