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
