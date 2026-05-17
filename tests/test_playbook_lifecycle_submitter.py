from __future__ import annotations

import asyncio
import json
from pathlib import Path

from bhiksha.domain.models import TradeRecord
from bhiksha.execution.order_manager import OrderResult, PreflightCheck
from bhiksha.packets.playbook_lifecycle import submit_playbook_live_ticket
from bhiksha.shared_kernel import ensure_kernel_on_path
from bhiksha.state.lifecycle import LifecycleState, TradeLifecycleStore
from bhiksha.tools.submit_playbook_live_ticket import main as submit_main

ensure_kernel_on_path()

from mala_bhiksha_kernel import (  # noqa: E402
    ExecutionPacket,
    FeatureContract,
    FeatureSpec,
    OperatorApproval,
    PacketKind,
    PacketLineage,
    PacketRef,
    PacketStatus,
    RuntimeMode,
    SourceArtifact,
    write_packet,
)


class StubOrderManager:
    def __init__(
        self,
        *,
        filled: bool = True,
        supports_targets: bool = False,
        stop_order_id: str | None = "STOP123",
        close_order_id: str | None = "EXIT123",
    ) -> None:
        self.supports_concurrent_exit_orders = supports_targets
        self.preflight_calls = 0
        self.entry_calls = 0
        self.stop_calls = 0
        self.target_calls = 0
        self.close_calls = 0
        self.filled = filled
        self.stop_order_id = stop_order_id
        self.close_order_id = close_order_id

    async def preflight_entry(self, option_symbol: str, limit_price: float, quantity: int):
        self.preflight_calls += 1
        return PreflightCheck(
            payload={"limitPrice": f"{limit_price:.2f}"},
            current_increment=0.05,
            buying_power_requirement=limit_price * quantity * 100,
            estimated_cost=limit_price * quantity * 100,
        )

    async def place_entry_order(
        self,
        option_symbol: str,
        limit_price: float,
        quantity: int,
        *,
        order_id: str | None = None,
    ):
        self.entry_calls += 1
        return OrderResult(order_id=order_id or "ENTRY123")

    async def wait_for_fill(self, order_id: str, *, timeout_seconds: int = 20, poll_seconds: int = 2):
        if not self.filled:
            return False, {"status": "SUBMITTED"}, "fill_timeout"
        return True, {"status": "FILLED", "averageFillPrice": "2.90"}, None

    async def place_stop_loss_order(
        self,
        option_symbol: str,
        stop_price: float,
        quantity: int,
        *,
        order_id: str | None = None,
    ):
        self.stop_calls += 1
        return OrderResult(order_id=self.stop_order_id, error=None if self.stop_order_id else "stop_rejected")

    async def place_target_order(
        self,
        option_symbol: str,
        limit_price: float,
        quantity: int,
        *,
        order_id: str | None = None,
    ):
        self.target_calls += 1
        return OrderResult(order_id="TARGET123")

    async def place_close_order(self, option_symbol: str, quantity: int, *, exit_mode, limit_price=None, order_id=None):
        self.close_calls += 1
        return OrderResult(order_id=self.close_order_id, error=None if self.close_order_id else "close_rejected")


class RecordingEvents:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def append(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))


class RecordingTrades:
    def __init__(self) -> None:
        self.records: list[TradeRecord] = []

    async def upsert_trade(self, record: TradeRecord) -> None:
        self.records.append(record)

    async def mark_closed(self, trade_id: str, **kwargs) -> None:
        return None

    async def get_open_trades(self) -> list[TradeRecord]:
        return [record for record in self.records if record.status != "closed"]

    async def get_recent_trades(self, *, limit: int = 100) -> list[TradeRecord]:
        return list(reversed(self.records))[:limit]


def test_playbook_lifecycle_blocks_current_shadow_packet_without_order_calls(tmp_path: Path) -> None:
    ticket_path = _write_live_ticket(tmp_path)
    packet_path = write_packet(tmp_path, _execution_packet(runtime_mode=RuntimeMode.SHADOW, shadow_only=True))
    order_manager = StubOrderManager()

    result = asyncio.run(
        submit_playbook_live_ticket(
            live_ticket_artifact=ticket_path,
            packet_path=packet_path,
            order_manager=order_manager,
            event_repository=RecordingEvents(),
            trade_state_repository=RecordingTrades(),
            lifecycle_store=TradeLifecycleStore(),
            out_root=tmp_path / "lifecycle",
        )
    )

    assert result.status == "blocked"
    assert "packet_runtime_mode_not_live_approval_gated:shadow" in result.block_reasons
    assert "packet_shadow_only_not_disabled" in result.block_reasons
    assert order_manager.entry_calls == 0


def test_playbook_lifecycle_submits_entry_and_arms_virtual_target(tmp_path: Path) -> None:
    ticket_path = _write_live_ticket(tmp_path)
    packet_path = write_packet(tmp_path, _execution_packet())
    events = RecordingEvents()
    trades = RecordingTrades()
    lifecycle = TradeLifecycleStore()
    order_manager = StubOrderManager(supports_targets=False)

    result = asyncio.run(
        submit_playbook_live_ticket(
            live_ticket_artifact=ticket_path,
            packet_path=packet_path,
            order_manager=order_manager,
            event_repository=events,
            trade_state_repository=trades,
            lifecycle_store=lifecycle,
            out_root=tmp_path / "lifecycle",
        )
    )

    assert result.status == "lifecycle_started"
    assert result.lifecycle_started is True
    assert result.entry_order_id is not None
    assert result.stop_order_id == "STOP123"
    assert result.stop_price == 1.6
    assert result.target_order_id is None
    assert result.target_price == 4.21
    assert result.trade_state == "open_protected"
    assert result.management_spec["target_r"] == 1.0
    assert result.management_spec["stop_anchor"] == "underlying_reversal_extreme"
    assert result.management_spec["source"] == "packet_runtime_controls"
    assert order_manager.preflight_calls == 1
    assert order_manager.entry_calls == 1
    assert order_manager.stop_calls == 1
    assert order_manager.target_calls == 0
    assert trades.records[-1].status == "open_protected"
    assert lifecycle.get("IWM", result.management_spec and _deployment_id()).state == LifecycleState.OPEN_PROTECTED
    assert events.events[-1][0] == "playbook_lifecycle_management_armed"
    assert Path(result.artifact_json).exists()


def test_playbook_lifecycle_places_broker_target_when_supported(tmp_path: Path) -> None:
    ticket_path = _write_live_ticket(tmp_path, policy_id="immediate_entry_bar_failure__fixed_2r")
    packet_path = write_packet(tmp_path, _execution_packet())
    order_manager = StubOrderManager(supports_targets=True)

    result = asyncio.run(
        submit_playbook_live_ticket(
            live_ticket_artifact=ticket_path,
            packet_path=packet_path,
            order_manager=order_manager,
            event_repository=RecordingEvents(),
            trade_state_repository=RecordingTrades(),
            lifecycle_store=TradeLifecycleStore(),
            out_root=tmp_path / "lifecycle",
        )
    )

    assert result.status == "lifecycle_started"
    assert result.target_order_id == "TARGET123"
    assert result.target_price == 5.51
    assert result.trade_state == "target_active"
    assert result.management_spec["target_r"] == 2.0
    assert order_manager.target_calls == 1


def test_playbook_lifecycle_records_reconciliation_when_entry_does_not_fill(tmp_path: Path) -> None:
    ticket_path = _write_live_ticket(tmp_path)
    packet_path = write_packet(tmp_path, _execution_packet())
    trades = RecordingTrades()

    result = asyncio.run(
        submit_playbook_live_ticket(
            live_ticket_artifact=ticket_path,
            packet_path=packet_path,
            order_manager=StubOrderManager(filled=False),
            event_repository=RecordingEvents(),
            trade_state_repository=trades,
            lifecycle_store=TradeLifecycleStore(),
            out_root=tmp_path / "lifecycle",
        )
    )

    assert result.status == "pending_entry_reconcile"
    assert result.lifecycle_started is False
    assert result.trade_state == "pending_entry_reconcile"
    assert result.stop_order_id is None
    assert trades.records[-1].status == "pending_entry_reconcile"


def test_playbook_lifecycle_stop_arm_failure_emergency_flattens_and_blocks(tmp_path: Path) -> None:
    ticket_path = _write_live_ticket(tmp_path)
    packet_path = write_packet(tmp_path, _execution_packet())
    trades = RecordingTrades()
    events = RecordingEvents()
    order_manager = StubOrderManager(stop_order_id=None)

    result = asyncio.run(
        submit_playbook_live_ticket(
            live_ticket_artifact=ticket_path,
            packet_path=packet_path,
            order_manager=order_manager,
            event_repository=events,
            trade_state_repository=trades,
            lifecycle_store=TradeLifecycleStore(),
            out_root=tmp_path / "lifecycle",
        )
    )

    assert result.status == "protection_failed_exit_pending"
    assert result.lifecycle_started is False
    assert result.trade_state == "protection_failed_exit_pending"
    assert result.stop_order_id is None
    assert result.emergency_exit_order_id == "EXIT123"
    assert "critical_stop_arm_failed:stop_rejected" in result.block_reasons
    assert order_manager.close_calls == 1
    assert trades.records[-1].status == "protection_failed_exit_pending"
    assert trades.records[-1].exit_order_id == "EXIT123"
    assert events.events[-1][0] == "playbook_lifecycle_stop_arm_failed"


def test_playbook_lifecycle_stop_arm_failure_without_flatten_is_critical(tmp_path: Path) -> None:
    ticket_path = _write_live_ticket(tmp_path)
    packet_path = write_packet(tmp_path, _execution_packet())
    order_manager = StubOrderManager(stop_order_id=None, close_order_id=None)

    result = asyncio.run(
        submit_playbook_live_ticket(
            live_ticket_artifact=ticket_path,
            packet_path=packet_path,
            order_manager=order_manager,
            event_repository=RecordingEvents(),
            trade_state_repository=RecordingTrades(),
            lifecycle_store=TradeLifecycleStore(),
            out_root=tmp_path / "lifecycle",
        )
    )

    assert result.status == "critical_unprotected"
    assert result.lifecycle_started is False
    assert result.emergency_exit_order_id is None
    assert "critical_stop_arm_failed:stop_rejected" in result.block_reasons
    assert "close_rejected" in result.block_reasons


def test_submit_playbook_live_ticket_cli_blocks_shadow_packet(tmp_path: Path) -> None:
    ticket_path = _write_live_ticket(tmp_path)
    packet_path = write_packet(tmp_path, _execution_packet(runtime_mode=RuntimeMode.SHADOW, shadow_only=True))

    code = submit_main(
        [
            "--live-ticket-artifact",
            str(ticket_path),
            "--packet",
            str(packet_path),
            "--db-path",
            str(tmp_path / "bhiksha.db"),
            "--out-root",
            str(tmp_path / "lifecycle"),
        ]
    )

    assert code == 2


def _write_live_ticket(
    tmp_path: Path,
    *,
    policy_id: str = "reversal_extreme__fixed_1r",
) -> Path:
    payload = {
        "status": "live_ticket_approved",
        "decision": "approve",
        "packet_id": "execution.mean_reversion_at_extremes.iwm_qqq",
        "packet_version": 1,
        "symbol": "IWM",
        "direction": "short",
        "timestamp": "2026-05-11 09:40 America/Chicago",
        "selected_management_policy_id": policy_id,
        "option_symbol": "IWM260330P00558000",
        "quantity": 1,
        "limit_price": 2.90,
        "underlying_entry_price": 286.38,
        "underlying_stop_price": 287.10,
        "operator": "Suman",
        "order_submission_allowed": True,
        "live_approval_required": False,
        "block_reasons": [],
    }
    path = tmp_path / "playbook_live_ticket.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _execution_packet(
    *,
    runtime_mode: RuntimeMode = RuntimeMode.LIVE_APPROVAL_GATED,
    shadow_only: bool = False,
) -> ExecutionPacket:
    return ExecutionPacket(
        packet_id="execution.mean_reversion_at_extremes.iwm_qqq",
        version=1,
        status=PacketStatus.APPROVED,
        title="IWM/QQQ Mean Reversion Execution",
        symbol_scope=["IWM", "QQQ"],
        intended_horizon="intraday-short-horizon",
        feature_contract=FeatureContract(
            contract_id="mean_reversion_at_extremes_intraday_v1",
            bar_interval="1m",
            session="rth",
            provider="polygon",
            warmup_bars=60,
            features=[FeatureSpec(name="opening_vwap_rth", provider_sensitive=True)],
        ),
        lineage=PacketLineage(
            source_system="mala_v2",
            source_artifacts=[SourceArtifact(label="test", uri="data/results/playbooks/test")],
        ),
        operator_approval=OperatorApproval(status="approved", actor="operator"),
        source_packet=PacketRef(
            packet_id="playbook.mean_reversion_at_extremes.iwm_qqq",
            version=1,
            kind=PacketKind.PLAYBOOK,
        ),
        runtime_mode=runtime_mode,
        capability_manifest_id="bhiksha.test",
        parity_report_id="parity.mean_reversion.test",
        runtime_controls={
            "allowed_management_policy_ids": [
                "reversal_extreme__fixed_1r",
                "immediate_entry_bar_failure__fixed_2r",
            ],
            "management_policy_specs_required": True,
            "management_policy_specs": {
                "reversal_extreme__fixed_1r": {
                    "policy_id": "reversal_extreme__fixed_1r",
                    "stop_family": "reversal_extreme",
                    "stop_anchor": "underlying_reversal_extreme",
                    "exit_family": "fixed_1r",
                    "target_model": "fixed_r",
                    "target_r": 1.0,
                    "hard_flat_time_et": "15:55",
                    "option_stop_fallback_pct": 0.45,
                    "target_order_mode": "virtual_or_broker",
                    "source_config_id": "cfg_1",
                },
                "immediate_entry_bar_failure__fixed_2r": {
                    "policy_id": "immediate_entry_bar_failure__fixed_2r",
                    "stop_family": "immediate_entry_bar_failure",
                    "stop_anchor": "underlying_entry_bar_failure",
                    "exit_family": "fixed_2r",
                    "target_model": "fixed_r",
                    "target_r": 2.0,
                    "hard_flat_time_et": "15:55",
                    "option_stop_fallback_pct": 0.45,
                    "target_order_mode": "virtual_or_broker",
                    "source_config_id": "cfg_2",
                },
            },
            "shadow_only": shadow_only,
            "live_automated_allowed": False,
            "live_ticket_required": True,
            "operator_must_select_management_policy": True,
            "requires_underlying_stop_price": True,
            "live_management_required": True,
        },
    )


def _deployment_id() -> str:
    return "playbook_execution_mean_reversion_at_extremes_iwm_qqq_iwm_short_live"
