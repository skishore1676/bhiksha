# Entry Reconciliation Autonomy

## Decision

Bhiksha should reconcile entry-order uncertainty without operator involvement
whenever broker truth permits a deterministic action. Human attention is the
last fallback, not part of the normal order lifecycle.

The runtime and the observer have deliberately separate responsibilities:

```text
Public order/portfolio APIs
        |
        v
Bhiksha runtime reconciliation (~15s)
        |-- terminal zero fill -> close attempt, release cash, re-arm lane
        |-- terminal fill      -> recover actual quantity, protect same pass
        |-- ambiguous state    -> preserve hold, block only affected lane
        v
SQLite lifecycle/events
        |
        v
reconciliation-supervisor (10m)
        |-- transient/recovered -> receipt only
        |-- unresolved >5m      -> one deduplicated Beacon gate + Tower waiting_you
        `-- later recovery      -> one recovery notice, clear Tower gate
```

Telegram and Control Tower never participate in broker reconciliation. A slow
or unavailable presentation surface cannot delay order polling, cash release,
or protection.

## State Contract

| State | Meaning | Operator behavior |
| --- | --- | --- |
| `healthy` | No active entry reconciliation hold. | Silent. |
| `self_healing` | Hold is younger than five minutes; the runtime is polling broker truth. | Silent; receipt only. |
| `recovered` | A zero-fill attempt was released or a real fill was recovered. | Included in scheduled reports; no interruption. |
| `needs_human` | Hold remained ambiguous beyond five minutes. The affected deployment stays fail-closed. | One deduplicated Beacon gate and a Control Tower `waiting_you` unit. |

Successful zero-fill releases are order attempts, not trades. They must not
enter trade counts, P&L, open-position counts, or protection statistics.

## Durable Evidence

The supervisor writes:

```text
artifacts/playbook/reconciliation_supervision/latest.json
artifacts/playbook/reconciliation_supervision/history.jsonl
```

The latest receipt carries the active fingerprint plus the individual order
keys already alerted. A partial recovery does not re-alert surviving holds;
only a newly stale order creates another interruption. The append-only history
preserves each observation and alert outcome.
The launchd runner also projects the latest result through
`artifacts/playbook/launchd/latest_status.json` for Lathi Control Tower.

## Schedule And Escalation

`com.bhiksha.reconciliation-supervisor` runs every ten minutes from 08:30
through 15:00 CT on trading days. The runtime itself continues reconciling at
its normal higher frequency. A hold becomes human-actionable after five
minutes, so its first external observation occurs no later than the next
ten-minute supervisor tick.

The alert is actionable and explicit: the listed deployment is blocked, no
duplicate entry will be placed, and Public order state must be verified before
manual release or retry. The same order fingerprint is not re-alerted.

## Public.com Follow-Up

No new broker-state inference is introduced by this change. A second audit
should map Public.com's website and API order states, cancellation transitions,
partial-fill presentation, history latency, and portfolio/order consistency.
Only then should Bhiksha consider shortening holds through additional broker
corroboration. Any such change is a money-path change and requires the normal
multi-round adversarial audit before deployment.

## Release Audit - 2026-07-16

Two adversarial review passes were run after the first green full suite.

Pass 1 found that a failed Telegram delivery could be mistaken for a legacy
successful alert and suppress its retry. The receipt now distinguishes a
missing legacy field from an explicit empty alerted-order list; a failed send
retries and does not open a recovery-notice obligation.

Pass 2 found that the launchd envelope did not expose the nested reconciliation
alert at its standard top-level `alert` field. The domain state was correct but
Control Tower transport health would have said `not_attempted`. The envelope
now carries the same alert receipt at both the supervision and standard
transport surfaces.

The independent Claude review attempt could not run because the development
Mac's Claude CLI was not logged in. No Claude output was accepted as audit
evidence. The two review passes above were instead pinned with executable
regressions and rerun through the full suite.

Verification:

- focused reconciliation/execution/reporting/status suite: 177 passed;
- full repository suite after both fixes: 918 passed;
- Python compile checks and `git diff --check`: clean;
- `ruff`: unavailable in the installed environment;
- read-only replay against the 2026-07-16 oldmac database snapshot: GREEN,
  zero live trades, zero live open positions, two `released_no_fill`
  recoveries (AMD and NVDA), and no human attention required.
