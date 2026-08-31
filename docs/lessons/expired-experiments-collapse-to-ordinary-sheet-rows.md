---
title: Expired experiments collapse to ordinary Sheet rows
type: migration
area: active-plan control plane
date: 2026-08-31
tags: [google-sheets, experiments, bhiksha, tradelab, subtraction]
refs: [416daff, tradelab@6d2f4c4, README.md, docs/deploy_runbook.md]
---

# Expired experiments collapse to ordinary Sheet rows

## What We Learned

An experiment-specific admission contract should disappear when the bounded
experiment ends. If the strategy still runs, it becomes an ordinary Sheet row:
the Sheet selects live or shadow, Bhiksha executes through its shared rails, and
TradeLab reads app-owned facts and status.

## Context and Evidence

PDD accumulated an authorization digest, evidence-binding registry, packet
reconciler, provider-overlap floor, inhibition store, runtime consult, and
separate TradeLab cohort/workbook publishers. The Sheet still said `live`, but
hidden state forced the compiled lane to shadow. Twenty-two other shadow rows
were also suppressed when their binding snapshots drifted from current Sheet
option-selection policy.

Removing those control-plane duplicates reduced Bhiksha by 7,064 lines and
TradeLab by 4,020 lines. The real Sheet then compiled 32 deployments with seven
explicit Mala policy kills, zero unexplained coverage loss, and
`release_safe=true`. Bhiksha passed 1,226 tests; TradeLab passed 248.

## When It Applies

Use a special canary contract only while it protects a currently bounded live
admission. Keep immutable receipts as history. Retain special runtime code only
when the experiment still has unique safety semantics that the shared execution
and risk rails cannot express.

## Apply It Next Time

When an operator change appears to require code, first trace:

```text
Sheet row -> compiled plan -> shared runtime -> app facts/status -> analyst
```

If a mutable registry, packet writer, analyst workbook, or experiment-specific
runtime latch sits beside that path, ask whether it still owns a live safety
boundary. If not, archive its receipts and delete the executable seam.

## Dead Ends

Keeping a read-only binding registry looked cheaper than deleting it. In
practice it remained an authorization-adjacent gate: current Sheet policy drift
suppressed valid shadow lanes. A compatibility layer is still a control plane
when it can prevent execution.
