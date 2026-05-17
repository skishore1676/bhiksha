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
- UI v0 cannot submit broker orders. It can create an approved live ticket, but
  broker submission remains outside the desk.

## Start

```bash
PYTHONPATH=/Users/suman/code/mala-bhiksha-kernel/src:src ./.venv/bin/python \
  -m bhiksha.tools.trader_desk \
  --port 8766
```

Open:

```text
http://127.0.0.1:8766
```

## Operator Flow

1. Check the system rail: packet, runtime mode, live boundary, and health.
2. Write the unbiased chart read.
3. Consult Mala from the desk.
4. Choose take, watch, or pass.
5. If taking, select the management policy and build option preview.
6. Approve or reject the live ticket.
7. Use the live management/lifecycle lane outside UI v0 for broker submission
   and monitoring.

## Future Playbook Expansion

The service exposes playbooks as cards. Future versions should populate those
cards from a packet registry rather than hard-coding the IWM/QQQ reversion
packet.
