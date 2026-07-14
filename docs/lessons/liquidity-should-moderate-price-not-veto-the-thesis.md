---
title: Relative option liquidity should moderate entry price, not veto a tested thesis
type: decision
area: option selection and live entry execution
date: 2026-07-13
tags: [open-interest, option-chain, patient-entry, google-sheet, repricing]
refs:
  - src/bhiksha/options/selectors.py
  - src/bhiksha/execution/pricing.py
  - src/bhiksha/execution/supervisor.py
  - docs/lessons/sheet-is-the-operator-control-surface.md
---

# Relative Option Liquidity Should Moderate Entry Price, Not Veto A Tested Thesis

## Context
SMH produced valid strategy signals but its eligible contracts often sat just
outside a fixed spread threshold. A percentile OI idea existed, but a relative
liquidity measure used as another hard selector gate would still suppress the
backtested thesis and would be unstable as the chain composition changed.

## What We Learned
Keep a low absolute OI floor and a hard spread ceiling as safety gates. Use the
selected contract's OI percentile within the requested symbol/type/DTE cohort
to moderate how far a limit order moves from bid toward ask. Relative liquidity
then changes execution patience rather than deciding whether the signal exists.

## Why / When It Applies
This fits tested intraday strategies where several minutes of patient price
discovery are acceptable. It does not fit emergency exits or strategies whose
edge depends on immediate entry. Missing percentile evidence must become
bid-only, never an implicit permission to chase.

## Specifics
The Sheet controls an opt-in ladder through `execution_overrides`: initial
`entry_pricing_spread_fraction`, OI scaling, reprice checkpoints and fractions,
and a final cancel deadline. Fraction `0` means bid, `0.5` means mid, and `1`
means ask. Existing lanes retain their legacy global pricing unless explicitly
overridden.

Each replacement still rechecks quote quality, cancel/status races, broker
preflight, cash availability, and the lane's maximum trade premium. That last
check matters because quantity was sized at the cheaper initial limit; without
it, a later replacement could exceed the configured premium cap.

## Apply It Next Time
When a chain gate suppresses a sound thesis, first separate safety vetoes from
execution-quality evidence. Preserve hard sanity bounds, turn relative quality
into price or patience, expose the policy in the operator Sheet, and record the
effective value on every order attempt.
