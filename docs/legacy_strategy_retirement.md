# Legacy Strategy Retirement Gate

The shared-contract refactor does not grandfather older Mala-promoted strategy
rows into the new runtime path.

Current old-lane strategy/deployment YAML may remain in the repo as fixtures,
history, or post-mortem evidence, but any active legacy wire must be treated as
blocked until it re-earns promotion through:

1. fresh Mala evidence,
2. shared-kernel packet id and version,
3. Bhiksha feature/runtime capability,
4. signal parity,
5. explicit operator approval.

Run the gate:

```bash
./.venv/bin/python -m bhiksha.tools.legacy_retirement \
  --deployments-dir config/deployments \
  --strategy-catalog-dir config/strategy_catalog
```

Exit code `2` means at least one active legacy wire exists. That is expected
until the old shadow lane is wound down; it is not approval to trade it.

This gate is intentionally diagnostic for now. The next implementation step is
to make active legacy wires non-loadable after the forensic parity report is
written and the operator agrees which rows are retired versus re-hypothesized.
