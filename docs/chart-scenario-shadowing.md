# Chart-scenario shadowing

This lane observes validated `morning-market-scenario-selection-shadow.v1`
packets. It is an experiment receipt path, not an execution path.

## Zero-order boundary

The chart-scenario package has no import or capability path to an order manager,
broker client, order submission/cancellation, reconciliation, the live-plan
compiler, or the live execution supervisor. Its quote input is a read-only
snapshot protocol. Every receipt and every event records
`broker_effect_count=0`; a synthetic entry or exit is a mark observation and
never a fill.

Do not add this lane to `active_plan.json`, `manual_entry`, `active_strategies`,
Google Sheets, a scheduler, or a broker service. The existing live plan is
neither read nor written by this package.

## Artifacts and state

The default experiment-only paths are:

```text
artifacts/chart_scenarios/active_shadow_plan.json
artifacts/chart_scenarios/active_shadow_plan.receipt.json
artifacts/chart_scenarios/shadow_events.sqlite3
```

The input bundle must carry the exact kernel hashes for its component manifest,
chart evidence, candidate pool/candidates, arm selections, and scenarios. It
must use the v1 chart-scenario schema, `authorization_mode=shadow`,
`source_type=chart_scenario_experiment`, and
`trigger_version=market-context-trigger.v1`.

The bundle must also carry an exact `exit_policy_registry` keyed by exit
profile. It must cover the union of every scenario's compatible profiles. Each
selected scenario embeds the same canonical policy material as its selected
registry entry, and its policy ID, schema version, and hash must match. Bhiksha
does not supply target, stop, hard-flat, giveback, or profile-selection
defaults; missing, unresolved, or mismatched economics fail validation.

## Install

From the Bhiksha checkout, using the shared kernel contract worktree first:

```bash
PYTHONPATH=/Users/suman/code/worktrees/bhiksha-market-context/src:/Users/suman/code/worktrees/kernel-market-context/src \
  /Users/suman/code/bhiksha/.venv/bin/python -m bhiksha.chart_scenarios install \
  --input /path/to/validated_chart_scenario_shadow_plan.json
```

Validation runs before the destination is replaced. Installation writes a
temporary file in the destination directory, fsyncs it, and uses atomic replace;
the receipt is replaced atomically afterward. A failed validation or write
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
  --db-path artifacts/chart_scenarios/shadow_events.sqlite3
```

The cycle is restart-safe. The event identity includes
`(campaign_id, run_id, arm_id, scenario_id, trigger_version)` and the event
role/observation ID. Replaying an observation does not duplicate a trigger,
synthetic entry, or terminal event; a terminal scenario cannot be re-armed.

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

Stop invoking the experiment-only observer and archive or remove the generated
files under `artifacts/chart_scenarios/` according to the repository retention
policy. Historical receipts remain evidence. Rollback does not alter the live
active plan or existing strategy tables. A new behavior or policy must be
issued as a new treatment/component version and must pass bundle validation
before observation resumes.
