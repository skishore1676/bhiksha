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

## Public.com Order Contract Audit - 2026-07-16

The follow-up audit used Public's official Get order, Cancel order, Portfolio
v2, History, Place order, and Replace order documentation, then compared those
contracts with the real AMD/NVDA payloads retained on oldmac.

The API is the automation source of truth; the website is an operator projection
of the same asynchronous lifecycle. The resulting rules are:

- DELETE 200 means cancellation requested, not canceled. GET order must confirm
  terminal state before any replacement or protection handoff.
- GET order 404 can be indexing lag while the order is already in the market.
  It keeps the reconciliation hold active.
- Portfolio v2 corroborates visible positions and open orders, but absence from
  one snapshot never proves no fill.
- A visible partial position does not release `pending_entry_reconcile` while
  its entry order remains nonterminal. Bhiksha protects the observed quantity
  and keeps polling the order. It uses the fail-closed
  `live_entry_reconcile_hold` source, so target/profile exits and hard-flat
  submission wait for terminal entry-order truth.
- Pending cancel/replace orders remain attached during position reconciliation,
  preventing duplicate stop or target submission.
- If the observed position quantity grows after protection was armed, Bhiksha
  compares it with the resting close order's remaining quantity. It resizes
  protection only after the old order is confirmed dead; a pending cancel keeps
  the old order attached and raises a runtime issue instead of duplicating it.
- Public's asynchronous replace endpoint is not adopted yet. It becomes a good
  option only after replacement-chain identity and replay are durable.

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

## Public Order Contract Hardening - 2026-07-16

Three adversarial passes followed the official-contract implementation:

1. Preserved the original submitted order quantity while a partial portfolio
   position remains on hold; otherwise a later residual fill could exceed the
   rewritten denominator and remain unresolved forever.
2. Added the fail-closed `live_entry_reconcile_hold` source and quantity-aware
   protection repair, so targets/exits cannot race a residual entry and a stop
   is resized only after confirmed cancellation.
3. Audited emergency and target handoffs. Hard-flat defers while the entry is
   nonterminal, and Public target activation no longer uses cancel-request
   acceptance as permission to submit.

Final verification: focused broker/reconciliation suite 147 passed; full suite
935 passed; Python compile and `git diff --check` clean. Ruff remains unavailable
in the installed environment.
