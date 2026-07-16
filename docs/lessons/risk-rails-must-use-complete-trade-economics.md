---
title: Risk rails must use complete trade economics
type: bug
area: live risk and promotion governance
date: 2026-07-16
tags: [risk, partial-fill, pnl, demotion, repromotion]
refs:
  - src/bhiksha/risk/risk_manager.py
  - src/bhiksha/risk/demotion_store.py
  - src/bhiksha/tools/risk_demotion_admin.py
---

# Risk Rails Must Use Complete Trade Economics

## Context
Profile exits rewrite `trade_sessions.quantity` to the residual runner size.
The banked legs live in `trade_partial_fills`. Rail B originally calculated
expectancy from only the final residual, which falsely demoted IWM and
overstated QQQ's loss. The same residual-only helper also fed Rail A.

## What We Learned
A trade-level risk decision must include the final residual plus every
confirmed, non-abandoned partial leg. A daily book decision must instead
attribute each leg to its own fill date: banked partials when they fill and the
residual when it closes.

Re-promotion also needs an evidence boundary. Removing a demotion without a
persisted cutoff lets the same old losing window immediately demote the row
again, so it does not create a real second chance.

## Specifics
- Rail B mirrors the weekly scorecard's complete realized P&L semantics.
- Rail A books confirmed partials on `filled_at` and the final leg on
  `exit_filled_at`; unconfirmed or abandoned legs are never estimated.
- Operator re-promotion preserves the prior demotion and records
  `repromoted_at`; only trades closed after that timestamp count toward the new
  Rail B window.
- The protected command refuses to run while Bhiksha is active, requires an
  exact confirmation, and changes a batch atomically.

## Apply It Next Time
Any metric, gate, report, or optimizer that reads realized trade economics must
state whether it is measuring a whole trade or a calendar period. Whole-trade
logic joins all durable legs. Period logic books each leg on its own confirmed
fill date. Add a laddered-trade regression before trusting the result.
