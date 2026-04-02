# Bhiksha

Bhiksha is the live execution runtime for session payloads compiled from `mala`.

Current live scope:

- live sessions loaded from one authoritative `active_session.json`
- `market_impulse`, `jerk_pivot_momentum`, `elastic_band_reversion`, `opening_drive_classifier`, and `manual_trigger`
- single-leg options execution through Public
- live underlying bars from Schwab
- underlying-anchored thesis exits plus option catastrophe protection
- intraday emergency `halt_and_flatten` operator control

## Start Here

- Architecture: `/Users/suman/kg_env/projects/bhiksha/docs/architecture.md`
- Session log: `/Users/suman/kg_env/projects/bhiksha/docs/agent.md`
- Deploy runbook: `/Users/suman/kg_env/projects/bhiksha/docs/deploy_runbook.md`

## Repo Layout

- `config/`: app config, provider config, deployment manifests, broker token files
- `src/bhiksha/app/`: runtime bootstrap and startup health wiring
- `src/bhiksha/strategy/`: strategy plugins
- `src/bhiksha/market_data/`: bar adapters, rolling store, Newton feature pipeline
- `src/bhiksha/execution/`: planning, broker orders, supervision, protective stops
- `src/bhiksha/state/`: live position tracking and broker reconciliation
- `src/bhiksha/tools/`: operator commands
- `tests/`: unit and smoke-level regression tests

## Most Important Files

- Canonical operator entrypoint: `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/tools/bionic_session.py`
- Runtime entrypoint: `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/tools/trade_session.py`
- Continuous loop: `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/tools/dry_run_live_loop.py`
- Market Impulse strategy: `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/strategy/market_impulse.py`
- Jerk Pivot strategy: `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/strategy/jerk_pivot_momentum.py`
- Execution planner: `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/execution/planner.py`
- Order manager: `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/execution/order_manager.py`
- Execution supervisor: `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/execution/supervisor.py`
- Public broker adapter: `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/execution/brokers/public/adapter.py`
- Schwab bar adapter: `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/market_data/adapters/schwab.py`
- Session payload artifact: `/Users/suman/kg_env/projects/bhiksha/artifacts/playbook/active_session.json`

## Commands

Pre-open prepare:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.bionic_session prepare
```

Health only:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.healthcheck
```

Warm-start smoke test:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.bionic_session run --max-bars 0
```

Dry-run live loop:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.bionic_session run
```

Live loop:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.bionic_session run --live
```

Note:

- deployments with `execution.shadow_only: true` still evaluate live bars and emit simulated plans during `--live`, but they do not send orders or create tracked positions.

Session summary:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.session_summary
```

Legacy importer:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.import_playbook ...
```

This remains for older generated-manifest workflows only. In the Bionic loop,
the canonical authority is `active_session.json`, not generated deployment YAMLs.

Observation report:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.observation_report --skip-replay
```

Canonical post-close review:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.bionic_session review
```

Signal inspector:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.signal_inspector --trading-days 3
```

Signal inspector with CSV export:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.signal_inspector \
  --deployment-id jerk_pivot_momentum_tsla_short_v1 \
  --trading-days 3 \
  --csv artifacts/signal_inspector/tsla_last_3_trading_days.csv
```
For all deployements:
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.signal_inspector \
  --trading-days 3 \
  --csv artifacts/signal_inspector/all_enabled_last_3_trading_days.csv


This now reports:

- total event counts
- per-deployment event counts
- latest lifecycle state per deployment
- counts of `signal=True` and `exit=True` decisions
- recent event details for lifecycle, signal, and exit decisions

Tests:

```bash
PYTHONPATH=src .venv/bin/pytest -q
```

## Operating Notes

- In Bionic mode, Bhiksha executes only the deployments present in `active_session.json`.
- `config/bias_inputs.yaml` still supports:
  - `emergency.halt_and_flatten: true` to stop new entries and flatten Bhiksha-managed positions intraday
- Quotes and preflight are already validated against Public.
- Restart safety is broker-sync based: existing Public option positions are re-imported on startup and each completed bar.
- Compact research-style execution inputs such as `dte: "7-21"`, `delta_target: "0.35-0.55"`, and `entry_window_et: "09:45-14:30"` now normalize directly into the Bhiksha manifest model.
- Historical signal inspection now uses NYSE trading-day lookbacks rather than naive calendar-day windows.
- Optional signal-inspector CSV exports are intended to live under `artifacts/signal_inspector/`, which is gitignored.

Verification:

- `105` tests passing.
- `python3 -m compileall src tests` passes cleanly.
