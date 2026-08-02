# Bhiksha Trader Desk Lite

The Trader Desk is a Bhiksha-native sidecar for playbook operations. It is not a
separate trading engine and does not borrow execution logic from older UI
projects.

## Boundary

- Bhiksha runtime stays responsible for live metrics, packet compile, risk,
  option preview, lifecycle, and feedback artifacts.
- Mala is queried only through the playbook consultation bridge.
- The operator owns chart context, take/watch/pass, policy selection, live
  ticket approval, emergency intervention, and subjective feedback.
- UI v1 can submit a broker order only through the approval-gated
  Approve+Submit flow, then Bhiksha immediately starts lifecycle management.

## Start

```bash
MALA_REPO_ROOT="${MALA_REPO_ROOT:-../mala_v2}" \
PYTHONPATH="../mala-bhiksha-kernel/src:src" \
  ./.venv/bin/python -m bhiksha.tools.trader_desk --port 8766
```

From this Mac, when Bhiksha runs on oldmac, use:

```bash
scripts/open_oldmac_trader_desk.sh
```

Open:

```text
http://127.0.0.1:8766
```

## Broker-inert consultation service

PAT or another localhost client can use a smaller HTTP surface that cannot
preview, approve, submit, or manage an order. It defaults to the Mala v1
shadow-only packet and refuses a non-loopback bind:

```bash
bash scripts/start_trader_desk_consult_only.sh
```

The launcher is a dedicated process with no imports from Bhiksha bootstrap,
brokers, order managers, lifecycle stores, or submission code. It binds
`127.0.0.1:8767` by default. Install it on oldmac with
`scripts/install_trader_desk_consult_only_oldmac.sh install`. The only routes are:

```text
GET  /api/status
GET  /api/preflight
GET  /api/latest
POST /api/consult
```

All full-desk health, live-context, decision, option-preview, ticket,
approval-submit, and live-management routes return `404` in this mode.

The localhost PAT adapter should call `POST http://127.0.0.1:8767/api/consult`
with JSON shaped as follows (`timestamp` is optional and defaults to market now):

```json
{
  "symbol": "IWM",
  "direction": "short",
  "chart_read": "Stretched above VWAP and starting to reject.",
  "timestamp": "2026-08-03 09:40 America/Chicago"
}
```

`symbol` must be `IWM` or `QQQ`, `direction` must be `long` or `short`, and
`chart_read` is required. A successful response has `status=consulted` plus the
packet/version, normalized market context, compile result, verdict, policy,
selected exit, allowed management policy ids, and consultation artifact paths.
The adapter must treat any non-200 response or any other `status` as a failed
consultation and must not fall through to a trading endpoint.

## Operator Flow

1. Check readiness: packet, runtime mode, market state, provider health, and quote.
2. Write the unbiased chart read.
3. Consult Mala using the default market-now timestamp.
4. Select the management policy and preview the option.
5. Hold the Approve+Submit button to create the live ticket, submit the order,
   and start lifecycle management.
6. Watch lifecycle state and intervene if Bhiksha reports a critical protection
   failure.

Use `Rehearsal` preview mode to exercise the full desk flow without live
option-chain or quote calls. Use `Live Provider` mode for real readiness; it
will block when broker auth or market-data access is not healthy.

## Future Playbook Expansion

The service exposes playbooks as cards. Future versions should populate those
cards from a packet registry rather than hard-coding the IWM/QQQ reversion
packet.
