# Monday Live Playbook Pilot Runbook

Scope: IWM/QQQ mean-reversion playbook, one-contract small-account pilot,
operator approval gated. This is not autonomous trading.

## Hard Boundary

- packet: `/Users/suman/code/mala_v2/packets/execution/execution.mean_reversion_at_extremes.iwm_qqq/v2.json`
- runtime mode: `live_approval_gated`
- live automation: disabled
- max quantity: `1`
- max premium: `$300`
- live entry requires an approved live ticket
- option preview requires the underlying stop price
- live management requires a started lifecycle artifact

## Preflight

```bash
PYTHONPATH=/Users/suman/code/mala-bhiksha-kernel/src:src ./.venv/bin/python \
  -m bhiksha.tools.compile_packet \
  --packet /Users/suman/code/mala_v2/packets/execution/execution.mean_reversion_at_extremes.iwm_qqq/v2.json \
  --capability-manifest artifacts/capabilities/bhiksha_packet_capabilities_v1.json \
  --legacy-retirement-report artifacts/legacy_retirement/current.json
```

Expected: `executable=true`, `runtime_mode=live_approval_gated`, no block
reasons.

## Operator Flow

1. Consult from Bhiksha using the v2 packet and your pre-Mala chart read.
2. If taking, record a `take` decision and select one allowed management policy.
3. Preview the option and include both current underlying price and the
   underlying stop price.
4. Approve the live ticket with `APPROVE_LIVE_PLAYBOOK_TICKET`.
5. Submit the approved live ticket with the v2 packet.
6. Start the live management monitor in dry mode first.
7. After the lifecycle/trade state looks correct, restart the monitor with
   `--execute`.

## Option Preview Example

```bash
PYTHONPATH=/Users/suman/code/mala-bhiksha-kernel/src:src ./.venv/bin/python \
  -m bhiksha.tools.preview_playbook_option \
  --intent-artifact artifacts/playbook/intents/<intent_id>/playbook_operator_decision.json \
  --packet /Users/suman/code/mala_v2/packets/execution/execution.mean_reversion_at_extremes.iwm_qqq/v2.json \
  --underlying-price <live_underlying_entry_price> \
  --underlying-stop-price <playbook_invalidation_price>
```

## Live Management Dry Run

```bash
PYTHONPATH=/Users/suman/code/mala-bhiksha-kernel/src:src ./.venv/bin/python \
  -m bhiksha.tools.manage_playbook_live_trade \
  --lifecycle-artifact artifacts/playbook/lifecycle/<lifecycle_id>/playbook_lifecycle_submission.json \
  --packet /Users/suman/code/mala_v2/packets/execution/execution.mean_reversion_at_extremes.iwm_qqq/v2.json \
  --db-path bhiksha.db \
  --quote-provider schwab \
  --loop \
  --json
```

## Live Management Execute

```bash
PYTHONPATH=/Users/suman/code/mala-bhiksha-kernel/src:src ./.venv/bin/python \
  -m bhiksha.tools.manage_playbook_live_trade \
  --lifecycle-artifact artifacts/playbook/lifecycle/<lifecycle_id>/playbook_lifecycle_submission.json \
  --packet /Users/suman/code/mala_v2/packets/execution/execution.mean_reversion_at_extremes.iwm_qqq/v2.json \
  --db-path bhiksha.db \
  --quote-provider schwab \
  --loop \
  --execute \
  --json
```

## Abort Rule

If compile, option preview, live ticket, lifecycle submit, or monitor reports a
block reason, stop the pilot and fix the artifact/code path before trading.
