---
title: Persist the selected contract when DTE fallback skips an illiquid expiry
type: gotcha
area: option-selection evidence
date: 2026-08-03
tags: [options, dte-fallback, evidence, reporting]
refs: [src/bhiksha/options/chain_snapshot.py, src/bhiksha/ops/weekly_trading_decisions.py, tests/test_chain_snapshot.py]
---

# Persist the selected contract when DTE fallback skips an illiquid expiry

## What We Learned

`allow_nearest_after` means the selector uses the first later expiry that has
an eligible contract, not necessarily the first later expiry. A bounded chain
snapshot that saves only the numerically nearest expiry can therefore omit the
contract actually selected while still claiming that the snapshot persisted.

## Context and Evidence

On 2026-08-03 a PDD request for 0-3 DTE inspected the 4-DTE expiry, found no
eligible contract, and selected an 11-DTE contract. The attempt row and hashes
named the 11-DTE winner, but the bounded snapshot rows contained only the
4-DTE expiry. The weekly fact exporter consequently treated the observation as
complete even though a reviewer could not resolve the selected contract from
the persisted rows.

The repair keeps selection behavior unchanged. Snapshot capture now includes
the requested window, the nearest later expiry, and the expiry actually chosen
by fallback. The fact exporter independently verifies that the attempt's
selected symbol matches the trade and has an `is_selected=1` snapshot row;
otherwise it emits `option_selection_selected_contract_not_persisted` and
classifies the fact as `plumbing_invalid`.

## When It Applies

Apply this check whenever option selection can scan beyond one fallback expiry
or whenever a report treats a telemetry-persisted flag as proof of exact
selection attribution.

## Apply It Next Time

For a suspicious trade, join `trade_sessions.option_selection_snapshot_id` to
`option_chain_snapshot_attempts` and `option_chain_snapshots`. The attempt's
selected symbol, the trade's option symbol, and exactly one selected snapshot
row must agree before the trade is decision-grade.

Do not change selector thresholds or fallback order to repair this condition;
fix the observational sidecar and quarantine the incomplete historical fact.
