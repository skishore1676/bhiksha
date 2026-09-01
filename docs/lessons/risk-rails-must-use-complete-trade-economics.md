---
title: Risk rails must use complete trade economics
type: bug
area: live risk and promotion governance
date: 2026-07-16
tags: [risk, partial-fill, pnl, session-veto]
refs:
  - src/bhiksha/risk/risk_manager.py
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

Rail B is deliberately a session-scoped entry veto. It may refuse a live entry
when the complete-economics window is negative, but it may not persist a mode
override or change the next plan. The Sheet remains the sole LIVE/SHADOW
authority; a new session evaluates the same evidence again if the Sheet still
authorizes the lane as live.

## Specifics
- Rail B mirrors the weekly scorecard's complete realized P&L semantics.
- Rail A books confirmed partials on `filled_at` and the final leg on
  `exit_filled_at`; unconfirmed or abandoned legs are never estimated.
- A Rail B refusal is latched only for the running session and emitted as a
  `risk_manager_session_block` event with the priced trade ids and explicit
  `sheet_authorization_changed=false` evidence.
- Persistent mode changes happen only when the operator changes the Sheet.

## Apply It Next Time
Any metric, gate, report, or optimizer that reads realized trade economics must
state whether it is measuring a whole trade or a calendar period. Whole-trade
logic joins all durable legs. Period logic books each leg on its own confirmed
fill date. Add a laddered-trade regression before trusting the result.
