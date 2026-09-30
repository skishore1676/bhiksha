# Evidence collection reliability — 2026-09-29

Keep the existing executor, independent quote observer, SQLite evidence store,
and Sheet report owner. This repair adds no scheduler or trading authority.

- The observer stays resident outside market hours, publishing
  `idle_market_closed` without requesting option quotes.
- Status distinguishes heartbeat, successful poll, provider quote time,
  received quote time, durable append time, and pending writer age. The existing
  watchdog detects stalled in-flight writes as well as a stalled main loop.
- Status snapshots serialize their write/replace operation. Write failures go
  to stderr as well as in-memory health, so a failed status file is diagnosable.
- Registration catch-up freezes the event ceiling before scanning; concurrent
  inserts are picked up on the next pass instead of silently skipped.
- The existing `Exit_Comparisons` publisher includes the collector snapshot.
  A fresh heartbeat without fresh usable quotes is not healthy market evidence.
- Quote lineage/freshness requirements, frozen exit policies and historical
  gap labels stay intact. Restart cannot reconstruct missed option quotes.

Acceptance: targeted missing-history, concurrent-registration, writer-stall,
market-closed, and restart tests; full suite; deployed revision and fresh idle
heartbeat on oldmac; published Sheet readback. A complete regular session of
natural collection remains necessary to demonstrate sustained reliability.
The prior stale heartbeat's exact cause remains unproven; these changes fix
verified blind spots rather than claiming a proven host failure.


## September 30 follow-up

A transient failure to persist a rejected quote permanently censored AMD's
comparison. The independent observer now keeps that cohort active, records the
storage failure in collector health, and continues to exclude the invalid mark.
The next admissible quote still records a continuity gap when the frozen interval
limit is exceeded. Legacy embedded collection is unchanged. Existing censors and
historical tapes are not rewritten. This scoped repair does not resolve oldmac's
host pressure or prove a complete reliable session.
