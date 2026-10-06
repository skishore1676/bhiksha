# Session entry and reporting repairs — October 6, 2026

Status: development candidate; not deployed. Suman will give separate deployment
authorization after reviewing the completed change and validation.

## Baseline and authority

The isolated development branch is `codex/session-entry-repairs-20261006`, based
on `6b22286b780e854719747bdd1b26e46b0974e3d9`. A read-only oldmac check on October
6 verified the same clean production head. The original Air checkout at
`/Users/suman/code/bhiksha` is preserved.

The authorized scope is source fixes and development tests. No production
database, reports, active plan, Sheet, account funding, credentials, scheduler,
service, order or position is changed by this candidate.

## Completed source changes

- Entry pricing and paper observation share proved provider timestamp lineage.
  Numeric provider timestamps are accepted; local receipt time is never provider
  proof. Supplied invalid or future side timestamps fail closed. Entry pricing
  and modeled entry fills use a shared eight-second freshness threshold.
- A timestamp-only entry rejection may make one additional quote read. Both
  observations and fetch timing are retained. Window, signal and retry guards
  are rechecked; hard price/liquidity, premium, cash and risk limits remain in
  force. A refresh does not grant another signal or an unlimited retry.
- A weekly confirmation keeps its original timestamp. Its retry allowance is
  anchored to the later of confirmation and the authorized session entry start.
  Daily confirmations use the next exchange session's entry start. Delayed
  observation, history repair and restart do not start a new allowance. Frozen
  deployments retain their original execution window.
- Coverage reporting distinguishes observed receipts, explicit data gaps and
  missing lane evidence. It does not infer complete session coverage from one
  false evaluation. Historical startup inventory takes precedence over a newer
  active plan. Confirmation followed by a gate is distinct from no trigger.
  RYG tables label aggregate evaluations and recorded trades explicitly; proxy
  counts are no longer presented as triggered signals or order attempts.
- The September 25 IWM case is an exact-identity reporting exclusion, closed by
  the operator on October 5. Raw ledger facts remain unchanged. Attributed P&L
  is unknown, excluded observations cannot affect outcome statistics, and a
  known eligible subtotal is separate from an incomplete overall total.

Cash sizing continues to size against the configured premium cap and then
reserve settled cash for the full order. Shadow capture continues to require a
later fresh ask touching the resting limit. Neither policy is tuned to force
fills. Modeled shadow exit-cohort registration already exists and is unchanged.

After the retrospective, Suman requested an entry quote-age ceiling between five
and ten seconds. This candidate uses eight seconds, inclusive, as that operator
policy choice. It is not presented as an empirically calibrated optimum. Quotes
older than eight seconds remain stale; missing, unproved and future timestamps
still fail closed. The retrospective's historical five-second counts remain
historical evidence. Exit Edge's separate five-second experiment tape admission
policy is unchanged.

## Historical reporting disposition

The exact excluded case is trade `edb45772-3304-4623-ad91-b6ed1c8f4a59`, deployment
`strategy_market_impulse_all_basket_discovery_iwm_long_live_row_3`, option
`IWM260929C00285000`, broker entry order
`98f8868b-438f-4c46-a509-75605218b52c`, entry timestamp
`2026-09-25T13:54:52.525000+00:00`. A conflicting or incomplete identity does not
match this disposition. The exception classifies a closed operator case; it
does not reconcile its disputed quantities or manufacture exit economics.

The running weekly Markdown watermark is not modified by this candidate.
After deployment approval, regenerate affected reporting through the supported
report path and record this named historical exclusion in the weekly review.
Newer reviewed sessions may then be marked covered without treating the closed
exception as a zero-dollar trade. Any separate unresolved coverage or live
safety problem must still be visible.

## Validation and review

Final validation on October 6, including the eight-second entry policy, passed:

- Full repository suite: **1,491 passed in 24.40 seconds**.
- Focused session-repair, pricing, entry/exit and planner regressions:
  **123 passed**, including 59 session-repair cases. Boundary coverage accepts
  eight seconds exactly, rejects eight seconds plus one microsecond, accepts a
  seven-second planner quote without a refresh, and applies the same ceiling
  to modeled paper fills. Stale-refresh and delayed-risk fixtures use nine
  seconds to continue exercising rejection and reservation release.
- `python -m compileall -q src/bhiksha tests` and `git diff --check` passed.

The full-suite command, run from this isolated checkout, was:

```sh
PYTHONPATH="$PWD/src:/Users/suman/code/mala-bhiksha-kernel/src" \
  /Users/suman/code/bhiksha/.venv/bin/python -m pytest -q
```

Tests use the main checkout's `.venv` Python 3.13.7, with this branch's `src`
first on `PYTHONPATH` and the kernel's `src` second. Import resolution was
verified into the isolated checkout. Kernel revision was
`f4f1223b24e02bd3fc04f393088a508f9731d56c`. The final full-suite run permitted
the existing test's localhost HTTP listener; no broker service was involved.
Supporting logs are saved in the task's `verification/eight-second-full-suite.txt`
and `verification/eight-second-focused.txt`. The original five-second candidate's
logs remain available separately.

Direct final review covered the two-read quote bound, freshness and permission
checks after asynchronous work, reservation release on rejection, immutable
retry anchors, truthful coverage labels, and exact-case exclusion before P&L
and outcome statistics. Session cadence is still unproved where the existing
receipts lack an expected cadence; production quote availability and fill
behavior require observation after a separately approved deployment.

Astra's design review and Sol's implementation turn had already started when
Suman's cost preference changed. Astra finished that one design turn. No
additional subagents or Astra review turns were started afterward. The final
diff received direct Sol review and automated checks; it has no independent
Astra diff review.

## Deployment checklist — only after explicit approval

1. Review the committed diff and recorded test results. Preserve existing
   production changes if the target has advanced since the verified baseline.
2. Stage the exact reviewed source through the repository's normal deployment
   procedure and required tests. Verify imported code and source fingerprints.
   Any runtime restart or plan publication belongs to this separately approved
   deployment, not to development validation.
3. Preview compilation without replacing the live plan. Verify that each weekly
   lane's retry anchor uses its final execution start, frozen intents retain
   their execution contract, and risk/premium/cash controls are unchanged.
4. Regenerate the affected reports after approval. Verify IWM is EXCLUDED with
   null attributed P&L, eligible subtotals are labelled, cumulative results do
   not count the case, and gap/unknown coverage cannot be mistaken for no signal.
   Apply the reporting disposition to the weekly review watermark separately;
   do not rewrite the historical trade ledger.
5. Read back production ownership, source version and effective configuration.
   Observe a natural subsequent session for bounded quote reads, provenance
   fields and retry expiry. Do not place a test order to prove this release.
6. If rollback is needed, restore the previous source/plan through the normal
   authorized procedure. This candidate requires no ledger-data rollback.
