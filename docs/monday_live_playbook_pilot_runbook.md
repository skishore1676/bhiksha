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
- if the entry fills but the protective stop cannot be armed, Bhiksha records a
  critical stop-arm failure, attempts an emergency close, and the submit command
  exits non-zero

## Preflight

The easiest Monday entrypoint is the guided pilot desk:

```bash
PYTHONPATH=/Users/suman/code/mala-bhiksha-kernel/src:src ./.venv/bin/python \
  -m bhiksha.tools.playbook_pilot_desk preflight
```

```bash
PYTHONPATH=/Users/suman/code/mala-bhiksha-kernel/src:src ./.venv/bin/python \
  -m bhiksha.tools.compile_packet \
  --packet /Users/suman/code/mala_v2/packets/execution/execution.mean_reversion_at_extremes.iwm_qqq/v2.json \
  --capability-manifest artifacts/capabilities/bhiksha_packet_capabilities_v1.json \
  --legacy-retirement-report artifacts/legacy_retirement/current.json
```

Expected: `eligibility=eligible`, `executable=true`,
`runtime_mode=live_approval_gated`, no block reasons.

Check where you are in the flow:

```bash
PYTHONPATH=/Users/suman/code/mala-bhiksha-kernel/src:src ./.venv/bin/python \
  -m bhiksha.tools.playbook_pilot_desk latest
```

## Latency Probe

Run this before market open to separate desk orchestration latency from
broker/provider readiness:

```bash
PYTHONPATH=/Users/suman/code/mala-bhiksha-kernel/src:src ./.venv/bin/python \
  -m bhiksha.tools.playbook_pilot_desk latency-probe \
  --option-preview-mode simulated
```

Run the live-provider version only as a readiness check. It does not submit
orders, but it will fail fast if broker auth or option-chain access is not
ready:

```bash
PYTHONPATH=/Users/suman/code/mala-bhiksha-kernel/src:src ./.venv/bin/python \
  -m bhiksha.tools.playbook_pilot_desk latency-probe \
  --option-preview-mode live
```

The probe writes `artifacts/playbook/latency/<timestamp>/PLAYBOOK_LATENCY_PROBE.md`.
Current measured shape: preflight and decision capture are near-instant; Mala
consultation is the dominant leg at roughly 3.5 seconds on the local cached
historical sample.

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

The guided version walks those first five steps with prompts:

```bash
PYTHONPATH=/Users/suman/code/mala-bhiksha-kernel/src:src ./.venv/bin/python \
  -m bhiksha.tools.playbook_pilot_desk guided
```

By default, `guided` stops after an approved live ticket. To let it ask about
broker submission too, add `--allow-live-submit`.

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
If lifecycle submit reports `protection_failed_exit_pending` or
`critical_unprotected`, treat it as an active risk event, not a normal
lifecycle start.
