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

Runtime loading also suppresses enabled Mala-origin legacy deployments with
`legacy_wire_retired`, so stale ignored generated files cannot silently become
runtime deployments. Strategy-catalog entries remain readable as historical
fixtures, but they are disabled and `approval_status: retired` until they
re-earn promotion.

Packet compilation can also consume the report:

```bash
./.venv/bin/python -m bhiksha.tools.compile_packet \
  --packet ../mala_v2/packets/execution/execution.mean_reversion_at_extremes.iwm_qqq/v1.json \
  --capability-manifest artifacts/capabilities/bhiksha_packet_capabilities_v1.json \
  --legacy-retirement-report artifacts/legacy_retirement/current.json
```

If `active_legacy_wire_count` is greater than zero, execution packets are
blocked with `legacy_retirement_blocked:<count>` even when feature capability
exists.
