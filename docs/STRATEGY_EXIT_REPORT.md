# Strategy and exit report

This is an on-demand, broker-inert report owned by Bhiksha. It is not wired to
the live runtime, any publisher, arming surface, or scheduler. Keep private
report artifacts outside tracked source. Do not deploy this worktree implicitly.

## Three-step workflow

1. Extract the selected entry cohort and calculate the deterministic JSON feed:

   ```sh
   PYTHONPATH="$REPORT_WORKTREE/src:$KERNEL_ROOT/src" "$BHIKSHA_ROOT/.venv/bin/python" \
     -m bhiksha.tools.strategy_exit_report \
     --db-path "$BHIKSHA_ROOT/bhiksha.db" \
     --exit-db-path "$BHIKSHA_ROOT/artifacts/observations/exit_edge_live.sqlite3" \
     --start YYYY-MM-DD --end YYYY-MM-DD --output "$REPORT_DIR/report.json"
   ```

2. The agent reads that feed, writes a separate inference JSON, then attaches it
   **without rereading the live stores**:

   ```sh
   PYTHONPATH="$REPORT_WORKTREE/src:$KERNEL_ROOT/src" "$BHIKSHA_ROOT/.venv/bin/python" \
     -m bhiksha.tools.strategy_exit_report --report-json "$REPORT_DIR/report.json" \
     --inferences "$REPORT_DIR/inferences.json" --output "$REPORT_DIR/report.json"
   ```

   Inference input has `report_digest` from the extraction CLI, `author`,
   `written_at` and `rows`. Each row has `row_id`, `assessment`, `reason`, and
   optional `next_step`. Assessments are exactly `promising`, `collecting`,
   `evidence blocked`, or `not promising`. Missing next step becomes `None`.
   An unknown/duplicate row ID or mismatched evidence digest is rejected.
   Interpretation is agent-authored; the exporter does not recommend live
   promotion. Names, figures, assessments and optional actions must be supported
   by that exact evidence snapshot.

3. Stage `scripts/reports/render_strategy_exit_report.mjs` and
   `scripts/reports/hide_supporting_sheets.py` in a disposable
   rendering directory with a `node_modules` symlink to the configured
   `@oai/artifact-tool` dependency environment. Execute:

   ```sh
   "$ARTIFACT_NODE" "$RENDER_DIR/render_strategy_exit_report.mjs" \
     "$REPORT_DIR/report.json" "$REPORT_DIR/Bhiksha-Strategy-Exit-Review.xlsx"
   ```

   The renderer exports one workbook, renders every worksheet, validates the
   header/table contents, and preserves detail records before saving.
   Review the previews and error scan before delivery. Native Excel execution
   is a separate verification, not implied by successful export.

## Meaning and boundaries

- Dates are Central calendar dates; detail timestamps are typed UTC Excel
  dates, with UTC named in their headers.
- Strategy/version/source/symbol/direction/entry/lane are from historical
  startup receipts. A current plan never relabels an older trade. Full frozen
  deployment hashes allow consolidation only when the contents are identical.
  Different exit experiment hashes still produce separate comparison rows.
- Fired means unique recorded positive signal identities, not proof of every
  upstream opportunity. Captured means a persisted decision or outcome; fills
  are independent trade-store evidence. Retry transitions do not multiply
  these identities. This deliberately differs from the existing Sheet's
  filled-as-captured definition and does not change that Sheet.
- Evaluation receipts establish partial coverage, not a complete monitoring
  denominator. Without evaluations or positive receipts, signal counts are
  Unknown. Zero means no positive signal was recorded in the observed evidence.
- The matrix uses daily signal/entry cohorts; Date and Monday Week starting
  are filterable typed dates. It includes observed no-signal configurations,
  not invented unobserved daily inventory. Recent dates are sorted first.
  Both visible tabs, Strategy review and Exit pairs, begin with column names at row 1 and data at row 2; it has
  no title or summary rows. Three detail tabs are hidden, not deleted.
  The default window for the
  first artifact was the last completed trading week; there is no scheduler.
- Live and shadow recorded outcomes are separate columns. P&L math reuses the
  application's partial-fill accounting and historical exclusions. Open
  outcomes do not become full closed-trade results. Missing eligible economics
  blocks the group's recorded P&L aggregate instead of producing zero.
- Modeled exit columns all use the same intersection of clean trades within
  that frozen experiment. Exit pairs separately shows each challenger against its primary on matched trades,
  even when another arm is unfinished. Its pair populations may differ; do not
  rank challengers across different samples. Individual outcomes remain in Exit detail. Persisted censors, gaps and
  unfinished arms cannot become clean wins. Legacy non-named evaluators are
  retained as issues and are not pooled with named-exit experiments.
- Fee assumption is explicitly zero. Modeled execution prices still use the
  frozen next-observation-bid evaluator and its latency constraints.
  This does not alter the existing Sheet's stress-cost assumption.
- Matrix funnel/actual counts can repeat across different frozen exit sets.
  Headline trade/signal totals use unique detail records, never summed matrix
  context. Report row IDs and the source receipt support traceability.
- Reads use SQLite `mode=ro`. The trading store has a bounded read transaction;
  exit cases are read sequentially from a separate store. There is no atomic
  cross-store snapshot. Record this limitation; do not claim broker
  reconciliation or an exact reconstruction of every historical ledger state.
- Quote replay is bounded at the window end. Later final/partial exit fills
  are excluded from the window's terminal economics. This remains an entry
  cohort view, not a cash-flow-by-exit-date statement.

The repository-owned skill is
`.agents/skills/bhiksha-strategy-exit-review/SKILL.md`. Trading Lead receives
this on-demand workflow and owns subsequent agent inference. Orchestration
may transport it, but calculations and evidence ownership stay here. No
Lobster workflow or recurring job is needed or installed.


## Sampled measurement, generation 6 (authorized 2026-10-11)

New independent-observer cohorts freeze a 15-second regular poll cadence,
30-second maximum provider quote age, 65-second observable-time gap limit,
and receipt-time exit decisions. The next eligible later observation supplies
the modeled bid fill. This is a sampled estimate, not proof of execution or of
all intrapoll stop/target crossings. Live entry and execution freshness rules
remain five seconds. Existing cohorts retain their original specification,
provider decision clock and freshness limit; they are never re-signed or pooled
with the new generation.

The existing observer may make one extra batch of at most 20 stale/missing
contracts after two seconds, rotating candidates across cycles. That refresh
has a ten-second deadline within the existing 45-second total cycle budget.
Invalid timestamp lineage and prices stay excluded; no order path is added.
Per-batch receipt timestamps prevent later batches from aging earlier responses.

Strategy review keeps all-exit common-sample totals. Exit pairs shows registered,
completed, gap-affected and pending pair counts, primary/candidate P&L and delta.
Gap and pending counts can overlap. A later gap in a still-running arm does not
invalidate an already completed pair. Diagnostic continuation P&L is available
only in Exit detail and never contributes to comparable totals.

The on-demand report also replays the saved tape at a five-second age limit,
using the same decision clock and reconstructing gaps after filtering. It does
not fetch historical quotes or write this derived tape to production. Strict
versus sampled deltas use the intersection of clean pairs under both limits;
additional sampled pairs are disclosed separately. A sign reversal or a lost
positive advantage is labeled sensitive to quote quality. Same direction is
not a significance claim. An unavailable strict result remains unavailable.
The strict replay may be unfinished when the original collection has stopped;
that limitation is not filled with a synthetic price.
