"""Manage an open live playbook lifecycle from packet-declared rules."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bhiksha.domain.enums import ExitMode
from bhiksha.domain.models import TradeRecord
from bhiksha.execution.order_manager import OrderManager
from bhiksha.persistence.repository import (
    EventRepository,
    NullEventRepository,
    NullTradeStateRepository,
    TradeStateRepository,
)
from bhiksha.shared_kernel import ensure_kernel_on_path

ensure_kernel_on_path()
from mala_bhiksha_kernel import ExecutionPacket, PacketStatus, RuntimeMode, read_packet_file  # noqa: E402


@dataclass(frozen=True, slots=True)
class PlaybookLiveManagementResult:
    status: str
    action: str
    packet_id: str
    packet_version: int
    trade_id: str
    symbol: str
    direction: str
    option_symbol: str
    quantity: int
    current_underlying_price: float | None
    underlying_stop_price: float | None
    hard_flat_time_et: str
    trigger_reasons: list[str]
    block_reasons: list[str]
    canceled_order_ids: list[str]
    exit_order_id: str | None
    trade_state: str
    artifact_json: str
    artifact_md: str


async def manage_playbook_live_trade(
    *,
    lifecycle_artifact: Path,
    packet_path: Path,
    current_underlying_price: float | None,
    order_manager: OrderManager,
    event_repository: EventRepository | None = None,
    trade_state_repository: TradeStateRepository | None = None,
    current_time: datetime | None = None,
    out_root: Path = Path("artifacts/playbook/live_management"),
    dry_run: bool = True,
) -> PlaybookLiveManagementResult:
    """Evaluate underlying-anchor and hard-flat rules for one live playbook trade."""
    lifecycle_payload = _load_json(lifecycle_artifact)
    packet = read_packet_file(packet_path)
    if not isinstance(packet, ExecutionPacket):
        raise ValueError(f"expected execution packet, found {packet.kind.value}")

    events = event_repository or NullEventRepository()
    trades = trade_state_repository or NullTradeStateRepository()
    open_trade = await _resolve_trade(trades, str(lifecycle_payload.get("trade_id", "")))
    blocks = _packet_blocks(packet)
    blocks.extend(_lifecycle_blocks(lifecycle_payload, open_trade))

    management_spec = dict(lifecycle_payload.get("management_spec", {}) or {})
    hard_flat_time_et = str(management_spec.get("hard_flat_time_et", "15:55"))
    direction = str(lifecycle_payload.get("direction", ""))
    underlying_stop_price = _optional_float(lifecycle_payload.get("underlying_stop_price"))
    if underlying_stop_price is None:
        blocks.append("underlying_stop_price_missing")

    now = current_time or datetime.now(UTC)
    trigger_reasons = _trigger_reasons(
        direction=direction,
        current_underlying_price=current_underlying_price,
        underlying_stop_price=underlying_stop_price,
        hard_flat_time_et=hard_flat_time_et,
        now=now,
    )
    action = "hold" if not trigger_reasons else "exit"
    canceled_order_ids: list[str] = []
    exit_order_id: str | None = None
    trade_state = open_trade.status if open_trade is not None else "blocked"

    if blocks:
        status = "blocked"
        action = "block"
    elif action == "hold":
        status = "hold"
    elif open_trade is not None:
        if dry_run:
            status = "exit_would_submit"
            trade_state = open_trade.status
        else:
            cancel_blocks = await _cancel_exit_orders(order_manager, open_trade, canceled_order_ids)
            blocks.extend(cancel_blocks)
            if blocks:
                status = "blocked"
                action = "block"
            else:
                result = await order_manager.place_close_order(
                    str(open_trade.option_symbol),
                    int(open_trade.quantity),
                    exit_mode=ExitMode.EMERGENCY,
                )
                exit_order_id = result.order_id
                if exit_order_id is None:
                    blocks.append(result.error or "exit_order_submit_failed")
                    status = "blocked"
                    action = "block"
                else:
                    trade_state = "exit_pending"
                    await trades.upsert_trade(
                        replace(
                            open_trade,
                            status=trade_state,
                            exit_order_id=exit_order_id,
                            exit_submitted_at=datetime.now(UTC),
                            exit_mode=ExitMode.EMERGENCY,
                        )
                    )
                    await events.append(
                        "playbook_live_management_exit_submitted",
                        {
                            "packet_id": packet.packet_id,
                            "packet_version": packet.version,
                            "trade_id": open_trade.trade_id,
                            "symbol": open_trade.symbol,
                            "option_symbol": open_trade.option_symbol,
                            "exit_order_id": exit_order_id,
                            "trigger_reasons": trigger_reasons,
                            "current_underlying_price": current_underlying_price,
                            "underlying_stop_price": underlying_stop_price,
                            "canceled_order_ids": canceled_order_ids,
                        },
                    )
                    status = "exit_submitted"

    artifact_dir = out_root / _management_id(packet, lifecycle_payload)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    result = PlaybookLiveManagementResult(
        status=status,
        action=action,
        packet_id=packet.packet_id,
        packet_version=packet.version,
        trade_id=str(lifecycle_payload.get("trade_id", "")),
        symbol=str(lifecycle_payload.get("symbol", "")),
        direction=direction,
        option_symbol=str(lifecycle_payload.get("option_symbol", "")),
        quantity=int(lifecycle_payload.get("quantity", 0) or 0),
        current_underlying_price=current_underlying_price,
        underlying_stop_price=underlying_stop_price,
        hard_flat_time_et=hard_flat_time_et,
        trigger_reasons=trigger_reasons,
        block_reasons=blocks,
        canceled_order_ids=canceled_order_ids,
        exit_order_id=exit_order_id,
        trade_state=trade_state,
        artifact_json=str(artifact_dir / "playbook_live_management.json"),
        artifact_md=str(artifact_dir / "PLAYBOOK_LIVE_MANAGEMENT.md"),
    )
    _write_artifacts(result, dry_run=dry_run)
    return result


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object at {path}")
    return payload


async def _resolve_trade(trades: TradeStateRepository, trade_id: str) -> TradeRecord | None:
    if not trade_id:
        return None
    for trade in await trades.get_open_trades():
        if trade.trade_id == trade_id:
            return trade
    return None


def _packet_blocks(packet: ExecutionPacket) -> list[str]:
    blocks: list[str] = []
    if packet.status != PacketStatus.APPROVED:
        blocks.append(f"packet_status_not_approved:{packet.status.value}")
    if packet.operator_approval.status != "approved":
        blocks.append("packet_operator_approval_missing")
    if packet.runtime_mode != RuntimeMode.LIVE_APPROVAL_GATED:
        blocks.append(f"packet_runtime_mode_not_live_approval_gated:{packet.runtime_mode.value}")
    controls = packet.runtime_controls
    if controls.get("shadow_only") is not False:
        blocks.append("packet_shadow_only_not_disabled")
    if controls.get("live_ticket_required") is not True:
        blocks.append("packet_live_ticket_required_missing")
    if controls.get("live_management_required") is not True:
        blocks.append("packet_live_management_required_missing")
    return blocks


def _lifecycle_blocks(payload: dict[str, Any], trade: TradeRecord | None) -> list[str]:
    blocks: list[str] = []
    if payload.get("status") != "lifecycle_started":
        blocks.append(f"lifecycle_status_not_started:{payload.get('status')}")
    if not payload.get("lifecycle_started"):
        blocks.append("lifecycle_not_started")
    if trade is None:
        blocks.append("open_trade_not_found")
    elif trade.status not in {"open_protected", "target_active", "open_unprotected"}:
        blocks.append(f"trade_not_open:{trade.status}")
    if not str(payload.get("option_symbol", "")).strip():
        blocks.append("option_symbol_missing")
    if int(payload.get("quantity", 0) or 0) <= 0:
        blocks.append("quantity_missing")
    return blocks


def _trigger_reasons(
    *,
    direction: str,
    current_underlying_price: float | None,
    underlying_stop_price: float | None,
    hard_flat_time_et: str,
    now: datetime,
) -> list[str]:
    reasons: list[str] = []
    if current_underlying_price is not None and underlying_stop_price is not None:
        if direction == "long" and current_underlying_price <= underlying_stop_price:
            reasons.append("underlying_stop_anchor_breached")
        if direction == "short" and current_underlying_price >= underlying_stop_price:
            reasons.append("underlying_stop_anchor_breached")
    if _is_hard_flat_due(now, hard_flat_time_et):
        reasons.append("hard_flat_time_reached")
    return reasons


def _is_hard_flat_due(now: datetime, hard_flat_time_et: str) -> bool:
    try:
        hour, minute = [int(part) for part in hard_flat_time_et.split(":", 1)]
    except ValueError:
        return False
    et_now = now.astimezone(ZoneInfo("America/New_York"))
    return et_now.time() >= time(hour=hour, minute=minute)


async def _cancel_exit_orders(
    order_manager: OrderManager,
    trade: TradeRecord,
    canceled_order_ids: list[str],
) -> list[str]:
    blocks: list[str] = []
    for order_id in (trade.stop_order_id, trade.target_order_id):
        if not order_id:
            continue
        canceled, error = await order_manager.cancel_order(order_id)
        if canceled:
            canceled_order_ids.append(order_id)
        else:
            blocks.append(f"exit_protection_cancel_failed:{order_id}:{error}")
    return blocks


def _management_id(packet: ExecutionPacket, payload: dict[str, Any]) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    raw = f"{packet.packet_id}_{payload.get('trade_id', 'unknown_trade')}"
    slug = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_")[:96]
    return f"{stamp}_{slug}"


def _write_artifacts(result: PlaybookLiveManagementResult, *, dry_run: bool) -> None:
    payload = asdict(result) | {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "lane": "live",
        "dry_run": dry_run,
    }
    Path(result.artifact_json).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    Path(result.artifact_md).write_text(_markdown(payload), encoding="utf-8")


def _markdown(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Playbook Live Management",
            "",
            f"- status: `{payload['status']}`",
            f"- action: `{payload['action']}`",
            f"- dry_run: `{payload['dry_run']}`",
            f"- packet: `{payload['packet_id']}` v`{payload['packet_version']}`",
            f"- trade_id: `{payload['trade_id']}`",
            f"- symbol: `{payload['symbol']}`",
            f"- direction: `{payload['direction']}`",
            f"- option_symbol: `{payload['option_symbol']}`",
            f"- current_underlying_price: `{payload['current_underlying_price']}`",
            f"- underlying_stop_price: `{payload['underlying_stop_price']}`",
            f"- hard_flat_time_et: `{payload['hard_flat_time_et']}`",
            f"- trigger_reasons: `{', '.join(payload['trigger_reasons'])}`",
            f"- block_reasons: `{', '.join(payload['block_reasons'])}`",
            f"- canceled_order_ids: `{', '.join(payload['canceled_order_ids'])}`",
            f"- exit_order_id: `{payload['exit_order_id']}`",
            f"- trade_state: `{payload['trade_state']}`",
            "",
        ]
    )


def _optional_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
