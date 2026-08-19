---
title: Persist the selected contract for the one bounded DTE fallback
type: gotcha
area: option-selection evidence
date: 2026-08-19
tags: [options, dte-fallback, evidence, reporting]
refs: [src/bhiksha/options/chain_snapshot.py, src/bhiksha/ops/weekly_trading_decisions.py, tests/test_chain_snapshot.py]
---

# Persist the selected contract for the one bounded DTE fallback

## What We Learned

`allow_nearest_after` now means exactly one bounded retry: when the requested
DTE window has no eligible contract, inspect the numerically nearest later
expiry and apply the same delta, open-interest, and spread filters. The
downstream premium and contract-count limits remain unchanged. If that expiry
has no eligible contract, selection ends. The selector does not walk farther
expiries looking for a match.

The attempt receipt and chain snapshot must say whether the primary window or
the nearest-later fallback supplied the selected contract. A genuine no-match
must still produce a terminal attempt receipt.

## Context and Evidence

On 2026-08-03 the older selector could inspect a nearer illiquid expiry and
continue to a farther eligible expiry. That behavior made the effective DTE
bound difficult to reason about and once allowed the selected contract to fall
outside the bounded snapshot.

The 2026-08-19 policy deliberately tightens that behavior. Fallback activates
when the primary window has zero *eligible* contracts, not only when it has zero
contracts. It then evaluates only the nearest later expiry. Snapshot capture
continues to include the requested window, the fallback expiry, and the exact
selected contract. The fact exporter independently verifies that the attempt's
selected symbol matches the trade and has an `is_selected=1` snapshot row;
otherwise it emits `option_selection_selected_contract_not_persisted` and
classifies the fact as `plumbing_invalid`.

## When It Applies

Apply this check whenever `allow_nearest_after` is used or whenever a report
treats a telemetry-persisted flag as proof of exact selection attribution.

## Apply It Next Time

For a suspicious trade, join `trade_sessions.option_selection_snapshot_id` to
`option_chain_snapshot_attempts` and `option_chain_snapshots`. The attempt's
selected symbol, the trade's option symbol, and exactly one selected snapshot
row must agree before the trade is decision-grade.

Do not loosen delta, liquidity, spread, premium, or contract-count thresholds
inside fallback. Operator-owned thresholds stay identical; only the single DTE
window changes, and the terminal receipt records which window was evaluated.
