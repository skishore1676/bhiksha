---
name: bhiksha-strategy-exit-review
description: Build Suman's on-demand Bhiksha Excel strategy, signal and exit review from read-only application evidence, with agent-authored inference. Use when asked for this report; does not activate strategies or schedule jobs.
---

# Bhiksha strategy and exit review

Bhiksha owns this skill and its calculation/export scripts. Trading Lead owns
the inference when invoked. No broker calls, arming/Sheet writes, runtime
changes, automatic promotion, or recurring schedule belong to this workflow.

## Build on request

1. Read the repository `AGENTS.md` and [report contract](../../../docs/STRATEGY_EXIT_REPORT.md).
   Use the isolated reporting checkout for code, the live app only for read-only
   databases. Use the requested dates. If omitted, use the last completed
   Monday–Friday week in America/Chicago and state that window in the reply.
   Put private outputs in a fresh run directory outside tracked source.
2. Run the extraction CLI from the contract using the live app's `.venv` Python
   with reporting checkout `src` first on `PYTHONPATH`, then kernel `src`.
   Verify `bhiksha.__file__` resolves into the reporting checkout. Save its
   `report_digest`. Finish with a JSON snapshot and source receipt; missing
   sources are blockers, not zero activity.
3. Read that exact snapshot and author `inferences.json` using the contract's
   schema. Cover every main-report `row_id`. Use plain language:
   - **Assessment:** promising / collecting / evidence blocked / not promising.
   - **Reason:** result, sample size, and important limitation.
   - **Next step:** smallest useful action, or `None`.
   Give the agent room to judge evidence; do not invent thresholds or infer
   promotion from the largest P&L. One-day cohorts and sparse samples usually
   need more evidence. Distinguish execution gaps from a proven application bug.
   Include author and timestamp; bind annotations to the snapshot digest.
4. Attach annotations through `--report-json` (never reread the live stores
   between extraction and inference attachment). Stage both
   `scripts/reports/render_strategy_exit_report.mjs` and
   `scripts/reports/hide_supporting_sheets.py` beside a `node_modules` symlink
   to the configured artifact-tool environment. Run the renderer.
   On oldmac the supplied environment is
   `/Users/sunny/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/`:
   executable `bin/node`, dependencies `node_modules`. Do not install into
   the production app or modify dependency directories. Follow the applicable
   spreadsheet skill's operation marker before authoring.
5. Inspect the verification JSON, previews and saved OOXML. Confirm row 1 is
   the filterable header, row 2 starts data, Date is Central signal/entry cohort,
   Week starting is Monday, Inference is last. Strategy review and Exit pairs are visible;
   Trades, Exit detail and Signal detail remain hidden for agent traceability.
   Check all source trades/signals are retained and dates do not repeat the
   full-window totals. Disclose any unavailable native Excel verification.
6. Return the final Excel and requested window. Deliver to Suman's requested
   destination using the available file route; distinguish transfer success
   from verified destination placement. Do not expose raw private data to
   external recipients or claim a live-promotion decision.

## Evidence semantics

Fees are $0 per Suman's report instruction. Live and shadow recorded P&L and
modeled exits are separate columns. Modeled columns compare a common set of
clean trades for the same frozen experiment. Gaps/unfinished outcomes remain
unavailable, never zero. Captured means a persisted signal decision/outcome,
not a fill. Partial monitoring is not a complete-session denominator.
Counts may repeat across frozen experiment rows: never sum matrix context as
unique trade/signal totals. Detail records and JSON remain the analysis inputs.

This is a sequential script + agent workflow. Lobster is not required and no
automation is installed. Add a routine only after a separate user decision.


## Pair and quote-quality interpretation

Read `pairwise_comparisons` as well as the all-exit common matrix. Each candidate
uses its own matched primary sample; compare different candidates only on a
common trade population. Explain registered, completed, pending and gap counts.
New generation-6 results are sampled estimates (15-second polls, quotes up to
30 seconds old, decisions at receipt). Historical cohorts keep frozen rules.
Use the five-second sensitivity deltas only on their stated common trade set;
show additional sampled pairs separately. A sign reversal/lost advantage is
quote-quality sensitive; unchanged direction alone is not promotion evidence.
Gap-affected diagnostic dollars belong outside comparable totals. Never infer
actual fill precision from modeled next-observation bids.
