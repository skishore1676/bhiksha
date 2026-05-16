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
  -> Bhiksha records a shadow execution intent
  -> Bhiksha builds an option preview ticket
  -> shadow lane records simulated option PnL
  -> live lane can create an approval-gated ticket
```

The bridge now has parallel shadow and live lanes. The shadow lane exists to
learn whether the playbook would have made money using the option vehicle and
management choice. The live lane exists to make a real order possible only
after an explicit approval ticket. The submitter/reconciliation layer is still
separate.

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

## Operator Decision

After reading the consultation, record the red/green decision from Bhiksha:

```bash
PYTHONPATH=/Users/suman/code/mala-bhiksha-kernel/src:src ./.venv/bin/python \
  -m bhiksha.tools.decide_playbook_trade \
  --consultation-artifact artifacts/playbook/consultations/<consultation_id>/consultation_bridge.json \
  --decision take \
  --selected-management-policy reversal_extreme__fixed_1r \
  --operator-note "Taking; clean rejection and fast fixed-risk management only."
```

For a pass:

```bash
PYTHONPATH=/Users/suman/code/mala-bhiksha-kernel/src:src ./.venv/bin/python \
  -m bhiksha.tools.decide_playbook_trade \
  --consultation-artifact artifacts/playbook/consultations/<consultation_id>/consultation_bridge.json \
  --decision pass \
  --operator-note "Passing; setup is not clean enough for options risk."
```

The take path requires a management policy id from the execution packet's
allowed list. It writes:

```text
artifacts/playbook/intents/<intent_id>/playbook_operator_decision.json
artifacts/playbook/intents/<intent_id>/PLAYBOOK_OPERATOR_DECISION.md
```

The intent can be `shadow_intent_ready`, `operator_pass`, or `blocked`.
`shadow_intent_ready` still has `order_submission_allowed=false`; it is the
machine-readable handoff for the next option-preview/live-approval layer, not
an order ticket.

## Option Preview

For a `shadow_intent_ready` artifact, Bhiksha can resolve the option candidate
and run the same chain/quote/risk checks used by the execution planner:

```bash
PYTHONPATH=/Users/suman/code/mala-bhiksha-kernel/src:src ./.venv/bin/python \
  -m bhiksha.tools.preview_playbook_option \
  --intent-artifact artifacts/playbook/intents/<intent_id>/playbook_operator_decision.json \
  --packet /Users/suman/code/mala_v2/packets/execution/execution.mean_reversion_at_extremes.iwm_qqq/v1.json \
  --underlying-price 210.25
```

It writes:

```text
artifacts/playbook/option_previews/<preview_id>/playbook_option_preview.json
artifacts/playbook/option_previews/<preview_id>/PLAYBOOK_OPTION_PREVIEW.md
```

The preview can be `option_preview_ready` or `blocked`. A ready preview includes
the option symbol, quantity, estimated entry price, and risk reasons. It still
has `order_submission_allowed=false` and `live_approval_required=true`.

## Shadow Outcome

At management exit or end of day, record the simulated option result:

```bash
PYTHONPATH=/Users/suman/code/mala-bhiksha-kernel/src:src ./.venv/bin/python \
  -m bhiksha.tools.record_playbook_shadow_outcome \
  --option-preview-artifact artifacts/playbook/option_previews/<preview_id>/playbook_option_preview.json \
  --exit-timestamp "2026-05-11 15:55 America/New_York" \
  --exit-reason end_of_day_mark \
  --exit-price 3.40
```

For live-market shadowing, `--quote-current` can use the current option quote
as the exit mark. The output writes:

```text
artifacts/playbook/shadow_outcomes/<outcome_id>/playbook_shadow_outcome.json
artifacts/playbook/shadow_outcomes/<outcome_id>/PLAYBOOK_SHADOW_OUTCOME.md
```

This is the artifact that tells us whether the playbook and management choice
actually made or lost money in the shadow lane.

## Live Ticket

The live lane starts from the same option preview, but it requires an explicit
operator approval phrase:

```bash
PYTHONPATH=/Users/suman/code/mala-bhiksha-kernel/src:src ./.venv/bin/python \
  -m bhiksha.tools.create_playbook_live_ticket \
  --option-preview-artifact artifacts/playbook/option_previews/<preview_id>/playbook_option_preview.json \
  --decision approve \
  --operator Suman \
  --operator-note "Approve one contract only; manage with fixed 1R policy." \
  --approval-phrase APPROVE_LIVE_PLAYBOOK_TICKET
```

It writes:

```text
artifacts/playbook/live_tickets/<ticket_id>/playbook_live_ticket.json
artifacts/playbook/live_tickets/<ticket_id>/PLAYBOOK_LIVE_TICKET.md
```

An approved live ticket has `order_submission_allowed=true` but
`submitter_status=not_submitted`. It is permission for the later submitter
layer; it is not itself a broker order.

## Execution Boundary

The current bridge is a consultation backend, not live trading automation.

For live use, the next layer must add:

1. order submission from an approved live ticket
2. position manager and broker reconciliation
3. fill/fire/outcome feedback artifact back to Mala

Until those exist, a live approval ticket means "the submitter may act after
its own final checks," not "an order has already been placed."
