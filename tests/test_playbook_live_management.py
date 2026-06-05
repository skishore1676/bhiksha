from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from bhiksha.domain.enums import ExitMode
from bhiksha.domain.models import TradeRecord
from bhiksha.execution.order_manager import OrderResult
from bhiksha.packets.playbook_live_management import manage_playbook_live_trade
from tests.test_playbook_lifecycle_submitter import RecordingEvents, RecordingTrades, _execution_packet

from bhiksha.shared_kernel import ensure_kernel_on_path

ensure_kernel_on_path()
from mala_bhiksha_kernel import write_packet  # noqa: E402


class StubOrderManager:
    def __init__(self) -> None:
        self.canceled: list[str] = []
        self.close_calls = 0

    async def cancel_order(self, order_id: str):
        self.canceled.append(order_id)
        return True, None

    async def place_close_order(
        self,
        option_symbol: str,
        quantity: int,
        *,
        exit_mode: ExitMode,
        limit_price: float | None = None,
        order_id: str | None = None,
    ):
        self.close_calls += 1
        assert exit_mode == ExitMode.EMERGENCY
        return OrderResult(order_id="EXIT123")


def test_live_management_holds_when_anchor_not_breached(tmp_path: Path) -> None:
    trades = RecordingTrades()
    trade = _open_trade()
    trades.records.append(trade)

    result = asyncio.run(
        manage_playbook_live_trade(
            lifecycle_artifact=_write_lifecycle(tmp_path),
            packet_path=write_packet(tmp_path, _execution_packet()),
            current_underlying_price=286.90,
            order_manager=StubOrderManager(),
            event_repository=RecordingEvents(),
            trade_state_repository=trades,
            current_time=datetime.fromisoformat("2026-05-18T14:00:00+00:00"),
            out_root=tmp_path / "management",
            dry_run=False,
        )
    )

    assert result.status == "hold"
    assert result.action == "hold"
    assert result.trigger_reasons == []


def test_live_management_exits_when_short_anchor_breached(tmp_path: Path) -> None:
    trades = RecordingTrades()
    trades.records.append(_open_trade())
    events = RecordingEvents()
    order_manager = StubOrderManager()

    result = asyncio.run(
        manage_playbook_live_trade(
            lifecycle_artifact=_write_lifecycle(tmp_path),
            packet_path=write_packet(tmp_path, _execution_packet()),
            current_underlying_price=287.25,
            order_manager=order_manager,
            event_repository=events,
            trade_state_repository=trades,
            current_time=datetime.fromisoformat("2026-05-18T14:00:00+00:00"),
            out_root=tmp_path / "management",
            dry_run=False,
        )
    )

    assert result.status == "exit_submitted"
    assert result.trigger_reasons == ["underlying_stop_anchor_breached"]
    assert result.canceled_order_ids == ["STOP123"]
    assert result.exit_order_id == "EXIT123"
    assert trades.records[-1].status == "exit_pending"
    assert trades.records[-1].exit_mode == ExitMode.EMERGENCY
    assert events.events[-1][0] == "playbook_live_management_exit_submitted"
    assert Path(result.artifact_json).exists()


def test_live_management_defaults_to_dry_run(tmp_path: Path) -> None:
    trades = RecordingTrades()
    trades.records.append(_open_trade())
    order_manager = StubOrderManager()

    result = asyncio.run(
        manage_playbook_live_trade(
            lifecycle_artifact=_write_lifecycle(tmp_path),
            packet_path=write_packet(tmp_path, _execution_packet()),
            current_underlying_price=287.25,
            order_manager=order_manager,
            event_repository=RecordingEvents(),
            trade_state_repository=trades,
            current_time=datetime.fromisoformat("2026-05-18T14:00:00+00:00"),
            out_root=tmp_path / "management",
        )
    )

    assert result.status == "exit_would_submit"
    assert result.trigger_reasons == ["underlying_stop_anchor_breached"]
    assert order_manager.close_calls == 0
    assert trades.records[-1].status == "open_protected"


def test_live_management_exits_at_hard_flat_time(tmp_path: Path) -> None:
    trades = RecordingTrades()
    trades.records.append(_open_trade())

    result = asyncio.run(
        manage_playbook_live_trade(
            lifecycle_artifact=_write_lifecycle(tmp_path),
            packet_path=write_packet(tmp_path, _execution_packet()),
            current_underlying_price=286.50,
            order_manager=StubOrderManager(),
            event_repository=RecordingEvents(),
            trade_state_repository=trades,
            current_time=datetime.fromisoformat("2026-05-18T19:56:00+00:00"),
            out_root=tmp_path / "management",
            dry_run=False,
        )
    )

    assert result.status == "exit_submitted"
    assert result.trigger_reasons == ["hard_flat_time_reached"]


def _write_lifecycle(tmp_path: Path) -> Path:
    payload = {
        "status": "lifecycle_started",
        "lifecycle_started": True,
        "packet_id": "execution.mean_reversion_at_extremes.iwm_qqq",
        "packet_version": 1,
        "trade_id": "trade-1",
        "symbol": "IWM",
        "direction": "short",
        "option_symbol": "IWM260330P00558000",
        "quantity": 1,
        "underlying_entry_price": 286.38,
        "underlying_stop_price": 287.10,
        "management_spec": {
            "policy_id": "reversal_extreme__fixed_1r",
            "stop_anchor": "underlying_reversal_extreme",
            "hard_flat_time_et": "15:55",
        },
    }
    path = tmp_path / "playbook_lifecycle_submission.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _open_trade() -> TradeRecord:
    return TradeRecord(
        trade_id="trade-1",
        deployment_id="playbook_execution_mean_reversion_at_extremes_iwm_qqq_iwm_short_live",
        symbol="IWM",
        option_symbol="IWM260330P00558000",
        quantity=1,
        entry_price=2.90,
        underlying_entry_price=286.38,
        status="open_protected",
        entry_order_id="ENTRY123",
        stop_order_id="STOP123",
        stop_price=1.60,
    )
