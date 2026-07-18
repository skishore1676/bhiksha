# Provider Reconciliation Attention

## Decision

Bhiksha owns the meaning and recovery of Public portfolio-reconciliation
failures. Lathi must not infer that a warning color needs Suman. The only
cross-repo paging contract is the explicit boolean `attention_required`.

```text
Public portfolio request
        |
        |-- network/5xx -> 3 immediate attempts with exponential delay
        |-- other error -> preserve last verified snapshot
        v
15-second reconciliation loop
        |-- succeeds -> emit durable recovery evidence
        |-- <5 minutes -> self_healing; fail closed for new live entries
        |-- live money at unknown risk -> needs_human immediately
        `-- unresolved >=5 minutes -> needs_human
        v
reconciliation-supervisor (10 minutes)
        |-- self_healing/recovered -> receipt only
        |-- needs_human -> one deduplicated Beacon alert
        `-- later recovery -> clear alert and Control Tower attention
```

The 15-second loop is intentionally faster than a conventional exponential
backoff because broker position truth is a money-safety dependency. Immediate
retries are limited to errors that are safe to retry. HTTP 4xx responses are
not blindly replayed; the next bounded reconciliation cycle provides the retry
while preserving fail-closed entry behavior.

## State Contract

| State | Meaning | Operator behavior |
| --- | --- | --- |
| `healthy` | No provider failure has been observed in the active window. | Silent. |
| `self_healing` | A failure is active but still inside the automatic recovery window. | Silent; retain evidence. |
| `recovered` | A later portfolio synchronization proved the failure cleared. | Historical report only. |
| `needs_human` | Live money is at uncertain risk or recovery remained exhausted for five minutes. | One deduplicated Beacon alert and Control Tower attention. |

Daily reports keep both observed counts and active counts. Historical warnings
therefore remain auditable without making the report operationally unhealthy
after a later successful synchronization.

## Ownership Boundary

- Bhiksha records attempts, successful synchronization, recovery state, and
  `attention_required`.
- `com.bhiksha.reconciliation-supervisor` owns deduplicated escalation and
  recovery clearing for both entry-order holds and provider reconciliation.
- Session reports describe the day; report color alone is never an operator
  gate.
- Lathi renders app-owned truth. If `attention_required` is present, it is
  authoritative. For older external apps that omit the field, Lathi retains
  its conservative `domain.ok == false` fallback.

## Escalation Rules

Escalate immediately when reconciliation is stale while a live position is
known, because protection and broker truth may diverge. Otherwise allow five
minutes of automatic recovery. New live entries remain fail-closed whenever
the last successful reconciliation exceeds the runtime staleness limit.

Recovered incidents remain in SQLite events, daily reports, and supervisor
history. They leave the active Control Tower attention rail automatically.

## Release Audit - 2026-07-18

Two adversarial passes followed implementation.

Pass 1 challenged recovery truth. Only an explicit
`reconciliation_recovered` event or a later successful `portfolio_sync_ms`
metric may clear a failure. Unrelated runtime metrics cannot clear it, and
observed warning counts remain intact after recovery.

Pass 2 challenged escalation and deduplication. A failure inside the recovery
window stays silent; a condition older than five minutes opens one provider
attention key through the existing reconciliation supervisor; duplicate runs
do not re-alert; and a later successful sync sends one recovery notice and
clears the key. Missing `attention_required` remains fail-safe in Lathi.

A read-only replay against oldmac's July 17 database found the observed Public
portfolio warning followed by successful synchronization about 16 seconds
later. The new report evaluates that session as `GREEN` with one observed
warning, one recovery, zero active warnings, and no attention required.

Verification at release-candidate state:

- Bhiksha full suite: 968 passed;
- Lathi full suite: 369 passed, 1 skipped;
- compile checks and `git diff --check`: clean.
