"""Submit approved playbook live tickets into Bhiksha's managed lifecycle."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bhiksha.domain.models import TradeRecord
from bhiksha.execution.order_manager import OrderManager, PreflightCheck, round_price
from bhiksha.persistence.repository import EventRepository, NullEventRepository, NullTradeStateRepository, TradeStateRepository
from bhiksha.shared_kernel import ensure_kernel_on_path
from bhiksha.state.lifecycle import TradeLifecycleStore

ensure_kernel_on_path()
from mala_bhiksha_kernel import (  # noqa: E402
    ExecutionPacket,
    ManagementPolicySpec,
    PacketStatus,
    RuntimeMode,
    read_packet_file,
)


@dataclass(frozen=True, slots=True)
class ManagementLifecycleSpec:
    policy_id: str
    stop_family: str
    stop_anchor: str
    exit_family: str
    target_model: str
    stop_loss_pct: float
    option_stop_fallback_pct: float
    target_r: float
    hard_flat_time_et: str
    target_order_mode: str
    source_config_id: str | None
    source: str


@dataclass(frozen=True, slots=True)
class PlaybookLifecycleSubmitResult:
    status: str
    lifecycle_started: bool
    packet_id: str
    packet_version: int
    symbol: str
    direction: str
    option_symbol: str
    quantity: int
    limit_price: float
    entry_price: float
    underlying_entry_price: float | None
    underlying_stop_price: float | None
    trade_id: str
    entry_order_id: str | None
    stop_order_id: str | None
    stop_price: float | None
    target_order_id: str | None
    target_price: float | None
    management_policy_id: str
    management_spec: dict[str, Any]
    trade_state: str
    block_reasons: list[str]
    artifact_json: str
    artifact_md: str


async def submit_playbook_live_ticket(
    *,
    live_ticket_artifact: Path,
    packet_path: Path,
    order_manager: OrderManager,
    event_repository: EventRepository | None = None,
    trade_state_repository: TradeStateRepository | None = None,
    lifecycle_store: TradeLifecycleStore | None = None,
    out_root: Path = Path("artifacts/playbook/lifecycle"),
    fill_timeout_seconds: int = 20,
    fill_poll_seconds: int = 2,
) -> PlaybookLifecycleSubmitResult:
    """Submit an approved playbook live ticket and arm packet-defined management."""
    ticket = _load_json(live_ticket_artifact)
    packet = read_packet_file(packet_path)
    if not isinstance(packet, ExecutionPacket):
        raise ValueError(f"expected execution packet, found {packet.kind.value}")

    events = event_repository or NullEventRepository()
    trades = trade_state_repository or NullTradeStateRepository()
    lifecycle = lifecycle_store or TradeLifecycleStore()
    blocks = _ticket_blocks(ticket)
    blocks.extend(_packet_blocks(ticket, packet))
    management_spec = _resolve_management_policy(packet, str(ticket.get("selected_management_policy_id", "")))
    if management_spec is None:
        blocks.append(f"management_policy_not_executable:{ticket.get('selected_management_policy_id')}")

    trade_id = str(uuid.uuid4())
    entry_order_id: str | None = None
    stop_order_id: str | None = None
    target_order_id: str | None = None
    stop_price: float | None = None
    target_price: float | None = None
    entry_price = 0.0
    trade_state = "blocked"
    lifecycle_started = False

    if not blocks and management_spec is not None:
        option_symbol = str(ticket["option_symbol"])
        quantity = int(ticket["quantity"])
        requested_limit = float(ticket["limit_price"])
        underlying_entry_price = _optional_float(ticket.get("underlying_entry_price"))
        underlying_stop_price = _optional_float(ticket.get("underlying_stop_price"))
        preflight = await order_manager.preflight_entry(option_symbol, requested_limit, quantity)
        entry_price = _preflight_limit_price(preflight, fallback=requested_limit)
        result = await order_manager.place_entry_order(
            option_symbol,
            entry_price,
            quantity,
            order_id=trade_id,
        )
        entry_order_id = result.order_id
        if entry_order_id is None:
            blocks.append(result.error or "entry_order_submit_failed")
            trade_state = "blocked"
        else:
            await events.append(
                "playbook_live_entry_submitted",
                {
                    "packet_id": packet.packet_id,
                    "packet_version": packet.version,
                    "trade_id": trade_id,
                    "entry_order_id": entry_order_id,
                    "option_symbol": option_symbol,
                    "quantity": quantity,
                    "limit_price": entry_price,
                    "management_policy_id": management_spec.policy_id,
                },
            )
            await trades.upsert_trade(
                TradeRecord(
                    trade_id=trade_id,
                    deployment_id=_deployment_id(packet, ticket),
                    symbol=str(ticket["symbol"]),
                    option_symbol=option_symbol,
                    quantity=quantity,
                    entry_price=entry_price,
                    underlying_entry_price=underlying_entry_price,
                    entry_timestamp=datetime.now(UTC),
                    status="pending_entry",
                    entry_order_id=entry_order_id,
                )
            )
            lifecycle.begin_entry(str(ticket["symbol"]), _deployment_id(packet, ticket), option_symbol=option_symbol, order_id=entry_order_id)
            filled, fill_payload, fill_error = await order_manager.wait_for_fill(
                entry_order_id,
                timeout_seconds=fill_timeout_seconds,
                poll_seconds=fill_poll_seconds,
            )
            await events.append(
                "playbook_live_entry_fill_check",
                {
                    "trade_id": trade_id,
                    "entry_order_id": entry_order_id,
                    "filled": filled,
                    "error": fill_error,
                    "payload": fill_payload or {},
                },
            )
            if not filled:
                trade_state = "pending_entry_reconcile"
                await trades.upsert_trade(
                    TradeRecord(
                        trade_id=trade_id,
                        deployment_id=_deployment_id(packet, ticket),
                        symbol=str(ticket["symbol"]),
                        option_symbol=option_symbol,
                        quantity=quantity,
                        entry_price=entry_price,
                        underlying_entry_price=underlying_entry_price,
                        entry_timestamp=datetime.now(UTC),
                        status=trade_state,
                        entry_order_id=entry_order_id,
                    )
                )
                lifecycle.mark_reconciliation_hold(
                    str(ticket["symbol"]),
                    _deployment_id(packet, ticket),
                    option_symbol=option_symbol,
                    order_id=entry_order_id,
                )
            else:
                entry_price = _filled_entry_price(fill_payload, fallback=entry_price)
                stop_price = round_price(entry_price * (1.0 - management_spec.stop_loss_pct))
                stop_result = await order_manager.place_stop_loss_order(option_symbol, stop_price, quantity)
                stop_order_id = stop_result.order_id
                target_price = round_price(entry_price * (1.0 + management_spec.stop_loss_pct * management_spec.target_r))
                target_order_id = None
                if bool(getattr(order_manager, "supports_concurrent_exit_orders", False)):
                    target_result = await order_manager.place_target_order(option_symbol, target_price, quantity)
                    target_order_id = target_result.order_id
                trade_state = "target_active" if stop_order_id and target_order_id else ("open_protected" if stop_order_id else "open_unprotected")
                await trades.upsert_trade(
                    TradeRecord(
                        trade_id=trade_id,
                        deployment_id=_deployment_id(packet, ticket),
                        symbol=str(ticket["symbol"]),
                        option_symbol=option_symbol,
                        quantity=quantity,
                        entry_price=entry_price,
                        underlying_entry_price=underlying_entry_price,
                        entry_timestamp=datetime.now(UTC),
                        status=trade_state,
                        entry_order_id=entry_order_id,
                        stop_order_id=stop_order_id,
                        stop_price=stop_price,
                        target_order_id=target_order_id,
                        target_price=target_price,
                    )
                )
                if target_order_id:
                    lifecycle.mark_target_active(str(ticket["symbol"]), _deployment_id(packet, ticket), option_symbol=option_symbol, order_id=target_order_id)
                else:
                    lifecycle.mark_open(
                        str(ticket["symbol"]),
                        _deployment_id(packet, ticket),
                        option_symbol=option_symbol,
                        order_id=stop_order_id or entry_order_id,
                        protected=bool(stop_order_id),
                    )
                await events.append(
                    "playbook_lifecycle_management_armed",
                    {
                        "trade_id": trade_id,
                        "entry_order_id": entry_order_id,
                        "stop_order_id": stop_order_id,
                        "stop_price": stop_price,
                        "target_order_id": target_order_id,
                        "target_price": target_price,
                        "target_order_mode": "broker_order" if target_order_id else "virtual_target",
                        "management_policy_id": management_spec.policy_id,
                        "underlying_entry_price": underlying_entry_price,
                        "underlying_stop_price": underlying_stop_price,
                    },
                )
                lifecycle_started = bool(stop_order_id)

    status = _status_for(blocks, trade_state, lifecycle_started)
    artifact_dir = out_root / _lifecycle_id(packet, ticket, trade_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    spec_payload = asdict(management_spec) if management_spec is not None else {}
    result_payload = PlaybookLifecycleSubmitResult(
        status=status,
        lifecycle_started=lifecycle_started,
        packet_id=packet.packet_id,
        packet_version=packet.version,
        symbol=str(ticket.get("symbol", "")),
        direction=str(ticket.get("direction", "")),
        option_symbol=str(ticket.get("option_symbol", "")),
        quantity=int(ticket.get("quantity", 0) or 0),
        limit_price=float(ticket.get("limit_price", 0.0) or 0.0),
        entry_price=entry_price,
        underlying_entry_price=_optional_float(ticket.get("underlying_entry_price")),
        underlying_stop_price=_optional_float(ticket.get("underlying_stop_price")),
        trade_id=trade_id,
        entry_order_id=entry_order_id,
        stop_order_id=stop_order_id,
        stop_price=stop_price,
        target_order_id=target_order_id,
        target_price=target_price,
        management_policy_id=str(ticket.get("selected_management_policy_id", "")),
        management_spec=spec_payload,
        trade_state=trade_state,
        block_reasons=blocks,
        artifact_json=str(artifact_dir / "playbook_lifecycle_submission.json"),
        artifact_md=str(artifact_dir / "PLAYBOOK_LIFECYCLE_SUBMISSION.md"),
    )
    _write_lifecycle_artifacts(result_payload)
    return result_payload


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object at {path}")
    return payload


def _ticket_blocks(ticket: dict[str, Any]) -> list[str]:
    blocks: list[str] = []
    if ticket.get("status") != "live_ticket_approved":
        blocks.append(f"live_ticket_not_approved:{ticket.get('status')}")
    if ticket.get("order_submission_allowed") is not True:
        blocks.append("live_ticket_order_submission_not_allowed")
    if not str(ticket.get("option_symbol", "")).strip():
        blocks.append("option_symbol_missing")
    if int(ticket.get("quantity", 0) or 0) <= 0:
        blocks.append("quantity_missing")
    if float(ticket.get("limit_price", 0.0) or 0.0) <= 0:
        blocks.append("limit_price_missing")
    if ticket.get("underlying_stop_price") is None:
        blocks.append("underlying_stop_price_missing")
    return blocks


def _packet_blocks(ticket: dict[str, Any], packet: ExecutionPacket) -> list[str]:
    blocks: list[str] = []
    if packet.status != PacketStatus.APPROVED:
        blocks.append(f"packet_status_not_approved:{packet.status.value}")
    if packet.operator_approval.status != "approved":
        blocks.append("packet_operator_approval_missing")
    if packet.packet_id != ticket.get("packet_id") or packet.version != ticket.get("packet_version"):
        blocks.append("live_ticket_packet_mismatch")
    if packet.runtime_mode != RuntimeMode.LIVE_APPROVAL_GATED:
        blocks.append(f"packet_runtime_mode_not_live_approval_gated:{packet.runtime_mode.value}")
    controls = packet.runtime_controls
    if controls.get("shadow_only") is not False:
        blocks.append("packet_shadow_only_not_disabled")
    if controls.get("live_automated_allowed") is not False:
        blocks.append("packet_live_automated_boundary_missing")
    if controls.get("live_ticket_required") is not True:
        blocks.append("packet_live_ticket_required_missing")
    return blocks


def _resolve_management_policy(packet: ExecutionPacket, policy_id: str) -> ManagementLifecycleSpec | None:
    specs = packet.runtime_controls.get("management_policy_specs")
    if isinstance(specs, dict) and isinstance(specs.get(policy_id), dict):
        raw = ManagementPolicySpec.model_validate(specs[policy_id])
        return ManagementLifecycleSpec(
            policy_id=policy_id,
            stop_family=raw.stop_family,
            stop_anchor=raw.stop_anchor,
            exit_family=raw.exit_family,
            target_model=raw.target_model,
            stop_loss_pct=float(raw.option_stop_fallback_pct),
            option_stop_fallback_pct=float(raw.option_stop_fallback_pct),
            target_r=float(raw.target_r),
            hard_flat_time_et=raw.hard_flat_time_et,
            target_order_mode=raw.target_order_mode,
            source_config_id=raw.source_config_id,
            source="packet_runtime_controls",
        )
    if packet.runtime_controls.get("management_policy_specs_required") is True:
        return None
    defaults = {
        "reversal_extreme__fixed_1r": 1.0,
        "immediate_entry_bar_failure__fixed_2r": 2.0,
    }
    if policy_id not in defaults:
        return None
    return ManagementLifecycleSpec(
        policy_id=policy_id,
        stop_family=policy_id.split("__", 1)[0],
        stop_anchor="option_premium_fallback",
        exit_family=policy_id.split("__", 1)[1] if "__" in policy_id else "unknown",
        target_model="fixed_r",
        stop_loss_pct=0.45,
        option_stop_fallback_pct=0.45,
        target_r=defaults[policy_id],
        hard_flat_time_et="15:55",
        target_order_mode="virtual_or_broker",
        source_config_id=None,
        source="bhiksha_default_playbook_policy_v1",
    )


def _preflight_limit_price(preflight: PreflightCheck, *, fallback: float) -> float:
    raw = preflight.payload.get("limitPrice")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return fallback


def _filled_entry_price(payload: dict[str, Any] | None, *, fallback: float) -> float:
    if not payload:
        return fallback
    for key in ("averageFillPrice", "averagePrice", "filledPrice", "price"):
        value = payload.get(key)
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue
    return fallback


def _optional_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _deployment_id(packet: ExecutionPacket, ticket: dict[str, Any]) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", packet.packet_id).strip("_").lower()
    symbol = str(ticket.get("symbol", "UNK")).lower()
    direction = str(ticket.get("direction", "unknown")).lower()
    return f"playbook_{slug}_{symbol}_{direction}_live"


def _status_for(blocks: list[str], trade_state: str, lifecycle_started: bool) -> str:
    if blocks:
        return "blocked"
    if lifecycle_started:
        return "lifecycle_started"
    if trade_state == "pending_entry_reconcile":
        return "pending_entry_reconcile"
    return "submitted"


def _lifecycle_id(packet: ExecutionPacket, ticket: dict[str, Any], trade_id: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    raw = f"{packet.packet_id}_{ticket.get('symbol', 'UNK')}_{ticket.get('direction', 'unknown')}_{trade_id}"
    slug = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_")[:96]
    return f"{stamp}_{slug}"


def _write_lifecycle_artifacts(result: PlaybookLifecycleSubmitResult) -> None:
    payload = asdict(result) | {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "lane": "live",
        "lifecycle_owner": "bhiksha",
    }
    Path(result.artifact_json).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    Path(result.artifact_md).write_text(_lifecycle_markdown(payload), encoding="utf-8")


def _lifecycle_markdown(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Playbook Lifecycle Submission",
            "",
            f"- status: `{payload['status']}`",
            f"- lifecycle_owner: `{payload['lifecycle_owner']}`",
            f"- packet: `{payload['packet_id']}` v`{payload['packet_version']}`",
            f"- symbol: `{payload['symbol']}`",
            f"- direction: `{payload['direction']}`",
            f"- option_symbol: `{payload['option_symbol']}`",
            f"- quantity: `{payload['quantity']}`",
            f"- entry_order_id: `{payload['entry_order_id']}`",
            f"- stop_order_id: `{payload['stop_order_id']}`",
            f"- stop_price: `{payload['stop_price']}`",
            f"- target_order_id: `{payload['target_order_id']}`",
            f"- target_price: `{payload['target_price']}`",
            f"- management_policy_id: `{payload['management_policy_id']}`",
            f"- trade_state: `{payload['trade_state']}`",
            f"- block_reasons: `{', '.join(payload['block_reasons'])}`",
            "",
        ]
    )
