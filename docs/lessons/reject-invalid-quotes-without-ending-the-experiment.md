---
title: Reject invalid quotes without ending the paired experiment
type: pattern
area: exit-edge
date: 2026-08-04
tags: [counterfactual, quote-lineage, evidence]
refs: [src/bhiksha/ops/exit_edge_live.py, src/bhiksha/ops/exit_edge_lab.py]
---

# Reject Invalid Quotes Without Ending the Paired Experiment

## What We Learned

A bad provider observation is not proof that the whole prospective cohort is
invalid. Persist the rejection, omit it from the admissible tape, and keep the
cohort active for a bounded retry. Censor only when continuity is truly lost
(queue/storage/restart) or the session ends before the virtual arms terminate.

## Context and Evidence

The original Exit Edge recorder censored a cohort on its first stale, missing,
or unproved bid/ask timestamp. Twelve confirmed fills produced twelve censors,
zero paired cohorts, and zero continuation calls because deactivation happened
before a fresh quote could arrive. The 2026-08-04 IWM cohort was censored after
41 seconds with `missing_provider_quote_timestamp` and no persisted quote.

The retry-aware collector stores rejected observations in
`exit_edge_quote_rejections`, keeps them out of the contiguous admissible tape,
and reports rejection counts and reasons. Its feed identity is versioned as
`order_manager_reused_quote_with_bounded_retry_v2`, preventing accidental
pooling with the older collection semantics.

## When It Applies

Use this pattern when collection is observational and retry is read-only,
bounded, and isolated from order authority. A queue drop, storage failure, or
restart still censors immediately because the missing interval cannot be
reconstructed honestly.

## Apply It Next Time

Check the durable rejection ledger before diagnosing a censored cohort. If a
provider mark is stale or lacks proved two-sided timestamps, reject and retry;
do not append it to the tape and do not deactivate the cohort. Whenever retry
semantics change, bump the quote-feed identity so experiment hashes remain
reproducible.
