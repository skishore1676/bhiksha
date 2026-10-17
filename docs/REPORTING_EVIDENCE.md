# Session review evidence rules

Before session-check SQL or comparison analysis, capture a fresh snapshot:

```sh
.venv/bin/python -m bhiksha.tools.monitoring_snapshot \
  --output-dir /tmp/bhiksha-monitor-UNIQUE-CHECK-ID
```

Require `snapshot.json` status `complete`. Point **all analytical database reads**
and `ProspectiveQuoteTapeRepository` instances at the returned copies, never at
the live files. Do not fall back to live analysis if capture fails. Reuse one
snapshot per check; record each database's capture interval and watermarks in
the existing `session_check` receipt. Separate databases and JSON sidecars are
not one atomic system snapshot. Keep broker/status freshness probes separate.

The default copy budget is 3,600 seconds per database, leaving headroom above
the observed 24-minute multi-GB copy at variable background I/O rates. `--timeout-seconds` overrides that
budget; it is checked between copy steps, not a hard filesystem deadline.
Validation and hashing follow after the source closes. During capture,
`snapshot.json` is atomically refreshed with the phase, copy pages, validation
VM steps, or hashed bytes (at most once every five seconds within each phase).
These counters indicate activity, not a precise validation percentage. Slow
advancement is not a stall.
A failed receipt preserves the last progress and incomplete DB bytes. Neither
is eligible for analysis; a new capture uses a distinct directory. The optional
owner `--deadline-seconds` bounds copy, copied-DB validation and hashing together;
it does not enlarge the per-database copy timeout or promise to interrupt a
blocked filesystem call. The backup parent retains its existing hard deadline
and marks a killed capture terminal using its last progress.

Capture uses SQLite online backup, includes committed WAL data and does not
change the live journal mode. WAL sources pin a read view; rollback-journal
sources take small, yielding backup steps. Source connections close before
integrity checking and analysis. Capture still takes brief SQLite reads and I/O;
it does not promise zero scheduling contention. Long analysis cannot retain a
live read lock. Never use a raw `cp`, `immutable=1` on a changing live database,
or a live SELECT cursor around per-cohort analysis. Preserve failed quote writes
and gaps; a snapshot cannot recover unrecorded market observations.

On macOS the capture command first applies `taskpolicy -b` to itself so its
backup and integrity scan yield CPU and disk priority to the runtime. Failure
to apply that policy stops capture. This lowers resource contention; it does
not prove that unrelated source or quote gaps could not occur during capture.

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
