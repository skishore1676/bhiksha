---
title: Historical findings from the 2026-07-02 adversarial reviews
type: historical-lesson
area: execution (profile-exit, risk rails), process
date: 2026-07-02
tags: [adversarial-audit, live-trading, testing, partials, settings-validation]
refs: [src/bhiksha/risk/risk_settings.py, src/bhiksha/execution/supervisor.py (_profile_state_identity_mismatch), src/bhiksha/state/reconciliation.py (_resolve_trade), d7f5517, dcb2514]
---

# Historical findings from the 2026-07-02 adversarial reviews

This lesson preserves what those operator-requested reviews found. It is not a
standing release requirement and does not authorize an agent to start an audit,
review worker, or re-audit loop. Audit runs occur only when Suman explicitly
requests them.

## Context
The 2026-07-02 cycle shipped three money-path changes (risk rails, reconciliation
source fix, ladder identity backstop). Each passed a full green test suite
before audit. The audits still found five real, reproducible live-money bugs.

## What we learned
A green suite proves the code does what its tests encode — including the bugs
its tests encode. The audit rounds each caught a DIFFERENT class:

1. **Round 1 (fresh adversarial, rails):** unvalidated settings — inverted
   drawdown tiers allowed new entries DURING a flatten (fail-open), and a
   negative pct flipped the threshold sign and would have flattened a healthy
   book on day one (fail-closed-too-hard). Root cause: silent env fallbacks
   with zero validation.
2. **Round 1 (fresh adversarial, source fix):** the trade-matcher's
   single-candidate shortcut let a STALE open record capture a new fill on the
   same contract — live authority plus a corrupted ladder (stale peak →
   spurious giveback square-off), self-reinforcing because the mismatch kept
   the stale record looking active.
3. **Round 2 (re-audit of MY fix):** the `banked_quantity > position_quantity`
   backstop fired on the ROUTINE post-partial tick (the position holds only the
   residual after T1) — reseed → T1 refire → **T2 runner amputated**, i.e. the
   fix silently destroyed the exact exit semantics being armed. **My own unit
   test had encoded the false positive as expected behavior.**
4. **Round 2:** my $0.05 price-contradiction threshold could reject a TRUE
   record (fallback-to-limit entry price + price-improved fill) — reintroducing
   the gate-shut symptom AND letting `sync_lifecycle` mis-close the real trade.

## Why the history remains useful
The reviews concerned changes that could place, size, or suppress real orders.
Their concrete reproductions and pinned regression tests remain useful evidence,
but they do not prescribe a future review workflow.

## Specifics
- Identity/sanity predicates must be tested against the **routine lifecycle**
  (post-partial residual state), not only adversarial cases — that's how bug #3
  got in and how its regression test (`test_post_partial_residual_tick_does_not_reseed`)
  keeps it out.
- Corroboration thresholds and contradiction thresholds are different numbers:
  match tightly ($0.05), contradict only on gross divergence (>10% AND >$0.25).
- Settings resolution for risk knobs must validate ranges AND cross-field
  invariants (`flatten >= halt`, `min_n <= window`) and surface every
  rejected/clamped input (`validation_warnings` → startup event); plus an
  independent semantic backstop (tier-2 flatten implies the entry block).

## Apply it next time
Run the focused lifecycle, settings-validation, identity, and regression tests
described above. If Suman explicitly requests an audit, use the historical
reproductions to define its hunt list and scope.
