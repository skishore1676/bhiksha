# Weekly execution review — 2026-09-27

Requested reviewer: GPT-6 Astra, high reasoning, read-only. Implementation stays
with the owning agent. Architecture supported with these corrections:

- Resolve branch prices from receipt-bound analyst-packet anchors, not legacy
  scenario fields or prose. Preserve publication and first-admission timestamps.
- Never replay pre-admission bars. Daily confirmation authorizes only the next
  session; use a fresh underlying observation for execution.
- Completed-bar tactical invalidation must bypass the old intrabar/manual guard.
  A branch invalidates independently; sibling reservations remain exclusive.
- Persist scenario consumption and freeze policy for filled positions. Treat
  uncertain revisions explicitly; do not invent cross-pack lineage from tickers.
- Distinguish initial intraday primary management from the author's longer thesis
  horizon and from the three-session alternative-exit comparison.
- Source failure blocks new entries from retained plans too. OFF does not abandon
  owned positions. Report configured versus loaded controls honestly.
- Retire Bhiksha's old projection; do not stop an independent producer without
  establishing all consumers. Preserve Workbench plan and execution lineage.

Operator's follow-up: chart analysis timeframe and entry confirmation are distinct.
Keep the author's rule as baseline; compare earlier entry separately before making
claims about lost alpha. Included in the execution design.
