# Playbook Consultation Bridge

This is the Bhiksha-native backend for consulting a Mala playbook before any
order is placed.

It is the current implementation layer behind the future Trader Desk button:

```text
operator chart read
  -> Bhiksha compiles the approved shadow execution packet
  -> Bhiksha calls Mala's playbook query and policy card
  -> Bhiksha records the consultation artifact
  -> operator decides take/pass and management policy
```

The bridge does not submit orders. It is intentionally shadow-only until the
option selector, live position manager, and feedback writer are promoted under
the same packet id.

## Command

```bash
PYTHONPATH=/Users/suman/code/mala-bhiksha-kernel/src:src ./.venv/bin/python \
  -m bhiksha.tools.consult_playbook \
  --packet /Users/suman/code/mala_v2/packets/execution/execution.mean_reversion_at_extremes.iwm_qqq/v1.json \
  --capability-manifest artifacts/capabilities/bhiksha_packet_capabilities_v1.json \
  --legacy-retirement-report artifacts/legacy_retirement/current.json \
  --symbol IWM \
  --direction short \
  --timestamp "2026-05-11 09:40 America/Chicago" \
  --chart-read "Stretched above VWAP and starting to reject; would consider fast fade only."
```

The chart read is required. Bhiksha must not query Mala first and then let the
operator backfill a story.

## Outputs

Each consultation writes:

```text
artifacts/playbook/consultations/<consultation_id>/consultation_bridge.json
artifacts/playbook/consultations/<consultation_id>/CONSULTATION_BRIDGE.md
```

The artifact records:

- packet id and version
- compile decision and block reasons
- operator chart read
- Mala query and policy-card artifact paths
- verdict, selected policy, and selected exit
- allowed management policy ids from the execution packet

## Execution Boundary

The current bridge is a consultation backend, not live trading automation.

For live use, the next layer must add:

1. option selection preview with liquidity and risk checks
2. operator-selected management policy capture
3. explicit live approval gate
4. order submission and position manager
5. fill/fire/outcome feedback artifact back to Mala

Until those exist, a green operator decision means "record the consultation and
prepare the shadow decision," not "place a live order."
