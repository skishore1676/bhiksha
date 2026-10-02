# Evidence collection reliability — 2026-09-29

Keep the existing executor, independent quote observer, SQLite evidence store,
and Sheet report owner. This repair adds no scheduler or trading authority.

- The observer stays resident outside market hours, publishing
  `idle_market_closed` without requesting option quotes or repeatedly scanning
  the databases. See the October 2 session-window correction below.
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


## October 2 connection ownership

The resident observer exhausted its file descriptors overnight: the sampled
process held 10,348 file entries, including 3,418 exit-store entries, and status
writes repeatedly failed. SQLite transaction contexts did not close connections.
The repository now closes every read/write connection in a finally block and
registration scans explicitly close their read-only event connection, while
preserving commit/rollback and read-only semantics. Regression tests retain
connection references so garbage collection cannot conceal a leak. Restart restores
collection, but this morning's missing quotes remain gap-affected evidence.

## October 2 session window

One existing launchd owner remains resident; no new scheduler is needed.
Ten minutes before the regular exchange open (normally 08:20 CT), it starts
saved-comparison recovery and registration catch-up. During the session it polls
every 15 seconds. At the exchange close it stops new requests, drains queued
facts, retains unfinished swing comparisons, then idles. A cold overnight start
does not initialize or replay SQLite; an already-warm owner keeps its state for
the next session. Off-hours work is limited to a status heartbeat each minute and
the existing watchdog. An empty observer writer waits instead of spinning.

The existing XNYS exchange-calendar dependency supplies holidays, daylight-saving
changes and early closes. This preserves the regular-equity observation window;
it does not extend the experiment into additional options trading hours.
Continuity accounting uses the same session boundaries, so closed market time
cannot create a quote gap. Existing stored quotes, gaps, censors and frozen
policies are not rewritten. Sheet reporting remains with the existing publisher.

Tests cover cold off-hours startup without a database, pre-open recovery,
collection, queued-write drainage, next-session resumption, holidays, weekends,
early closes and continuity across closed intervals. Natural overnight-to-open
proof remains a subsequent session check, distinct from these tests.
