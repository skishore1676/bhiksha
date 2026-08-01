# Paired Exit Edge Lab

The lab has two deliberately separate modes.

1. Historical mode audits whether existing persisted data can support a paired
   comparison. It never estimates paired outcomes. The current event history is
   generally right-censored at the authoritative exit, and legacy/profile
   buckets in the weekly scorecard contain different trades and are confounded
   by entry, contract, time, and lane selection.
2. Prospective mode replays the current profile, legacy mechanics, and the
   Exit Engine V2 six-arm registry from one immutable actual entry and one
   append-only executable quote tape. It admits a case only after every
   compared arm has a terminal modeled fill.

This is observational counterfactual evidence, not causal proof. Per tradelab
ADR-011 it cannot discover or validate other exit profiles.

## Prospective evidence contract

At entry, freeze a cohort containing `cohort_id`, immutable `trade_id`,
`cluster_id`, deployment, symbol, contract, entry timestamp, actual entry fill,
original quantity, both policy configs, evaluator/fill-model versions, analysis
knobs, and quote source/feed lineage. One SHA-256 hash covers that entire
experiment spec. Each quote record must carry provider `quote_at`, local
`received_at`, monotonic `sequence`, provider/cache `source` and `feed`, bid,
ask, last, spread, and derived freshness.

New cohorts also persist additive selection dimensions: selected DTE, absolute
delta, bid/ask spread, fallback policy, runtime mode, and authorization mode.
Older rows read as an empty dimension object rather than being rewritten.

The frozen v2 experiment carries Control plus five named shadow candidates.
Each arm has a canonical policy id, type, and hash. Control retains the trade's
immutable live-policy identity. Variant A uses activation `0.25R`, curvature
`1.5`, and a `0.0R` floor at T1; Variant B differs only in curvature `2.0`.
Common Giveback arms at `0.75R` and allows a `60%` retrace. Safety Stack composes
Variant A with that giveback and selects the stricter floor. Profit Preservation
uses a discrete `0R` floor after a `0.75R` peak. The candidates are built from
the exact Control canonical payload, so unrelated stop, target, time-stop, and
EOD semantics do not drift. Legacy v1 Control/A/B rows remain replayable.

Every candidate receives every captured quote row. Its shadow state is keyed by
`(trade_id, experiment_id, candidate_id)` and carries that candidate's policy
hash, monotonic locked floor, last observation, and revision. Candidates never
share a mutable floor. The report emits quote/provider
timestamps, age, spread, executable bid, current and peak R, candidate and
locked floors, hypothetical stop premium, would-ratchet/would-breach flags, and
the Control decision from the same row. Missing identity/timestamp and
expected-versus-recorded row counts are explicit.

Prospective fixtures must carry that explicit precomputed hash. The loader never
auto-signs an unhashed fixture, because doing so after a settings edit would make
mutable analysis settings look frozen.

The tape must continue after the first actual or virtual exit until all virtual
arms terminate or the cohort is explicitly censored. The recorder consumes an
existing quote cache/feed or an isolated low-priority quota; it must never add
broker calls that compete with protection or exit traffic.

Fill model:

- a trigger at sequence N cannot fill at N;
- a long-option exit fills at the first later-sequence, fresh, non-crossed
  executable bid after configured latency;
- this is a modeled natural-bid fill with no size, queue-position, or slippage
  guarantee;
- midpoint, last, ask fallback, and last-mark imputation are forbidden;
- modeled fills and real broker fills remain separate facts.

Missing bid, stale/crossed/out-of-order/duplicate quotes, sequence gaps, policy
identity, recorder failures, or a tape ending before all arms fill make the
case insufficient.

`ProspectiveQuoteTapeRepository` is a separate experiment store. Quote/state
writes and every `try_*` path retain a 1ms SQLite busy timeout and return
`False` on storage/serialization failure so the
experiment is censored without changing live decision/dispatch timing. It has
no broker imports and never restores or mutates the real profile FSM/order
state. Production integration must still enqueue writes off the money-path
thread. Cohort registration and quote appends are idempotent; conflicting reuse
of an identity or sequence fails closed for the experiment. Orphan quotes and
source/feed transitions are rejected. Censor reasons persist and the repository
can reconstruct a replay case after restart.

The observational `registration_summary` status/report query alone allows a
bounded 250ms SQLite busy interval so it can read through the recorder's brief
schema-initialization lock on slower hosts. A pre-schema read reports an empty
denominator; persistent locks, corruption, and unrelated schema errors still
surface. This readback value has no planner, risk, execution, reconciliation,
order, or broker path.

The sidecar persists candidate state in
`exit_edge_shadow_envelope_state`. Identity, state revision, and locked floor
are fail-closed against regression. This is evidence state only: it is not the
live profile FSM, committed stop state, or an order instruction.

## Commands

Historical eligibility audit against a read-only snapshot:

```bash
PYTHONPATH=src python -m bhiksha.tools.exit_edge_lab \
  --db-path /path/to/bhiksha.snapshot.db \
  --start 2026-07-02 --end 2026-07-31 \
  --output-dir /tmp/exit-edge-history
```

Prospective fixture replay:

```bash
PYTHONPATH=src python -m bhiksha.tools.exit_edge_lab \
  --fixture-json /path/to/paired-tape.json \
  --output-dir /tmp/exit-edge-paired
```

Fixture cases freeze an `experiment` object and use `quotes` entries with
`sequence`, `source`, `feed`, `quote_at`, `received_at`, `bid`, `ask`, and
`last`. The report includes paired delta P&L, holding-window MFE/MAE on
executable bids, both arms' time in trade, sample count, cluster labels, and
explicit censor reasons. Win rate and its Wilson interval are descriptive only.
Directional uplift requires at least eight labeled clusters, positive total and
mean paired P&L, and a positive distribution-free one-sided 95% lower bound on
median cluster uplift.

Residual governance risk: `cluster_id` is immutable once registered, but its
upstream derivation/provenance is not yet standardized. Before using cluster
inference for promotion, define and version the clustering rule (for example,
trading session plus correlated-underlying family) so relabeling cannot change
the inference unit after results are visible.

## Live observational tee (off by default)

Bhiksha can opportunistically feed the prospective repository from option
quotes the runtime already requested for management, protection, or repricing.
This is an inline **tee of completed existing requests**, not a quote cache and
not a new poller. It adds zero broker calls. The runtime thread performs only a
bounded `put_nowait`; SQLite, replay, censoring, and status writes run on a
daemon worker outside the symbol lock and money path.

Eligibility is strict. A cohort is created only when the broker fill payload
has `status=FILLED` and contains `averageFillPrice`/`averagePrice`, positive
`filledQuantity`, and `filledAt` or Public's FILLED-order completion field
`closedAt`, and the deployment carries a named profile plus a valid canonical
`exit-policy.v1` snapshot/id/hash. Plan price, requested quantity, submission
time, `openedAt`, and `lastTradeTime` are never substituted. Every
confirmed-fill attempt—including ineligible or queue-dropped attempts—is
queued for `exit_edge_registration_attempts`, so missing cohorts remain visible
in the denominator. Persistence failures are retained for in-session retry and
surfaced in the health artifact; a permanent storage outage still requires
reconciliation against Bhiksha's authoritative `entry_fill_check` history
before inference.

Quote timestamps are accepted only when Public supplies either its explicit
two-sided `quoteTimestamp`, or **both** `bidTimestamp` and `askTimestamp`. For
the side-specific form, the older side becomes the effective `quote_at`; this
makes the freshness test cover both sides. A missing, malformed, or future
side fails closed. Generic response timestamps, `lastTimestamp`, and trade
timestamps do not prove the age of the bid/ask and censor the cohort. The
recorder assigns a local sequence only after the observation enters its bounded
queue; any active-cohort queue drop permanently censors the tape, so a
synthetic contiguous sequence cannot hide missed observations. An unfinished
cohort found after restart is censored as `restart_gap_unobserved_quotes`.

This tee normally stops receiving marks when the actual position closes. It
keeps the cohort open in case another existing request happens to fetch the
same contract, but it does not add post-close calls. At session shutdown an
unfinished case is explicitly censored as
`no_post_exit_quote_source_session_shutdown_before_virtual_arms_terminal`.
Therefore this integration is useful for opportunistic proof and health
measurement; it is **not** guaranteed complete prospective collection. A
separately budgeted low-priority quote source would require another approval.

Enable persistently on oldmac only after syncing the reviewed commit, at a
session boundary. The installer writes the flag into both the live-start and
watchdog plists and creates an allowlisted, non-secret runtime marker consumed
by `run_bhiksha_job.sh`. That marker preserves the mode for scheduled start,
watchdog restart, Lathi ensure-running, and manual recovery. Generic installs
remove the marker and remain off:

```bash
BHIKSHA_INSTALL_EXIT_EDGE_LIVE_SHADOW_ENABLED=true \
  scripts/launchd/install_bhiksha_launchd.sh install
```

An interactive `export BHIKSHA_EXIT_EDGE_LIVE_SHADOW_ENABLED=true` can be used
for a startup-only smoke, but it does not enable the scheduled launchd job and
must not be treated as deployment proof. After install, inspect both plists,
`artifacts/playbook/runtime_flags/exit_edge_live_shadow.enabled`, and a
scheduled/recovery-context startup snapshot for
`exit_edge_live_shadow_enabled=true` before claiming enablement.

A live session writes the isolated
database to `artifacts/observations/exit_edge_live.sqlite3` and atomic health
readback to `artifacts/observations/exit_edge_live_status.json`. The status
must say `mode=observational_shadow_only`, `enforcement_authority=false`, and
`broker_calls_added=0`. Inspect `observed_quote_timestamp_fields`: at least one
proved lineage (`quoteTimestamp` or `bidTimestamp+askTimestamp`) must be present.
Any other field remains truthfully censored and cannot produce a useful paired
case.

The 2026-07-27 day-one cohorts were censored because the normalizer discarded
Public's side-specific timestamps before they reached the recorder. They remain
immutable censored evidence; this repair does not rewrite their tapes. Sessions
starting from the repaired release preserve and validate the paired Public
fields prospectively.

Disable persistently by reinstalling without the opt-in; the installer removes
both plist environments and the runtime marker. The checked-in config remains
`false`.

Generate the guarded repository report with:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.exit_edge_lab \
  --live-db-path artifacts/observations/exit_edge_live.sqlite3 \
  --output-dir artifacts/observations/exit_edge_readback
```

This report includes the confirmed-fill/eligible/registered denominator.
`inference_eligible` remains false if any eligible registration is missing, any
registered cohort is unfinished or censored, or there are no registered
cohorts. In that state the confidence indicator is forcibly
`live_collection_inference_blocked`, even if the successful subset looks
directionally positive. Missing health readback, any storage failure, or any
recorded observation drop also blocks inference.

## Weekly evidence receipt

The Friday `weekly-trading-decisions` job reads the isolated repository through
the same guarded analyzer and always writes:

```text
artifacts/playbook/reports/exit_policy_weekly_evidence_<week-end>.json
```

An operator replay uses `weekly-trading-decisions --week-end YYYY-MM-DD`; this
keeps a weekend recovery bound to the original Friday instead of silently creating
a second weekly identity for Saturday.

Schema `trading.exit_policy_weekly_evidence.v2` separates packet integrity from
evidence maturity. A valid receipt may truthfully say `not_collecting`,
`awaiting_first_collection`, `stale_collection`,
`insufficient`, `inconclusive`, or
`directional_profile_uplift`. Missing or stale evidence is never rendered as
zero uplift.

The outer `bhiksha.weekly_trading_decisions.v1` packet binds this artifact under
`exit_policy_evidence.bhiksha` and binds its digest-bearing receipt under
`exit_policy_evidence_receipts.bhiksha`. The older singular `exit_edge_*` fields
remain temporarily for compatible readers, but the per-producer v2 binding is the
authoritative TradeLab handoff.

The packet binds the exact reporting cutoff, current-week and cumulative
registration denominators, paired/insufficient/cluster counts, missingness,
Control-versus-candidate descriptive outcomes, health freshness, experiment
identity, W1/W2/W3 mature-cohort counters, additive entry dimensions, the
authorized-canary manifest, and safety invariants. A checkpoint without a
mature cohort is `insufficient_evidence`, never zero uplift. Health older than
12 hours is stale. A
historical rerun cannot consume a health receipt written after its cutoff.

`directional_profile_uplift` is the existing profile-versus-legacy inference;
it is not a candidate promotion gate. Every packet sets
`decision_ready=false` and `automatic_promotion=false`. TradeLab validates the
receipt and presents one compact section in its existing executive brief; it
does not create another report or trading authority.

To disable the scheduled collector, reinstall without the opt-in so the
installer removes both plist environments and the runtime marker. Removing an
interactive environment variable alone does not disable a marker-owned
scheduled context.
