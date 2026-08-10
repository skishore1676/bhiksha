# Chart-scenario shadowing

This lane observes validated `morning-market-scenario-selection-shadow.v1`
packets. It is an experiment receipt path, not an execution path.

## Zero-order boundary

The chart-scenario package has no import or capability path to an order manager,
broker client, order submission/cancellation, reconciliation, the live-plan
compiler, or the live execution supervisor. Its quote input is one of two
sealed, data-only adapters: in-memory immutable snapshots or a persisted JSON
snapshot export. Arbitrary callbacks and duck-typed quote providers are
rejected before observation. Every receipt and every event records
`broker_effect_count=0`; a synthetic entry or exit is a mark observation and
never a fill.

Do not add this lane to `active_plan.json`, `manual_entry`, `active_strategies`,
or a broker service. The existing live plan is neither read nor written by
this package. The only scheduled and Sheet effects allowed are the dedicated
Bhiksha shadow coordinator and exact-key projection to `Chart_Scenarios_v1`.

## Artifacts and state

The default experiment-only paths are:

```text
artifacts/chart_scenarios/active_shadow_plan.json
artifacts/chart_scenarios/active_shadow_plan.receipt.json
artifacts/chart_scenarios/shadow_events.sqlite3
```

Every output, receipt, and SQLite path is resolved and required to remain under
an `artifacts/chart_scenarios/` directory. This prevents a caller from pointing
the installer or repository at `artifacts/playbook/active_plan.json` or another
runtime artifact.

The input bundle must carry the exact kernel hashes for its component manifest,
chart evidence, candidate pool/candidates, arm selections, and scenarios. It
must use the v1 chart-scenario schema, `authorization_mode=shadow`,
`source_type=chart_scenario_experiment`, and
`trigger_version=market-context-trigger.v1`.

The bundle must contain both `chart_deterministic` and
`chart_agentic_rerank` arms. It must also carry an exact
`exit_policy_registry` keyed by exit
profile. It must cover the union of every scenario's compatible profiles. Each
selected scenario embeds the same canonical policy material as its selected
registry entry, and its policy ID, schema version, and hash must match. Bhiksha
does not supply target, stop, hard-flat, giveback, or profile-selection
defaults; missing, unresolved, or mismatched economics fail validation.
Every management-policy field must be physically present in the input, even
when its value is `null` or the kernel would otherwise provide a default.
Unsupported risk-envelope/protective-floor semantics fail closed.
The typed `invalidation_condition` is setup cancellation only and is evaluated
only before synthetic entry. Once entry is established, terminal management is
exclusively the selected frozen exit profile, so every post-entry terminal row
has a priced gross and after-cost net R result.

The exact cost model and quote-eligibility policy are material, content-addressed
objects rather than hash labels. Scenarios bind their hashes. Gross R is
reported separately from after-cost net R. After a staged T1 observation, the
ledger carries realized R and the remaining fraction, so later exits are
weighted rather than treating the original position as fully open.
The treatment also freezes the exact `option_selection_policy`: Schwab provider,
long-to-CALL/short-to-PUT mapping, DTE/delta/open-interest/spread filters, and
fallback behavior. The v1 live exporter therefore measures shadow operational
behavior on Schwab market data; it does not claim Public execution parity.

## Install

From the Bhiksha checkout, using the shared kernel contract worktree first:

```bash
PYTHONPATH=/Users/suman/code/worktrees/bhiksha-market-context/src:/Users/suman/code/worktrees/kernel-market-context/src \
  /Users/suman/code/bhiksha/.venv/bin/python -m bhiksha.chart_scenarios install \
  --input /path/to/validated_chart_scenario_shadow_plan.json
```

Validation runs before the destination is replaced. Installation writes a
temporary file in the destination directory, fsyncs it, and uses atomic replace;
the authenticated v2 receipt is replaced atomically afterward. Its
`receipt_hash` is the canonical hash of every field except itself. A failed validation or write
produces a failed receipt and leaves the prior plan artifact untouched. Missing
or unknown identity/schema/trigger/policy/source/hash data, a hash mismatch,
an incompatible exit profile, a future observation, or a non-shadow packet
fails closed.

## One fixture/read-only cycle

The fixture format is an object with a validated `plan`, a `bars` array of
completed OHLC bars, and a `quotes` array of persisted option snapshots. The
first quote is the synthetic-entry mark; later quotes are the same quote path
used for primary and counterfactual exit observations. Each compatible profile
is evaluated with its own frozen registry policy, and the exact policy identity
is retained in observation state.

```bash
PYTHONPATH=/Users/suman/code/worktrees/bhiksha-market-context/src:/Users/suman/code/worktrees/kernel-market-context/src \
  /Users/suman/code/bhiksha/.venv/bin/python -m bhiksha.chart_scenarios observe-one \
  --fixture /path/to/chart_scenario_fixture.json \
  --db-path artifacts/chart_scenarios/shadow_events.sqlite3 \
  --observation-slot 1
```

The cycle is restart-safe. Repository state is bound to the installed plan hash
and the complete exit-policy registry hash, so a restart cannot silently change
the counterfactual policies. The event identity includes
`(campaign_id, run_id, arm_id, scenario_id, trigger_version)` and the event
role/run-owned observation-slot ID. Replaying an observation does not duplicate
a trigger, synthetic entry, or terminal event; a terminal scenario cannot be
re-armed.
The slot ID is derived from the installed run-manifest hash and a monotonic
ordinal. A candidate cannot advance to the next slot until every installed arm
has bound the current slot. Both arms must present the same evaluated time and
canonical market-facts hash; caller labels are ignored. Once every expected arm
agrees, SQLite persists a content-addressed paired-fact proof for downstream
evaluation. A divergent timestamp, bar, or quote snapshot fails closed instead
of manufacturing paired evidence.

For an operational cycle, the runtime's read-only market-data/option-selection
adapter exports one candidate-keyed JSON snapshot. Bars come from the canonical
completed-bar seam and quotes are the canonical selector's already-chosen
snapshot rows; the chart-scenario lane neither owns a client credential nor
calls an order manager. One command fans those immutable facts across every
installed scenario, pairs shared candidates by the run-owned slot, and writes a
zero-effect receipt:

```bash
PYTHONPATH=/Users/suman/code/worktrees/bhiksha-market-context/src:/Users/suman/code/worktrees/kernel-market-context/src \
  /Users/suman/code/bhiksha/.venv/bin/python -m bhiksha.chart_scenarios observe-cycle \
  --plan artifacts/chart_scenarios/active_shadow_plan.json \
  --cycle-input /path/to/read-only-cycle-snapshot.json \
  --db-path artifacts/chart_scenarios/shadow_events.sqlite3 \
  --receipt artifacts/chart_scenarios/cycle-receipt.json
```

The cycle snapshot is bound to the installed plan, registered run, frozen
treatment, one positive slot ordinal, one evaluated time, and exactly one fact
record per installed candidate. Per-candidate diagnostics retain provider gaps;
no eligible contract or quote produces `quotes=[]`, allowing both arms to bind
the same facts and emit canonical quote-unavailable evidence. Its content hash is verified before any event
write. Live snapshot capture and scheduling stay in the supervisor-owned
read-only adapter; this command has no broker-submit capability.

Operational storage is run-scoped at
`artifacts/chart_scenarios/runs/<campaign_id>/<run_id>/`. Each daily run owns a
separate SQLite event chain and cycle-receipt directory, so a later run never
inherits an earlier run's predecessor hash. Already-terminal candidates are
represented in later receipts by authenticated terminal state/event carryforward
proofs rather than an invented current-slot market proof.

## Scheduled lifecycle

The experiment is one governed campaign with a new immutable run each target
session. Daily Chartographer/TradeLab inputs are executions of that experiment;
they are not automatically new experiment versions. A treatment change—such as
a changed chart reader, ranker prompt/model, frozen exit profile, cost model, or
narrative influence—requires a new campaign/version rather than silently
changing a daily run.

Bhiksha owns one opt-in launchd job, `com.bhiksha.chart-scenario-shadow`. It is
disabled unless the installer is run with
`BHIKSHA_INSTALL_CHART_SCENARIO_SHADOW_ENABLED=true`. The job uses a non-overlap
lock and a content-addressed campaign-static configuration at
`artifacts/chart_scenarios/campaign-config.json`. No daily human edit is part of
the operating contract. The same file freezes `starts_on`,
`checkpoint_after_sessions=5`, `max_sessions=10`, and `ends_on`. Every clock
tick passes the XNYS campaign-window preflight before Birdclaw, Cartographer,
or TradeLab can run. Non-session, pre-campaign, and post-campaign ticks produce
deterministic broker-inert skip receipts; daily inputs remain runs of the same
campaign rather than new experiments.

The static configuration is accepted only when it binds three hash-valid,
cross-consistent TradeLab campaign artifacts: `campaign.json`,
`campaign-protocol.json`, and `campaign-freeze-receipt.json`. Their manifest,
protocol, freeze, treatment, universe, and session-calendar hashes must agree.
Bhiksha independently regenerates the pinned XNYS session dates and rejects a
protocol whose inclusive window, 5-session checkpoint, 10-session maximum, or
authorized dates differ. This preflight happens before any daily producer is
invoked.

Generate that file and the four exact runtime records with the checked-in builder;
do not hand-author hashes or copy a fixture config:

```bash
python -m bhiksha.tools.chart_scenario_campaign_config \
  --experiment-root /absolute/artifacts/chart_scenarios/tradelab \
  --campaign-id "$CAMPAIGN_ID" \
  --birdclaw-checkout /absolute/birdclaw --birdclaw-db /absolute/birdclaw.sqlite \
  --birdclaw-node /absolute/node \
  --cartographer-checkout /absolute/market-cartographer \
  --cartographer-python /absolute/market-cartographer/.venv/bin/python \
  --tradelab-checkout /absolute/tradelab \
  --tradelab-python /absolute/tradelab/.venv/bin/python \
  --agent-broker-checkout /absolute/agent-broker \
  --agent-broker /absolute/agent-broker/.venv/bin/agent-broker \
  --kernel-src /absolute/mala-bhiksha-kernel/src \
  --cartographer-provider mala --cartographer-data-root /absolute/mala/data \
  --symbols "SPY,QQQ,..." \
  --runtime-dir /absolute/artifacts/chart_scenarios/runtime \
  --output /absolute/artifacts/chart_scenarios/campaign-config.json
```

Every checkout must be clean. Python roles require an isolated checkout-local venv;
the command records the interpreter, installed environment, import tree, dependency
identity, and argv before validating the complete config against TradeLab's freeze.

The chart plist never carries `BHIKSHA_ENV_FILE` and the chart process never
loads the production dotenv. Installation copies only explicit configuration
paths, the Google credential-file path, and the read-only Schwab token-file path
into the plist. The shell runner rebuilds the first Python environment with
`env -i`; Public credentials, Schwab app credentials, active-plan controls, and
all other live/order settings are absent before `launchd_job` or the coordinator
starts. Each later subprocess applies a narrower role allowlist.

Installation also freezes the kernel as an authenticated runtime record under
the chart launchd artifact directory. The record binds a clean exact Git commit,
the real non-symlink `src` path, the complete source-tree digest, and the exact
`mala_bhiksha_kernel` import origin/digest. The shell verifies that record before
starting the chart Python application, and the coordinator verifies the loaded
module against it again. A dirty checkout, retargeted symlink, commit change, or
copied/modified kernel fails closed.

The configuration freezes both the isolated Birdclaw checkout and the external
canonical Birdclaw SQLite file. On oldmac the database value is
`/Users/sunny/Documents/birdclaw/birdclaw-home/birdclaw.sqlite`. Bhiksha passes
that path only through `BIRDCLAW_DB`; it is not copied into the sanitized
narrative packet or daily contract. A missing narrative source writes a hashed
`unavailable_non_blocking` sidecar and continues without `--birdclaw-export`.
Narrative remains observational and cannot influence selection.

Before the session, the coordinator invokes only the fixed broker-inert
`market_cartographer.cli market-context-export` entrypoint. Its checkout,
read-only bar provider/root, frozen symbol universe, campaign ID, TradeLab
checkout/experiment root, kernel path, spreadsheet ID, and exact Agent Broker
executable come from that static configuration. Arbitrary commands, output
paths, shell strings, Bhiksha job names, and live/order entrypoints are not
accepted. On oldmac, Agent Broker is rooted at
`/Users/sunny/code/agent-broker`.

The Cartographer receipt must be hash-valid and target the still-unopened XNYS
session for the current Chicago date. The coordinator derives the new `run_id`
from that receipt, renders only known placeholders, validates the resulting
daily contract, and atomically emits it at:

```text
artifacts/chart_scenarios/daily-contracts/YYYY-MM-DD.json
```

The filename and
`target_session_date` must match. The contract is content addressed and binds
the campaign/run IDs, campaign-configuration hash, hash-valid Cartographer v2
receipt/session window, hash-valid sanitized Birdclaw packet, TradeLab checkout,
and exact run-scoped plan/projection paths. Bhiksha constructs only three fixed
TradeLab invocations: `prepare-run`, `refresh-projection`, and `finalize-run`.
All TradeLab writes are confined to the configured experiment root under
`artifacts/chart_scenarios`; a contract cannot redirect output to
`artifacts/playbook/active_plan.json` or another live path.

Preparation is attempted at 07:45, 07:55, 08:05, and 08:15 CT. Exact success is
idempotent. After that cutoff, a missing contract cannot run Cartographer (which
would select a later session); Bhiksha instead writes one authenticated
`missed_non_comparable` receipt and fails so the launchd wrapper alerts. Later
scheduled invocations do not duplicate that alert.

The authenticated Cartographer window—not a fixed 15:05 clock—determines the
phase. This also makes an XNYS early-close day enter terminal evaluation after
the supplied `end_at`. The schedule invokes the coordinator at 07:45 CT, every
10 minutes from 08:30 through 15:00 CT, and at 15:15 CT, in addition to the
bounded preparation retries. Morning and after-close completion receipts are
immutable/idempotent; conflicting replay is rejected.

An authenticated Cartographer `status=no_plan` is a successful broker-inert
daily outcome, not a failed or new experiment. Bhiksha verifies the exact
no-plan manifest, target session, artifact inventory, hashes, and zero-effect
map before writing the daily contract. It does not run Birdclaw for that outcome.
TradeLab must then return and persist its authenticated `status=no_plan`
preparation receipt. Bhiksha stores one no-plan completion receipt and stops
before narrative/ranker work, plan installation, observation, staging, or Sheet
projection. All later ticks for that run return the same authenticated no-plan
skip.

Lifecycle receipts live under each run's `coordinator/` directory. Morning runs
the fixed TradeLab preparation, validates/installs the plan, stages the install
receipt plus empty authenticated event export, regenerates the current
projection, and then projects. Intraday takes a Schwab-only read-only market
snapshot, observes one paired slot, verifies/exports the event chain, stages
contiguously numbered cycle receipts, asks TradeLab to regenerate projection
from current evidence, and only then upserts the Sheet. After close does the
same final observation/staging, then `finalize-run` deterministically consumes
all cycle receipts to build terminal/evaluation/current projection artifacts.
TradeLab never writes the Sheet directly.

### Immutable cycle evidence v4

Each `cycle-inputs/slot-NNNN.cycle-input.json` uses
`bhiksha.chart-scenario-cycle-input.v4`. Top-level identity binds the plan, run
manifest, treatment manifest, ordinal, evaluation cutoff, candidate array, and
content hash. A hash-valid schema example is in
`docs/examples/chart_scenario_cycle_input_v4.json`. It intentionally exercises
the campaign-conformance mix (`39m` entry/validation plus `daily` invalidation)
and the frozen campaign selector bounds (absolute delta 0.20–0.40, minimum OI
100, maximum spread 0.20, strict DTE). Its plan/run identities are illustrative;
a real run must bind its own sealed identities. Every candidate has exactly
`candidate_id`, `symbol`,
`bars_by_timeframe`, `option_selection`, `quotes`, and `diagnostics`.

The v4 acquisition envelope is explicit: `cycle_started_at` precedes all
provider calls; each series carries `bar_acquired_at`; option evidence carries
`chain_acquired_at`; every quote carries its own `acquired_at`; and
`evaluated_at == sealed_at` is recorded only after all facts are acquired.
Provider timestamps may not exceed their own acquisition clock, and no
acquisition may fall outside the cycle envelope. These clocks participate in
the relevant content hashes, so a later observer cannot relabel an old fact as
newly acquired.

There is one bar series for every entry, validation, and invalidation
timeframe—no implicit substitution. `39m` bars are anchored to 09:30
America/New_York independently per XNYS session; `daily` bars are keyed to
market sessions. Unsupported timeframes fail preparation. Bar provenance
`bhiksha.chart-scenario-bar-provenance.v2` binds
`implementation=xnys-session-anchor-v2`, calendar, timezone, session anchor,
the pinned `exchange_calendars` XNYS version, exact interval,
`completed_through`, the exact source-minute rows, source count/hash, output
hash, and its own content hash. Validation independently reconstructs every
output. A 39-minute bucket is visible only at start plus 39 minutes; a daily
bar is visible only at the authenticated XNYS close, including early closes.

Option proof `bhiksha.chart-scenario-option-selection.v3` binds the provider,
chain acquisition time, frozen policy hash, exact selection request, full
content-addressed point-in-time contract set,
canonical and effective contract identities, selection mode, and receipt hash.
Validation reconstructs DTE from expiration/evaluation date, checks normalized
unique contract identities, and independently reruns
`SingleLegOptionSelector`. `canonical_selector` is required before entry.
After entry, `persisted_contract` must exactly match the contract frozen in both
durable arm states; a later chain result cannot rewrite it.

Each selected quote embeds and hashes the exact sanitized provider response.
The provider/OCC identity is joined to the selected chain contract on normalized
underlying, call/put, expiration, and decimal strike. Delta and open interest
remain quote-time facts and may legitimately differ from the earlier chain.
Credential, account, and order-shaped keys are forbidden from raw evidence.

The quote tape is chronological and unique, capped by both `evaluated_at` and
the authenticated observation-window end. A transient missing, stale, or wide
exit quote records `quote_unavailable` and leaves the position open. At window
expiry the frozen operations-failure policy terminates it; a transient quote
never becomes a runtime-error exit. Every normalized quote is sealed by its
`snapshot_hash`, which is recomputed before observation. Snapshot IDs are
deduplicated across slots, while reuse of an ID with different facts is a hard
idempotency conflict.

Cycle receipt v4 binds the immutable input artifact path and hash, paired-fact
proofs, all durable events for each exact run/candidate/slot, diagnostics,
scenario results, explicit read-only authentication facts, and an all-false
effects map. Its
`created_at` is the sealed input's `evaluated_at`, making clean replay
byte-stable. A lost acknowledgement returns the exact existing receipt; the
same ordinal with any different input hash is rejected. Failed attempts use
separate content-addressed `*.failed.json` artifacts and are never canonical
completion receipts or eligible for TradeLab staging.

Bhiksha can self-check evidence in a fresh namespace:

```bash
python -m bhiksha.chart_scenarios replay-cycles \
  --plan artifacts/chart_scenarios/runs/CAMPAIGN/RUN/active_shadow_plan.json \
  --cycle-input-dir artifacts/chart_scenarios/runs/CAMPAIGN/RUN/cycle-inputs \
  --output artifacts/chart_scenarios/replays/RUN
```

This producer self-check is not scientific acceptance. TradeLab must parse the
immutable inputs, rerun selector/triggers/exits independently, verify receipt
and event hashes, and reject any mismatch without trusting Bhiksha's replay
conclusion.

Before any external tool invocation, the coordinator verifies a frozen
runtime-record file for Birdclaw, Market Cartographer, TradeLab, and Agent
Broker. Every record is content addressed and binds its stable path, resolved
checkout and clean Git commit, actual argv prefix, launcher and interpreter
path/symlink/realpath/digest/version, entrypoint and import-tree/map digests,
and dependency-lock identity. The coordinator revalidates the applicable
record immediately before each invocation and constructs argv only from its
frozen prefix. TradeLab independently rereads and revalidates the Agent Broker
record immediately before both ranker and narrative calls. Symlink escape,
ignored-venv substitution, editable-import drift, dirty checkout, commit drift,
or executable drift fails closed. Subprocess environments are role-scoped. Broker-inert tools
receive no Public or Schwab credentials; the live exporter receives only the
existing Schwab token-file/data settings and uses a GET-only client that cannot
refresh or persist OAuth state; the Sheet process receives only the Google
credential path.

TradeLab staging validates the installed plan, install receipt, every exact
`slot-NNNN.cycle-input.json`/v4 receipt pair, and the hash-linked event export
before publishing one immutable generation through an atomic `bhiksha`
symlink. Every non-install event must be covered exactly once by slot evidence.
The remaining global partition must be exactly one canonical `installed` event
per plan scenario, first for that scenario and authenticated to the installed
plan; omissions, extras, duplicates, or wrong event types are rejected.

The isolated launchd plist pins `BHIKSHA_KERNEL_SRC`, the absolute executable
`BHIKSHA_PYTHON`, and `BHIKSHA_CHART_SCENARIO_ARTIFACT_ROOT`; coordinator startup
verifies `mala_bhiksha_kernel.__file__` resolves beneath that reviewed source
tree. The installer also captures the Bhiksha commit, runner digest, and Python
realpath/digest/version. Before reading the chart marker, the runner revalidates
those identities with absolute system tools, requires a clean checkout, and
uses the pinned Python for both path validation and the coordinator process;
`PATH` cannot substitute a different interpreter. `BHIKSHA_ENV_FILE` may point
at the existing production `.env` so the
isolated checkout can reuse credentials without copying secrets. The Sheet
projector prefers its lane-specific credential override and falls back to
Bhiksha's existing `GOOGLE_API_CREDENTIALS_PATH`.

The scoped installer rejects symlinks in the launchd, chart-log, chart-marker,
and chart-artifact paths. It writes the plist and opt-in marker atomically. The
chart logs, status, and marker live only under
`artifacts/chart_scenarios/launchd/`; a legacy marker under
`artifacts/playbook/runtime_flags/` cannot arm this job. Scoped install and
rollback never rewrite or reload the seven production Bhiksha jobs.

The Sheet writer accepts only `Chart_Scenarios_v1` with the exact v1 header
contract and key `(campaign_id, run_id, arm, scenario_id)`. It identifies blank
preformatted rows from those key columns only, preserves validations, refuses
to append beyond available rows, and hashes the exact reread values. No control
tab or trading authorization field is writable through this lane.

## Status and readback

```bash
PYTHONPATH=/Users/suman/code/worktrees/bhiksha-market-context/src:/Users/suman/code/worktrees/kernel-market-context/src \
  /Users/suman/code/bhiksha/.venv/bin/python -m bhiksha.chart_scenarios status \
  --plan artifacts/chart_scenarios/active_shadow_plan.json \
  --db-path artifacts/chart_scenarios/shadow_events.sqlite3
```

The status command revalidates the experiment plan, reports durable event and
terminal counts, checks the hash-linked append-only event chain, and reports
zero broker effects. SQLite writes have a short bounded lock budget; status
readback has its own bounded read budget so a brief schema/writer lock does not
silently widen observation writes.

## Rollback

Run `install_bhiksha_launchd.sh uninstall-chart-scenario-shadow`. The scoped
rollback unloads/removes only the chart plist and clears
`chart_scenario_shadow.enabled`, so manual runner invocation is also disarmed;
live trading jobs and plists are untouched. Then archive or remove the generated
files under `artifacts/chart_scenarios/` according to the repository retention
policy. Historical receipts remain evidence. Rollback does not alter the live
active plan or existing strategy tables. A new behavior or policy must be
issued as a new treatment/component version and must pass bundle validation
before observation resumes.
