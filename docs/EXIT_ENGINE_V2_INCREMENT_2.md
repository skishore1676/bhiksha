# Exit Engine V2 Increment 2 — Bhiksha Implementation

This document is the operating handoff for the second Bhiksha increment. The
cross-repository product direction remains owned by TradeLab. Bhiksha owns
broker-safe execution, durable state, raw observations, and its weekly evidence
receipt.

## What exists

| Surface | State | Authority |
| --- | --- | --- |
| Six-arm shadow registry | Implemented | Observational only |
| Control, Variant A, Variant B | Implemented | Observational only |
| Common giveback, Safety Stack, Profit Preservation | Implemented | Observational only |
| Additive DTE, delta, spread, fallback, and runtime cohort dimensions | Implemented | Evidence only |
| W1/W2/W3 maturity counters | Implemented | Evidence only; never a promotion verdict |
| `risk.max_contracts` | Implemented | Live sizing limit when configured |
| Safety Stack live canary seam | Implemented, default off | At most one active deployment |
| Durable stop-ratchet handoff | Implemented | Broker effect only when the exact canary is armed |
| Guaranteed post-exit quote continuation | Not implemented | Existing quote tee remains protection-priority and broker-call neutral |

The fixed shadow universe is:

1. `control`: the immutable current policy;
2. `variant_a`: dynamic envelope with curvature `1.5`;
3. `variant_b`: dynamic envelope with curvature `2.0`;
4. `common_giveback`: arm at `0.75R`, then allow a `60%` retrace;
5. `safety_stack`: the stricter of Variant A and common giveback; and
6. `profit_preservation`: move the floor to `0R` after a `0.75R` peak.

Legacy three-arm experiment rows remain replayable. New cohorts freeze the v2
registry and reject a self-resigned or incomplete candidate catalog.

## Default live state

No canary is armed by these code changes. The defaults are:

```text
risk_envelope_live_mode = off
risk_envelope_live_candidate_id = null
```

The only permitted treatment is `safety_stack`. A manifest cannot arm it unless
all of these are true:

- profile exits already drive the live approval-gated route;
- the deployment is not shadow-only;
- selection is strict with the exact configured window `dte_min=4`,
  `dte_max=7` (subwindows are rejected);
- `risk.max_contracts=1`; and
- the base premium limit is exactly `$2,000`, with the canary cap exactly
  `0.20`, producing a receipted effective limit of `$400`;
- authority names the exact deployment
  `strategy_market_impulse_all_basket_discovery_iwm_long_live_row_3` and symbol
  `IWM`;
- authority carries aware `start_at`/`expires_at`, authorization id, exact
  active-plan id, and rollback action
  `disable_canary_restore_control`;
- the legacy `stop_to_breakeven_after_r_multiple` dial is disabled; and
- the complete active plan contains no other enabled canary.

At active-plan compile time Bhiksha computes
`risk_envelope_authorization_fingerprint`, a canonical hash of the stable
canary authority and execution inputs. The hash deliberately excludes itself,
the volatile plan generation time, and the later startup-config digest. The
runtime recomputes that authority fingerprint and then freezes both it and the
actual runtime-generated `config_fingerprint` at fill. This gives a
constructible plan-to-startup binding without asking an operator Sheet row to
predict the digest of the startup document that the row helps create.

The runtime also checks the actual selected DTE from frozen entry provenance,
the one-contract position, pre-T1 state, an existing proved protective stop,
Public's explicit two-sided `quoteTimestamp` or the validated
`bidTimestamp+askTimestamp` pair, an exact quote/position option-symbol match,
quote age for both sides, two-sided spread, live position source, authorization
time window, and frozen active-plan/startup bindings. For the paired form, the
older side is authoritative; either missing, malformed, stale, or future side
suppresses the ratchet. Generic and last-trade timestamps are never accepted.

## Broker-proved ratchet contract

A floor calculation is not a stop. The live treatment advances durable
`locked_floor_r` and `committed_stop_price` only after this sequence:

1. persist an idempotent `stop_ratchet` action intent with prior and requested
   stop facts;
2. broker-preflight and SELL-side snap the requested stop to the actual tick,
   then persist, submit, and prove that same canonical price;
3. cancel the prior stop and prove it terminal with explicit zero fill;
4. submit the replacement with the intent key as client order id;
5. GET the exact SELL/CLOSE STOP and prove working status, contract, remaining
   quantity, and stop price; and
6. commit the new durable floor and resolve the intent.

Terminal readback is classified as full/partial fill, explicit dead-zero-fill,
or ambiguous. Any fill closes/reconciles this one-contract canary and can never
trigger a restore. Only explicit dead-zero-fill can start one deterministic
restoration of the prior stop, and that restore identity is persisted before
submission and bound after acknowledgement. A missing fill quantity, pending
cancel, 404, timeout, wrong identity, or other ambiguous response does **not**
claim protection or submit a competing action: the intent remains open, the
trade becomes `STATE_DEGRADED`, and restart resumes from its durable
prior/replacement/restore stage.

Any such handoff safety violation also writes a durable deployment rollback
latch. The latch has deliberately asymmetric behavior:

- **new entries:** blocked for that deployment for the rest of the session and
  after process restart;
- **the open trade:** retained and reconciled against the exact prior,
  replacement, or restore STOP identity;
- **new treatment on the open trade:** no further canary ratchets are started;
  and
- **hard flat/emergency flat:** remains authoritative, but waits until the
  ratchet handoff is proved non-overlapping or its fill is reconciled.

Rollback therefore does not abandon a position or blindly restore Control. It
stops adding treatment while preserving the safest broker-proved protection
available. A later-working replacement or restore is first adopted into the
position tracker, trade record, and lifecycle state; only then may its durable
intent resolve.

The latch is visible without changing state:

```bash
python -m bhiksha.tools.risk_envelope_rollback_status \
  --db bhiksha.db
```

Both the session manifest and weekly evidence override a statically valid
canary with `disarmed_rollback_latched`, including the durable reason and
`latched_at`. There is intentionally no automatic clear and this readback
command has no mutation mode. A reset/re-arm is a separate money-path action:
first disarm the canary in the control plane, stop the runtime after flat,
obtain a new explicit operator authorization for the exact latch reset, record
who approved it and why, clear it through a separately reviewed admin change,
compile a fresh bounded authorization window/fingerprint, and only then restart
and prove the new session manifest. Editing dates or restarting the process
never clears the latch.

## Evidence and weekly review

The weekly receipt schema is `trading.exit_policy_weekly_evidence.v2`. It reports:

- all five candidate-versus-control descriptive results;
- each cohort's runtime policy hash, configured DTE min/max/fallback, runtime
  and authorization, symbol and cluster, plus actual selected DTE, delta, and
  liquidity;
- authorized live-canary manifests separately from mathematical candidates;
- W1, W2, and W3 mature-cohort counts; and
- `insufficient_evidence` when a checkpoint has no mature observations.

Both weekly and cumulative summaries always carry integer
`eligible_observation_count`, `paired_count`, `terminal_paired_count`,
`post_exit_complete_trade_count`, and `right_censored_trade_count`. Cohort DTE
bounds remain integers, including zero, and missing dimensions carry structured
source-data reasons. Reaching 21 days alone cannot produce
`week3_economics`: at least one complete terminal pair, complete post-exit
evidence, zero right censoring, and all six paired candidates are required.

Maturity is not causality and is not promotion authority. Every packet keeps
`decision_ready=false` and `automatic_promotion=false`; an operator decision
and a candidate-specific gate remain required.

### One-contract interpretation

This first live canary is intentionally capped at one contract while the shared
TREND_CONTINUATION core still allocates `60%` at T1. Integer execution therefore
turns the T1 partial into a full one-contract exit. The canary measures only
pre-T1 Safety Stack protection and cannot exercise T2 runner economics. Do not
interpret its live outcomes as evidence for the full T1/T2 ladder; runner
economics remain shadow evidence until a separately approved design changes
the quantity/allocation contract.

The recorder still consumes only quotes already requested by the runtime. It
adds zero broker calls and may be right-censored after the actual exit. Until a
separate poller proves explicit quota accounting and zero competition with
protection, reports must preserve
`no_post_exit_quote_source_session_shutdown_before_virtual_arms_terminal`
instead of filling missing marks.

Paper-shadow and dry-run positions do not own broker protection. They therefore
never hydrate durable live exit state, restore broker stops, enter
`STATE_DEGRADED`, or emit `exit_state_degraded_protection` /
`native_exit_blocked_state_degraded`. They continue through the in-memory
profile evaluator and paper-close ledger, including the existing synthetic
`DRY_RUN_STOP` used for simulation; that id is never a broker order. Live
positions retain the complete durable recovery and degradation behavior above.
Weekly reporting must not reclassify paper positions as live protection
incidents.

Daily reports retain those historical paper events under
`suppressed_shadow_runtime_issue_counts` and exclude them from status /
operational-health counts. The Friday facts export re-evaluates an older daily
JSON from immutable database facts when that JSON predates this classification,
then publishes the excluded total separately as `shadow_diagnostic_count`.
The 2026-07-27 database readback contains 16,943 paper-shadow
`exit_state_degraded_protection` events and one live event; the former remain
auditable diagnostics, while the live event remains an operational issue.

## 2026-07-28 canary infrastructure incident

The first natural IWM Safety Stack canary exposed an application identity bug,
not strategy evidence. Public's confirmed average fill was `0.9798`; the
reconciled trade/position ledger represented the same fill as `0.98`. Durable
restart hydration used exact float equality, falsely classified the position as
`runtime_state_identity_mismatch`, and closed profile/native exit authority
while the proved broker STOP remained in place.

This trade is infrastructure-invalid for Dynamic Risk Envelope evaluation. It
must not support acceptance, rejection, or expansion because the candidate did
not receive a valid execution trial.

The repair contract makes `trade_id`, deployment id, and option contract the
identity authority. Entry premium remains corroboration: invalid values fail
closed, and two valid prices represent the same fill only within the half-cent
envelope created by rounding a sub-cent average to the nearest option cent.
The precise broker fill remains the durable economic seed; it is not rewritten
to the rounded ledger value. The separate stale-ladder backstop retains its
existing greater-than-10% rule.

The money-path repair is commit
`78725a6f87fae660e51f7d8703940b27f57ce4f4`. During the required after-hours
oldmac suite, one pre-existing shadow-recovery test proved wall-clock
dependent: its four-day-old synthetic position reached the real end-of-day
flat gate. Commit `cdfc105fc0f3655c759bd1adddd406c2ae75c359` changes only
that test setup to disable EOD flat and use a current synthetic entry; it does
not change runtime code.

Release validation is complete:

- local source, `origin/main`, and oldmac first converged on `cdfc105`; both
  local and oldmac complete suites passed (`1082 passed`);
- the first adversarial design review rejected a broad 10%/`$0.25` tolerance
  because it could revive a stale low-premium post-partial ladder; the
  corrected half-cent rule passed the original exploit replay, an independent
  delta audit, and the exhaustive nearest-cent grid;
- the 2026-07-28 post-15:00 CT gate proved a clean oldmac checkout, a stopped
  process, zero Public positions, zero non-closed database trades, and zero
  rollback latches before deployment;
- the bounded after-hours restart started PID `38892` at
  `2026-07-28T20:29:17.403490+00:00` while every live lane was
  `time_window_blocked`;
- fresh manifest
  `artifacts/playbook/session_manifests/session_manifest_2026-07-28_c61405d3e2a75974.json`
  binds code commit `cdfc105`, clean source, config digest
  `c61405d3e2a75974`, active plan
  `active_plan_2026-07-27_exit_engine_v2_iwm_canary`, and exactly one armed
  `IWM` `safety_stack` canary with matching authorization fingerprint
  `dd4f0a1454d91605d9390ff8dfb13d2733032861182272ea3cf7975a00ed3f84`;
- only startup and runtime-metric events were written after the deployment
  baseline; there were zero submission, fill, exit-action, partial-scale,
  stop-ratchet, or order-placement effects; and
- the bounded process was then stopped. Final readback again proved no
  Bhiksha process, zero Public positions, zero non-closed trades, zero rollback
  latches, and both launchd jobs not running with the exact active-plan id and
  `BHIKSHA_EXIT_EDGE_LIVE_SHADOW_ENABLED=true`.

This is deployment and stop/start robustness proof, not a manufactured strategy
observation. The 2026-07-28 IWM trade remains infrastructure-invalid and
censored; the next natural eligible trade is the first clean post-repair
strategy sample.

## Next-agent checklist

Before changing stage:

1. Read this file, `docs/EXIT_EDGE_LAB.md`, and
   `docs/lessons/protective-stop-ratchets-need-proved-handoffs.md`.
2. Inspect the current active plan and session manifest. Code capability is not
   proof that a canary is armed.
   For the bounded canary, also prove launchd live-start and live-watchdog carry
   the exact stable `BHIKSHA_ACTIVE_PLAN_ID` authorized by the Sheet row; a
   date-derived plan ID will change across days and disarm or reject the plan.
3. Confirm the runtime defaults still arm zero canaries.
4. Inspect the active-plan sync log for the DTE/fallback/max-contract fields
   and every operator-supplied risk-envelope authority field, then prove the
   compiled active-plan authority fingerprint equals the startup snapshot
   authority fingerprint. There is no operator-supplied startup digest.
5. Run focused canary, persistence, recovery, Exit Edge, and weekly tests, then
   the complete suite.
6. Deploy only after the normal post-flat boundary and read back source SHA,
   oldmac SHA/tree, flat broker/database state, launchd state, generated session
   manifest, and weekly evidence schema.
7. If arming a treatment later, change exactly one approved active-plan row,
   recompile, prove its session manifest says `safety_stack`, strict `4-7 DTE`,
   `max_contracts=1`, `state=armed`, and matching plan/startup authority
   fingerprints, then monitor the action-intent, rollback latch, and
   broker-order readbacks. Expired or not-yet-valid authorization must render
   disarmed/safety-blocked. Never infer authority from the shadow registry.

Audit runs are performed only when Suman explicitly requests them.
