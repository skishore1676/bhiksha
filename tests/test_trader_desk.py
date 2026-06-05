from __future__ import annotations

from functools import partial
import asyncio
import json
from pathlib import Path
import threading
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

import pytest

from bhiksha.tools.trader_desk import TraderDeskHandler
import bhiksha.trader_desk.service as service_module
from bhiksha.trader_desk.service import TraderDeskConfig, TraderDeskService
from bhiksha.execution.order_manager import OrderResult, PreflightCheck, PublicQuote
from bhiksha.packets.consultation_bridge import ConsultationBridgeResult
from bhiksha.packets.option_preview import PlaybookOptionPreviewResult
from tests.test_playbook_option_preview import (
    _execution_packet as _preview_packet,
    _runtime_controls as _preview_runtime_controls,
    _write_intent,
)
from tests.test_playbook_shadow_and_live_lanes import _write_preview
from tests.test_packet_compile import _execution_packet, _supporting_manifest, _write_parity_report

from bhiksha.shared_kernel import ensure_kernel_on_path

ensure_kernel_on_path()
from mala_bhiksha_kernel import write_packet  # noqa: E402
from mala_bhiksha_kernel import RuntimeMode  # noqa: E402


def test_trader_desk_status_exposes_playbook_boundary(tmp_path: Path) -> None:
    service = _service(tmp_path)

    payload = service.status()

    assert payload["safety_boundary"] == "approval_gated_ui_submit_v1"
    assert payload["preflight"]["eligibility"] == "eligible"
    assert payload["playbooks"][0]["id"] == "execution.mean_reversion_at_extremes.iwm_qqq"
    assert "approve_submit_live_order" in payload["playbooks"][0]["operator_actions"]
    assert "lifecycle_management" in payload["playbooks"][0]["machine_actions"]
    assert payload["next_step"] == "consult when the chart setup appears"


def test_trader_desk_latest_moves_to_decision_after_consultation(tmp_path: Path) -> None:
    service = _service(tmp_path)
    consultation_dir = tmp_path / "artifacts" / "consultations" / "latest"
    consultation_dir.mkdir(parents=True)
    (consultation_dir / "consultation_bridge.json").write_text(
        json.dumps({"status": "consulted"}),
        encoding="utf-8",
    )

    payload = service.status()

    assert payload["latest"]["consultation"]["status"] == "consulted"
    assert payload["next_step"] == "record take/pass decision"


def test_trader_desk_simulated_option_preview(tmp_path: Path) -> None:
    packet_path = write_packet(
        tmp_path,
        _preview_packet(
            runtime_mode=RuntimeMode.LIVE_APPROVAL_GATED,
            runtime_controls=_preview_runtime_controls(shadow_only=False, live_ticket_required=True),
        ),
    )
    service = _service(tmp_path, packet_path=packet_path)
    intent_path = _write_intent(
        tmp_path,
        status="live_intent_ready",
        execution_mode="live_approval_gated",
    )

    payload = service.preview_option(
        {
            "intent_artifact": str(intent_path),
            "preview_mode": "simulated",
            "symbol": "IWM",
            "direction": "short",
            "underlying_price": 286.38,
            "underlying_stop_price": 287.10,
        }
    )

    assert payload["preview_mode"] == "simulated"
    assert payload["status"] == "option_preview_ready"
    assert payload["option_symbol"] == "IWM260515P00286000"
    assert payload["order_submission_allowed"] is False


def test_trader_desk_consult_defaults_to_market_now(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    captured = {}

    def fake_consult(**kwargs):
        captured.update(kwargs)
        return ConsultationBridgeResult(
            status="consulted",
            packet_id="execution.mean_reversion_at_extremes.iwm_qqq",
            packet_version=1,
            runtime_mode="shadow",
            symbol="QQQ",
            direction="short",
            timestamp=kwargs["timestamp"],
            chart_read=kwargs["chart_read"],
            compile_eligibility="eligible",
            compile_decision="take",
            compile_block_reasons=[],
            query_json="query.json",
            query_review="QUERY.md",
            policy_json="policy.json",
            policy_card="POLICY.md",
            verdict="watch",
            policy="state-management",
            selected_exit="fixed",
            allowed_management_policy_ids=["reversal_extreme__fixed_1r"],
            artifact_json=str(tmp_path / "consultation.json"),
            artifact_md=str(tmp_path / "CONSULTATION.md"),
        )

    monkeypatch.setattr(service_module, "consult_mala_playbook", fake_consult)

    payload = service.consult({"symbol": "QQQ", "direction": "short", "chart_read": "Tape is stretched."})

    assert payload["timestamp"].endswith("America/Chicago")
    assert captured["timestamp"] == payload["timestamp"]


def test_trader_desk_option_preview_fetches_underlying_when_omitted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path, underlying_source_factory=lambda: StubUnderlyingSource(286.38))
    captured = {}

    async def fake_preview(**kwargs):
        captured.update(kwargs)
        return PlaybookOptionPreviewResult(
            status="option_preview_ready",
            preview_ready=True,
            packet_id="execution.mean_reversion_at_extremes.iwm_qqq",
            packet_version=1,
            symbol="IWM",
            direction="short",
            timestamp="2026-05-11 09:40 America/Chicago",
            selected_management_policy_id="reversal_extreme__fixed_1r",
            management_spec={},
            intent_artifact="intent.json",
            option_symbol="IWM260330P00558000",
            quantity=1,
            estimated_entry_price=2.9,
            pricing_evidence={},
            underlying_entry_price=kwargs["underlying_price"],
            underlying_stop_price=kwargs["underlying_stop_price"],
            risk_reasons=["approved"],
            block_reasons=[],
            order_submission_allowed=False,
            live_approval_required=True,
            artifact_json=str(tmp_path / "preview.json"),
            artifact_md=str(tmp_path / "PREVIEW.md"),
        )

    monkeypatch.setattr(service_module, "build_playbook_option_preview", fake_preview)

    result = service.preview_option(
        {
            "intent_artifact": str(tmp_path / "intent.json"),
            "preview_mode": "live",
            "symbol": "IWM",
            "direction": "short",
            "underlying_stop_price": 287.1,
        }
    )

    assert captured["underlying_price"] == 286.38
    assert result["underlying_entry_price"] == 286.38


def test_trader_desk_approve_submit_starts_lifecycle_with_mocked_order_manager(tmp_path: Path) -> None:
    packet_path = write_packet(
        tmp_path,
        _preview_packet(
            runtime_mode=RuntimeMode.LIVE_APPROVAL_GATED,
            runtime_controls=_preview_runtime_controls(shadow_only=False, live_ticket_required=True),
        ),
    )
    order_manager = StubLifecycleOrderManager()
    service = _service(
        tmp_path,
        packet_path=packet_path,
        order_manager_factory=lambda: order_manager,
        health_provider=lambda: {"providers": [{"name": "public", "ok": True, "detail": "ok"}]},
    )
    preview_path = _write_preview(tmp_path)

    payload = service.approve_submit(
        {
            "option_preview_artifact": str(preview_path),
            "approval_confirmed": True,
            "operator_note": "Submit one contract.",
        }
    )

    assert payload["status"] == "lifecycle_started"
    assert payload["lifecycle"]["entry_order_id"] == "ENTRY123"
    assert payload["lifecycle"]["stop_order_id"] == "STOP123"
    assert order_manager.entry_calls == 1


def test_trader_desk_approve_submit_blocks_when_provider_health_fails(tmp_path: Path) -> None:
    packet_path = write_packet(
        tmp_path,
        _preview_packet(
            runtime_mode=RuntimeMode.LIVE_APPROVAL_GATED,
            runtime_controls=_preview_runtime_controls(shadow_only=False, live_ticket_required=True),
        ),
    )
    order_manager = StubLifecycleOrderManager()
    service = _service(
        tmp_path,
        packet_path=packet_path,
        order_manager_factory=lambda: order_manager,
        health_provider=lambda: {"providers": [{"name": "public", "ok": False, "detail": "auth_failed"}]},
    )
    preview_path = _write_preview(tmp_path)

    payload = service.approve_submit(
        {
            "option_preview_artifact": str(preview_path),
            "approval_confirmed": True,
            "operator_note": "Submit one contract.",
        }
    )

    assert payload["status"] == "blocked"
    assert payload["block_reasons"] == ["provider_health_failed:public:auth_failed"]
    assert order_manager.entry_calls == 0


def test_oldmac_trader_desk_launcher_uses_localhost_tunnel_only() -> None:
    script = Path("scripts/open_oldmac_trader_desk.sh").read_text(encoding="utf-8")

    assert "--host 127.0.0.1" in script
    assert "127.0.0.1:${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" in script
    assert "0.0.0.0" not in script


def test_trader_desk_http_status_endpoint() -> None:
    class FakeService:
        def status(self, *, include_health: bool = False):
            return {"status": "ok", "include_health": include_health}

        def health(self):
            return {"providers": []}

        def preflight(self):
            return {"eligibility": "eligible"}

        def latest_artifacts(self):
            return {}

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(TraderDeskHandler, service=FakeService()),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_port
        with urlopen(f"http://127.0.0.1:{port}/api/status?health=1", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert payload == {"include_health": True, "status": "ok"}


def _service(
    tmp_path: Path,
    *,
    packet_path: Path | None = None,
    order_manager_factory=None,
    underlying_source_factory=None,
    health_provider=None,
) -> TraderDeskService:
    _write_parity_report(tmp_path)
    if packet_path is None:
        packet_path = write_packet(tmp_path, _execution_packet())
    manifest_path = tmp_path / "capabilities.json"
    manifest_path.write_text(_supporting_manifest().model_dump_json(), encoding="utf-8")
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text(
        json.dumps({"status": "clear", "active_legacy_wire_count": 0}),
        encoding="utf-8",
    )
    return TraderDeskService(
        TraderDeskConfig(
            packet=packet_path,
            mala_repo=tmp_path / "mala_v2",
            capability_manifest=manifest_path,
            legacy_retirement_report=legacy_path,
            artifact_root=tmp_path / "artifacts",
            db_path=tmp_path / "bhiksha.db",
            order_manager_factory=order_manager_factory,
            underlying_source_factory=underlying_source_factory,
            health_provider=health_provider,
        )
    )


class StubUnderlyingSource:
    def __init__(self, price: float) -> None:
        self.price = price

    async def fetch_live_price(self, symbol: str):
        return self.price, service_module.datetime(2026, 5, 11, 14, 40, tzinfo=service_module.UTC)

    async def close(self):
        return None


class StubLifecycleOrderManager:
    supports_concurrent_exit_orders = False

    def __init__(self) -> None:
        self.entry_calls = 0
        self.stop_calls = 0

    async def preflight_entry(self, option_symbol: str, limit_price: float, quantity: int):
        return PreflightCheck(payload={"limitPrice": f"{limit_price:.2f}"})

    async def get_option_quote(self, option_symbol: str):
        return PublicQuote(symbol=option_symbol, bid=2.70, ask=2.90, last=2.80, open_interest=500)

    async def place_entry_order(self, option_symbol: str, limit_price: float, quantity: int, *, order_id: str | None = None):
        self.entry_calls += 1
        return OrderResult(order_id="ENTRY123")

    async def wait_for_fill(self, order_id: str, *, timeout_seconds: int = 20, poll_seconds: int = 2):
        return True, {"averageFillPrice": 2.90}, None

    async def place_stop_loss_order(self, option_symbol: str, stop_price: float, quantity: int, *, order_id: str | None = None):
        self.stop_calls += 1
        return OrderResult(order_id="STOP123")

    async def place_close_order(self, *args, **kwargs):
        return OrderResult(order_id="EXIT123")

    async def close(self):
        return None
