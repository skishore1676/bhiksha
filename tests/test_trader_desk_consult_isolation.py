from __future__ import annotations

from dataclasses import dataclass
import ast
import inspect
from pathlib import Path

import pytest

from bhiksha.tools import trader_desk_consult
from bhiksha.trader_desk import consult_service
from bhiksha.trader_desk.consult_service import (
    BrokerInertConsultationService,
    ConsultServiceConfig,
)


def test_consultation_process_has_no_trading_runtime_imports() -> None:
    source = inspect.getsource(consult_service) + inspect.getsource(
        trader_desk_consult
    )
    for forbidden in (
        "bhiksha.app.bootstrap",
        "bhiksha.execution",
        "bhiksha.market_data.adapters.public",
        "bhiksha.persistence",
        "bhiksha.state.lifecycle",
        "approve_submit",
        "live_ticket",
        "option_preview",
        "broker_submit",
    ):
        assert forbidden not in source

    assert '"/api/consult"' in source
    assert '"/api/status"' in source
    assert '"/api/preflight"' in source
    assert '"/api/latest"' in source


def test_consultation_transitive_bhiksha_import_graph_has_no_money_path() -> None:
    reachable = _reachable_bhiksha_modules(
        {
            "bhiksha.tools.trader_desk_consult",
            "bhiksha.trader_desk.consult_service",
        }
    )
    forbidden_prefixes = (
        "bhiksha.app.bootstrap",
        "bhiksha.execution",
        "bhiksha.persistence",
        "bhiksha.state.lifecycle",
        "bhiksha.integrations",
    )
    assert not {
        module
        for module in reachable
        if module.startswith(forbidden_prefixes)
    }
    assert "bhiksha.packets.consultation_bridge" in reachable
    assert "bhiksha.packets.runtime_compile" in reachable


def test_consultation_process_refuses_non_loopback() -> None:
    with pytest.raises(SystemExit):
        trader_desk_consult.main(
            [
                "--host",
                "0.0.0.0",
                "--packet",
                "packet.json",
                "--mala-repo",
                "mala",
                "--capability-manifest",
                "capabilities.json",
                "--legacy-retirement-report",
                "legacy.json",
            ]
        )


def test_consultation_launchers_are_dedicated_and_loopback_only() -> None:
    start = Path("scripts/start_trader_desk_consult_only.sh").read_text(
        encoding="utf-8"
    )
    installer = Path(
        "scripts/install_trader_desk_consult_only_oldmac.sh"
    ).read_text(encoding="utf-8")

    assert "bhiksha.tools.trader_desk_consult" in start
    assert "bhiksha.tools.trader_desk " not in start
    assert "--host 127.0.0.1" in start
    assert "v1.json" in start
    assert "v2.json" not in start
    assert "com.bhiksha.trader-desk-consult" in installer
    assert "start_trader_desk_consult_only.sh" in installer
    assert "live-start" not in installer


@dataclass(frozen=True)
class _Result:
    status: str = "consulted"
    verdict: str = "pass"
    policy: str = "none"
    selected_exit: str = "none"


def test_consultation_service_forces_broker_inert_request(monkeypatch) -> None:
    captured = {}

    def fake_consult(**kwargs):
        captured.update(kwargs)
        return _Result()

    monkeypatch.setattr(consult_service, "consult_mala_playbook", fake_consult)
    service = object.__new__(BrokerInertConsultationService)
    service.config = ConsultServiceConfig(
        packet=Path("packet.json"),
        mala_repo=Path("mala"),
        capability_manifest=Path("capabilities.json"),
        legacy_retirement_report=Path("legacy.json"),
        artifact_root=Path("artifacts"),
    )

    result = service.consult(
        {
            "symbol": "IWM",
            "direction": "long",
            "chart_read": "VWAP reclaim with a defined low",
            "timestamp": "2026-08-03T09:40:00-05:00",
        }
    )

    assert result["status"] == "consulted"
    assert captured["update_mala_log"] is False
    assert captured["symbol"] == "IWM"
    assert captured["direction"] == "long"


@pytest.mark.parametrize(
    "payload",
    [
        {"symbol": "SPY", "direction": "long", "chart_read": "x"},
        {"symbol": "IWM", "direction": "call", "chart_read": "x"},
        {
            "symbol": "IWM",
            "direction": "long",
            "chart_read": "x",
            "submit": True,
        },
    ],
)
def test_consultation_service_rejects_scope_expansion(payload) -> None:
    service = object.__new__(BrokerInertConsultationService)
    service.config = ConsultServiceConfig(
        packet=Path("packet.json"),
        mala_repo=Path("mala"),
        capability_manifest=Path("capabilities.json"),
        legacy_retirement_report=Path("legacy.json"),
        artifact_root=Path("artifacts"),
    )
    with pytest.raises(ValueError):
        service.consult(payload)


def _reachable_bhiksha_modules(seeds: set[str]) -> set[str]:
    src_root = Path("src")
    pending = list(seeds)
    visited: set[str] = set()
    while pending:
        module = pending.pop()
        if module in visited:
            continue
        visited.add(module)
        path = _module_path(src_root, module)
        if path is None:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module]
            for target in imported:
                if target == "bhiksha" or target.startswith("bhiksha."):
                    pending.append(target)
                    parts = target.split(".")
                    for size in range(1, len(parts)):
                        pending.append(".".join(parts[:size]))
    return visited


def _module_path(src_root: Path, module: str) -> Path | None:
    relative = Path(*module.split("."))
    file_path = src_root / relative.with_suffix(".py")
    if file_path.exists():
        return file_path
    init_path = src_root / relative / "__init__.py"
    return init_path if init_path.exists() else None
