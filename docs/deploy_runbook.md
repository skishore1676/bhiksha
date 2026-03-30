# Bhiksha Deploy Runbook

This runbook is for the first live session on Monday, 2026-03-30.

## Pre-Market Checklist

1. Confirm `.env` is present and current.
2. Confirm Schwab OAuth tokens exist in `/Users/suman/kg_env/projects/bhiksha/config/schwab_tokens.json`.
3. Run provider health:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.healthcheck
```

Expected:

- `PUBLIC=True` with `options_level=LEVEL_2`
- `POLYGON=True`
- `SCHWAB=True`

4. Run a warm-start dry run:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.trade_session --max-bars 0
```

Expected:

- `SYNC positions=...`
- `WARMED QQQ bars=...`
- `WARMED SPY bars=...`

## Dry-Run Session

Use this first if you want to watch signals without sending orders:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.trade_session
```

What it does:

- warms 1-minute Schwab bars
- syncs open Public positions on startup and every new bar
- evaluates QQQ/SPY deployments on completed bars only
- logs plans into SQLite without placing orders
- hard-flats tracked positions in dry-run mode after the configured ET cutoff

## Live Session

Use only after the dry-run session looks healthy:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.trade_session --live
```

Live behavior:

- gets the selected contract from Schwab chain data
- gets the actual execution quote from Public
- runs Public preflight before entry submission
- places the entry only if quote, spread, open interest, and risk checks pass
- waits for the entry fill and then places a protective stop
- syncs broker positions on every bar so restarts do not double-enter
- submits a hard-flat close order at the configured ET cutoff

## Day-1 Guardrails

- Deployments: `market_impulse_qqq_short_v1`, `market_impulse_spy_short_v1`
- Vehicle: single-leg long puts for short signals
- DTE: `0-7`
- Budget per trade: `$300`
- Stop loss: `45%`
- Hard flat: `15:55 ET`

## Current Known Limits

- The runtime is single-process and SQLite-backed.
- The order log is durable, but advanced resume logic is still broker-sync based rather than full event replay.
- Protective stops are submitted after fill; if a stop fills externally, the next portfolio sync clears the tracked position.
