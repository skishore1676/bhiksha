# Bhiksha Deploy Runbook

This runbook covers the current operator loop for Bhiksha with Mala playbook
imports and the intraday emergency control.

## Pre-Market Checklist

1. Confirm `.env` is present and current.
2. Confirm Schwab OAuth tokens exist in `/Users/suman/kg_env/projects/bhiksha/config/schwab_tokens.json`.
3. Confirm `config/bias_inputs.yaml` reflects the day's emergency controls and any importer-era fallbacks.
   In the Bionic loop, the daily symbol/thesis intent now comes from Mala's compiled `active_session.json`.
4. If you need an emergency fail-safe armed but inactive, confirm:

```yaml
emergency:
  halt_and_flatten: false
```

5. Run provider health:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.healthcheck
```

Expected:

- `PUBLIC=True` with `options_level=LEVEL_2`
- `POLYGON=True`
- `SCHWAB=True`

6. Compile and publish the latest Mala active session before open.

Preferred path from the Mala repo:

```bash
./.venv/bin/python scripts/compile_active_session.py \
  --playbook-catalog <path>/playbook_catalog.json \
  --out-dir data/results/active_session/<YYYY-MM-DD> \
  --manual-google-sheet-id <entry_v1_sheet_id> \
  --publish-bhiksha
```

Fallback importer path if you are using the older deployment-candidates export:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.import_playbook \
  --deployment-candidates <path>/deployment_candidates.json \
  --playbook-catalog <path>/playbook_catalog.json
```

Expected:

- one `active_session.json` is published into `artifacts/playbook/active_session.json`
- each deployment in the file is tagged as either `mala_playbook` or `operator_manual`
- manual entries suppress playbook entries on the same symbol
- optimized Bionic playbooks still carry:
  - `exit.thesis_exit_anchor: underlying`
  - `exit.thesis_exit_policy: ...`
  - `exit.catastrophe_exit_anchor: option_premium`

7. Run a warm-start dry run:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.trade_session \
  --session-payload artifacts/playbook/active_session.json \
  --max-bars 0
```

Expected:

- `SYNC positions=...`
- `WARMED QQQ bars=...`
- `WARMED SPY bars=...`

## Dry-Run Session

Use this first if you want to watch signals without sending orders:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.trade_session \
  --session-payload artifacts/playbook/active_session.json
```

What it does:

- warms 1-minute Schwab bars
- syncs open Public positions on startup and every new bar
- evaluates the active deployment set on completed bars only
- logs plans into SQLite without placing orders
- hard-flats tracked positions in dry-run mode after the configured ET cutoff

## Live Session

Use only after the dry-run session looks healthy:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.trade_session \
  --session-payload artifacts/playbook/active_session.json \
  --live
```

Live behavior:

- gets the selected contract from Schwab chain data
- gets the actual execution quote from Public
- runs Public preflight before entry submission
- places the entry only if quote, spread, open interest, and risk checks pass
- waits for the entry fill and then places a protective stop
- syncs broker positions on every bar so restarts do not double-enter
- submits a hard-flat close order at the configured ET cutoff

## Intraday Emergency Control

If your macro read is invalidated intraday, set:

```yaml
emergency:
  halt_and_flatten: true
```

in `config/bias_inputs.yaml`.

Current runtime behavior:

- Bhiksha reloads the emergency flag during the session
- new entries are skipped while the flag is on
- open Bhiksha-managed positions are flattened
- pending live entry orders are canceled instead of being treated as open positions

To return to normal routing on a later session, set the flag back to `false`
before startup or before the next import cycle.

## Day-1 Guardrails

- Deployments come only from `active_session.json` in session-payload mode.
- Vehicle: single-leg long puts for short signals
- DTE: `0-7`
- Budget per trade: `$300`
- Stop loss: `45%`
- Hard flat: `15:55 ET`

## Current Known Limits

- The runtime is single-process and SQLite-backed.
- The order log is durable, but advanced resume logic is still broker-sync based rather than full event replay.
- Protective stops are submitted after fill; if a stop fills externally, the next portfolio sync clears the tracked position.
- Bhiksha now supports `market_impulse`, `jerk_pivot_momentum`, `elastic_band_reversion`, `opening_drive_classifier`, and `manual_trigger`.
- In `--session-payload` mode, Bhiksha ignores `config/deployments/` entirely; the session file is the sole authority.
