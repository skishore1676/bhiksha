---
title: Validate signed deployments before attaching observation identity
type: gotcha
area: active-plan evidence binding
date: 2026-08-02
tags: [authorization, evidence, active-plan, canary]
refs: [src/bhiksha/active_plan/compiler.py, src/bhiksha/evidence/bindings.py, tests/test_active_plan_compiler.py, 414c7a2]
---

# Validate Signed Deployments Before Attaching Observation Identity

## What We Learned

A live-triage authorization signs the complete compiled deployment, including
source metadata. Prospective experiment metadata must therefore be attached
only after the signed deployment has passed authorization validation. When a
new observation packet differs from the packet signed by the canary, preserve
the signed fields and put the prospective packet in a separate observation
namespace.

## Context and Evidence

The first decision-ready evidence audit applied the new packet before
`_validate_compiled_live_triage_authority`. That changed signed metadata and
made an otherwise valid PDD authorization fail. It also exposed a tempting but
incorrect alternative: replacing the historical packet with the prospective
packet would make the new cohort look content-addressed to an authorization it
never received.

`test_pdd_v2_authorization_validates_before_observation_binding` now proves the
order: validate the unchanged canary, apply any demotion/inhibition overrides,
then attach observational identity to the final effective deployment. Runtime
attribution prefers `observation_evidence_*`; authorization validation keeps
using the original signed fields.

Observation bindings are not a second authorization system. If a binding is
incompatible with a Sheet-authorized live row, the compiler preserves the
validated live deployment and marks its observation identity
`evidence_binding_quarantined`; normalized facts then classify that trade as
`plumbing_invalid` rather than using mismatched evidence. The same mismatch on
a shadow row suppresses the row, because an unattributable observation has no
research value.

Canonical plan writers also require a complete coverage ledger. Every enabled
operator row must be either loaded or excluded by an explicit policy gate.
Missing rows caused by observation binding, invalid input, duplicate identity,
or unexplained compiler loss make the candidate diagnostic-only and preserve
the previously installed plan.

## When It Applies

Use this split whenever authorization covers a whole deployment and later
systems need additive experiment, analytics, or provenance metadata. If the
new packet itself is explicitly authorized, a separate namespace is not
needed and the identity may be marked content-addressed.

## Apply It Next Time

Before adding metadata to a live deployment, recompute its authorization hash.
If the hash changes, either validate before the additive projection or issue a
new authorization. Never weaken the authorization hash by silently excluding
arbitrary metadata keys.
