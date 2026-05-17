from __future__ import annotations

from functools import partial
import json
from pathlib import Path
import threading
from http.server import ThreadingHTTPServer
from urllib.request import urlopen

from bhiksha.tools.trader_desk import TraderDeskHandler
from bhiksha.trader_desk.service import TraderDeskConfig, TraderDeskService
from tests.test_playbook_option_preview import (
    _execution_packet as _preview_packet,
    _runtime_controls as _preview_runtime_controls,
    _write_intent,
)
from tests.test_packet_compile import _execution_packet, _supporting_manifest, _write_parity_report

from bhiksha.shared_kernel import ensure_kernel_on_path

ensure_kernel_on_path()
from mala_bhiksha_kernel import write_packet  # noqa: E402
from mala_bhiksha_kernel import RuntimeMode  # noqa: E402


def test_trader_desk_status_exposes_playbook_boundary(tmp_path: Path) -> None:
    service = _service(tmp_path)

    payload = service.status()

    assert payload["safety_boundary"] == "no_live_submission_from_ui_v0"
    assert payload["preflight"]["eligibility"] == "eligible"
    assert payload["playbooks"][0]["id"] == "execution.mean_reversion_at_extremes.iwm_qqq"
    assert "approve_live_ticket" in payload["playbooks"][0]["operator_actions"]
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
    assert payload["option_symbol"] == "IWM260515P00475000"
    assert payload["order_submission_allowed"] is False


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


def _service(tmp_path: Path, *, packet_path: Path | None = None) -> TraderDeskService:
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
        )
    )
