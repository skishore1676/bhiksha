# Session review evidence rules

Use `signal_evaluation` receipts for evaluation totals and count `signal_decision`
positives separately. Reconcile per-lane totals against the event total before
publishing. Since `ce4fd043` (October 8, 2026), lane coverage excludes decisions;
subtracting positives again understates evaluations. Historical reports can
predate that repair: check the actual totals, rather than applying a blanket
adjustment.

Attribute an entry policy using the startup revision loaded at the fill and its
pricing receipt. October 8 SMH at 09:04 CT preceded the midpoint release at
10:08 CT. Friday October 9 supplies the natural broker proof for the new policy.

A confirmed positive without explained handling requires attention. Report
valid risk, authorization, invalidation, and window blocks separately from
application/provider failures. An unfilled terminal order needs its reason:
expiry/cancellation differs from rejection. An enqueue receipt proves enqueue
at its timestamp; it proves no currently working order, and remains unresolved
at close without terminal handling. False evaluations are not positive
opportunities. Missing bars or cadence are unknown opportunities; never invent
missed signals from them.

`evaluation_runtime_ready` begins the measurable runtime interval after warmup;
`evaluation_runtime_ended` bounds it. Calendar minutes apply only within those
intervals, respecting holidays and early closes. Received source ticks plus
`signal_evaluation` and `evaluation_inactive` receipts distinguish observations,
intentional skips, and missing evaluations. Missing source ticks leave lane
eligibility unknown. Process loss, uninstrumented history, and conditional
manual intrabar polling stay explicit unknowns. Completeness for recorded
runtime intervals is not whole-session coverage, and cadence proof is separate
from historical confirmation-data quality.

Keep raw incomplete Cartographer receipts. Report terminal history diagnostics
separately from gaps blocking waiting arms. Repair requests must reach the
original unresolved close, retain original admission/author rules, and never
skip a missing invalidation or silently rearm. Preserve the first proved
invalidation timestamp. Unavailable history is unknown, not a new confirmation.

The existing `Exit_Comparisons` Sheet distinguishes trades with any eligible
clean pair, all-candidate fully complete cohorts, gap/censored cohorts, and
still-collecting candidates; those counts overlap. `clean_trades` in publication
receipts means any eligible clean pair. It cannot prove an exit winner. Preserve
raw historical reports and add explicit dated corrections to the weekly owner
artifact rather than rewriting their evidence.
