# Bhiksha

Bhiksha is a live execution runtime for strategy deployments proposed by `mala`.

Day 1 scope:

- `QQQ` Market Impulse short deployment
- `SPY` Market Impulse short deployment
- single-leg long puts
- live underlying bars from Schwab
- execution through Public
- config-driven deployments and conservative risk defaults

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

- Runtime entrypoint: `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/tools/trade_session.py`
- Continuous loop: `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/tools/dry_run_live_loop.py`
- Market Impulse strategy: `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/strategy/market_impulse.py`
- Execution planner: `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/execution/planner.py`
- Order manager: `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/execution/order_manager.py`
- Execution supervisor: `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/execution/supervisor.py`
- Public broker adapter: `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/execution/brokers/public/adapter.py`
- Schwab bar adapter: `/Users/suman/kg_env/projects/bhiksha/src/bhiksha/market_data/adapters/schwab.py`
- Deployment manifests:
  - `/Users/suman/kg_env/projects/bhiksha/config/deployments/market_impulse_qqq_short_v1.yaml`
  - `/Users/suman/kg_env/projects/bhiksha/config/deployments/market_impulse_spy_short_v1.yaml`

## Commands

Health:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.healthcheck
```

Warm-start smoke test:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.trade_session --max-bars 0
```

Dry-run live loop:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.trade_session
```

Live loop:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.trade_session --live
```

Session summary:

```bash
PYTHONPATH=src .venv/bin/python -m bhiksha.tools.session_summary
```

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

## Day-1 Notes

- Live orders are still unproven in production because we have intentionally not sent one yet.
- Quotes and preflight are already validated against Public.
- Restart safety is broker-sync based: existing Public option positions are re-imported on startup and each completed bar.
