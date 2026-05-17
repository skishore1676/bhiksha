"""Service layer for the Bhiksha Trader Desk sidecar."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, time
import json
import os
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from bhiksha.shared_kernel import ensure_kernel_on_path

ensure_kernel_on_path()

from bhiksha.app.bootstrap import build_runtime
from bhiksha.domain.models import OptionContractSnapshot
from bhiksha.execution.order_manager import OrderManager
from bhiksha.execution.order_manager import PublicQuote
from bhiksha.market_data.adapters.public import PublicBarSource
from bhiksha.packets.playbook_lifecycle import submit_playbook_live_ticket
from bhiksha.packets.consultation_bridge import consult_mala_playbook
from bhiksha.packets.live_ticket import APPROVAL_PHRASE, create_playbook_live_ticket
from bhiksha.packets.operator_decision import record_playbook_operator_decision
from bhiksha.packets.option_preview import build_playbook_option_preview
from bhiksha.packets.runtime_compile import compile_packet_for_runtime, load_legacy_retirement_report
from bhiksha.persistence.sqlite import SQLiteBackend, SQLiteEventRepository, SQLiteTradeStateRepository
from bhiksha.state.lifecycle import TradeLifecycleStore
from mala_bhiksha_kernel import CapabilityManifest  # noqa: E402


def _default_mala_repo() -> Path:
    bhiksha_repo = Path(__file__).resolve().parents[3]
    candidates = [
        os.getenv("MALA_REPO_ROOT"),
        os.getenv("MALA_V2_REPO_ROOT"),
        str(bhiksha_repo.parent / "mala_v2"),
        str(Path.home() / "Documents" / "mala_v2"),
        str(Path.home() / "code" / "mala_v2"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if (path / "packets").exists():
            return path
    return bhiksha_repo.parent / "mala_v2"


DEFAULT_MALA_REPO = _default_mala_repo()
DEFAULT_PACKET = DEFAULT_MALA_REPO / "packets/execution/execution.mean_reversion_at_extremes.iwm_qqq/v2.json"
DEFAULT_CAPABILITY_MANIFEST = Path("artifacts/capabilities/bhiksha_packet_capabilities_v1.json")
DEFAULT_LEGACY_REPORT = Path("artifacts/legacy_retirement/current.json")
DEFAULT_ARTIFACT_ROOT = Path("artifacts/playbook")
CENTRAL = ZoneInfo("America/Chicago")


@dataclass(frozen=True, slots=True)
class TraderDeskConfig:
    packet: Path = DEFAULT_PACKET
    mala_repo: Path = DEFAULT_MALA_REPO
    capability_manifest: Path = DEFAULT_CAPABILITY_MANIFEST
    legacy_retirement_report: Path = DEFAULT_LEGACY_REPORT
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT
    db_path: Path = Path("bhiksha.db")
    update_mala_log: bool = False
    order_manager_factory: Callable[[], Any] | None = None
    underlying_source_factory: Callable[[], Any] | None = None
    health_provider: Callable[[], dict[str, Any]] | None = None
    require_provider_health_for_submit: bool = True


class TraderDeskService:
    """Small orchestration surface over Bhiksha packet operations."""

    def __init__(self, config: TraderDeskConfig) -> None:
        self.config = config

    def status(self, *, include_health: bool = False) -> dict[str, Any]:
        compile_payload = self.preflight()
        latest = self.latest_artifacts()
        payload: dict[str, Any] = {
            "desk": "bhiksha_trader_desk",
            "safety_boundary": "approval_gated_ui_submit_v1",
            "playbooks": [
                {
                    "id": compile_payload.get("packet_id", ""),
                    "version": compile_payload.get("version", 0),
                    "title": "IWM/QQQ Mean Reversion At Extremes",
                    "symbols": ["IWM", "QQQ"],
                    "runtime_mode": compile_payload.get("runtime_mode", ""),
                    "eligibility": compile_payload.get("eligibility", "unknown"),
                    "executable": compile_payload.get("executable", False),
                    "management_policy_ids": compile_payload.get("management_policy_ids", []),
                    "block_reasons": compile_payload.get("block_reasons", []),
                    "operator_actions": [
                        "set_chart_context",
                        "consult",
                        "take_watch_pass",
                        "select_management_policy",
                        "approve_submit_live_order",
                        "emergency_flatten",
                        "feedback",
                    ],
                    "machine_actions": [
                        "runtime_preflight",
                        "mala_consultation",
                        "option_preview",
                        "risk_gate",
                        "broker_submit",
                        "lifecycle_management",
                        "outcome_capture",
                    ],
                }
            ],
            "preflight": compile_payload,
            "latest": latest,
            "next_step": _suggest_next_step(latest),
        }
        if include_health:
            payload["health"] = self.health()
        return payload

    def live_context(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        symbol = str(payload.get("symbol", "QQQ")).strip().upper() or "QQQ"
        context = {
            "market_timestamp": _market_timestamp(),
            "rth_open": _is_rth_open(),
            "symbol": symbol,
            "preflight": self.preflight(),
            "latest": self.latest_artifacts(),
            "quote": None,
            "health": self._health_payload(),
        }
        try:
            context["quote"] = asyncio.run(self._fetch_underlying_quote(symbol))
        except Exception as exc:
            context["quote"] = {"symbol": symbol, "ok": False, "error": str(exc)}
        return context

    def health(self) -> dict[str, Any]:
        if self.config.health_provider is not None:
            return self.config.health_provider()

        async def _run() -> dict[str, Any]:
            runtime = build_runtime()
            report = await runtime.health_report()
            return {
                "dry_run": report.dry_run,
                "enabled_deployments": report.enabled_deployments,
                "providers": [
                    {"name": item.name, "ok": item.ok, "detail": item.detail}
                    for item in report.provider_health
                ],
            }

        return asyncio.run(_run())

    def preflight(self) -> dict[str, Any]:
        manifest = CapabilityManifest.model_validate_json(
            self.config.capability_manifest.read_text(encoding="utf-8")
        )
        legacy = load_legacy_retirement_report(self.config.legacy_retirement_report)
        result = compile_packet_for_runtime(
            self.config.packet,
            capability_manifest=manifest,
            legacy_retirement_report=legacy,
        )
        return {
            "eligibility": result.eligibility,
            "executable": result.executable,
            "packet_id": result.packet_id,
            "version": result.version,
            "runtime_mode": result.runtime_mode,
            "feature_contract_id": result.feature_contract_id,
            "feature_contract_fingerprint": result.feature_contract_fingerprint,
            "management_policy_ids": result.management_policy_ids or [],
            "block_reasons": result.block_reasons,
        }

    def latest_artifacts(self) -> dict[str, dict[str, str]]:
        root = self.config.artifact_root
        latest = {
            "consultation": _latest(root / "consultations", "consultation_bridge.json"),
            "intent": _latest(root / "intents", "playbook_operator_decision.json"),
            "option_preview": _latest(root / "option_previews", "playbook_option_preview.json"),
            "live_ticket": _latest(root / "live_tickets", "playbook_live_ticket.json"),
            "lifecycle": _latest(root / "lifecycle", "playbook_lifecycle_submission.json"),
            "live_management": _latest(root / "live_management", "playbook_live_management.json"),
        }
        return {key: _artifact_payload(path) for key, path in latest.items()}

    def consult(self, payload: dict[str, Any]) -> dict[str, Any]:
        timestamp = str(payload.get("timestamp", "")).strip() or _market_timestamp()
        result = consult_mala_playbook(
            packet_path=self.config.packet,
            symbol=_required_text(payload, "symbol").upper(),
            direction=_required_text(payload, "direction").lower(),
            timestamp=timestamp,
            chart_read=_required_text(payload, "chart_read"),
            mala_repo=self.config.mala_repo,
            capability_manifest_path=self.config.capability_manifest,
            legacy_retirement_report_path=self.config.legacy_retirement_report,
            out_root=self.config.artifact_root / "consultations",
            update_mala_log=bool(payload.get("update_mala_log", self.config.update_mala_log)),
        )
        return asdict(result)

    def decide(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = record_playbook_operator_decision(
            consultation_artifact=Path(_required_text(payload, "consultation_artifact")),
            decision=_required_text(payload, "decision"),
            selected_management_policy_id=_optional_text(payload, "selected_management_policy_id"),
            operator_note=_required_text(payload, "operator_note"),
            out_root=self.config.artifact_root / "intents",
        )
        return asdict(result)

    def preview_option(self, payload: dict[str, Any]) -> dict[str, Any]:
        async def _run() -> dict[str, Any]:
            preview_mode = str(payload.get("preview_mode", "live")).strip().lower()
            if preview_mode not in {"live", "simulated"}:
                raise ValueError("preview_mode must be live or simulated")
            symbol = str(payload.get("symbol", "QQQ")).strip().upper() or "QQQ"
            direction = str(payload.get("direction", "short")).strip().lower() or "short"
            underlying_price = _optional_float(payload, "underlying_price")
            if preview_mode == "live" and underlying_price is None:
                quote = await self._fetch_underlying_quote(symbol)
                underlying_price = _optional_float(quote, "price")
            chain_service = _SimulatedChainService(symbol, direction) if preview_mode == "simulated" else None
            order_manager = _SimulatedOrderManager() if preview_mode == "simulated" else None
            result = await build_playbook_option_preview(
                intent_artifact=Path(_required_text(payload, "intent_artifact")),
                packet_path=self.config.packet,
                chain_service=chain_service,
                order_manager=order_manager,
                out_root=self.config.artifact_root / "option_previews",
                underlying_price=underlying_price,
                underlying_stop_price=_optional_float(payload, "underlying_stop_price"),
            )
            return asdict(result) | {"preview_mode": preview_mode}

        return asyncio.run(_run())

    def approve_submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("approval_confirmed") is not True:
            raise ValueError("approval_confirmed=true is required")
        health_blocks = self._provider_health_blocks() if self.config.require_provider_health_for_submit else []
        if health_blocks:
            return {
                "status": "blocked",
                "ticket": None,
                "lifecycle": None,
                "block_reasons": health_blocks,
                "artifact_json": "",
                "artifact_md": "",
            }
        ticket = create_playbook_live_ticket(
            option_preview_artifact=Path(_required_text(payload, "option_preview_artifact")),
            decision="approve",
            operator=_optional_text(payload, "operator") or "Suman",
            operator_note=_required_text(payload, "operator_note"),
            approval_phrase=APPROVAL_PHRASE,
            out_root=self.config.artifact_root / "live_tickets",
        )

        async def _run() -> dict[str, Any]:
            backend = SQLiteBackend(str(self.config.db_path))
            order_manager = self._make_order_manager()
            try:
                lifecycle = await submit_playbook_live_ticket(
                    live_ticket_artifact=Path(ticket.artifact_json),
                    packet_path=self.config.packet,
                    order_manager=order_manager,
                    event_repository=SQLiteEventRepository(str(self.config.db_path), backend=backend),
                    trade_state_repository=SQLiteTradeStateRepository(str(self.config.db_path), backend=backend),
                    lifecycle_store=TradeLifecycleStore(),
                    out_root=self.config.artifact_root / "lifecycle",
                )
            finally:
                close = getattr(order_manager, "close", None)
                if close is not None:
                    maybe_coro = close()
                    if hasattr(maybe_coro, "__await__"):
                        await maybe_coro
            return {
                "status": lifecycle.status,
                "ticket": asdict(ticket),
                "lifecycle": asdict(lifecycle),
                "block_reasons": lifecycle.block_reasons,
                "artifact_json": lifecycle.artifact_json,
                "artifact_md": lifecycle.artifact_md,
            }

        return asyncio.run(_run())

    def live_management_status(self) -> dict[str, Any]:
        latest = self.latest_artifacts()
        lifecycle = _latest_payload(latest.get("lifecycle", {}).get("path", ""))
        management = _latest_payload(latest.get("live_management", {}).get("path", ""))
        return {
            "latest": latest,
            "lifecycle": lifecycle,
            "live_management": management,
            "trade_state": lifecycle.get("trade_state") or management.get("trade_state") or "",
            "status": management.get("status") or lifecycle.get("status") or "none",
            "critical": (lifecycle.get("status") or management.get("status")) in {
                "critical_unprotected",
                "protection_failed_exit_pending",
                "blocked",
            },
        }

    def _make_order_manager(self) -> Any:
        if self.config.order_manager_factory is not None:
            return self.config.order_manager_factory()
        return OrderManager()

    def _health_payload(self) -> dict[str, Any]:
        try:
            return self.health()
        except Exception as exc:
            return {"providers": [{"name": "health", "ok": False, "detail": str(exc)}]}

    def _provider_health_blocks(self) -> list[str]:
        health = self._health_payload()
        providers = health.get("providers") or []
        blocks = [
            f"provider_health_failed:{item.get('name', 'unknown')}:{item.get('detail', '')}"
            for item in providers
            if item.get("ok") is not True
        ]
        if not providers:
            blocks.append("provider_health_unavailable")
        return blocks

    async def _fetch_underlying_quote(self, symbol: str) -> dict[str, Any]:
        source = self.config.underlying_source_factory() if self.config.underlying_source_factory else PublicBarSource()
        try:
            price = await source.fetch_live_price(symbol)
        finally:
            close = getattr(source, "close", None)
            if close is not None:
                maybe_coro = close()
                if hasattr(maybe_coro, "__await__"):
                    await maybe_coro
        if price is None:
            raise ValueError(f"could not fetch live price for {symbol}")
        value, timestamp = price
        return {
            "symbol": symbol,
            "ok": True,
            "price": float(value),
            "timestamp": timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp),
            "provider": "public",
        }

    def live_ticket(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = create_playbook_live_ticket(
            option_preview_artifact=Path(_required_text(payload, "option_preview_artifact")),
            decision=_required_text(payload, "decision"),
            operator=_optional_text(payload, "operator") or "Suman",
            operator_note=_required_text(payload, "operator_note"),
            approval_phrase=_optional_text(payload, "approval_phrase"),
            out_root=self.config.artifact_root / "live_tickets",
        )
        return asdict(result) | {"approval_phrase_required": APPROVAL_PHRASE}


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key, "")).strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _optional_text(payload: dict[str, Any], key: str) -> str | None:
    value = str(payload.get(key, "")).strip()
    return value or None


def _optional_float(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    if value in {None, ""}:
        return None
    return float(value)


def _market_now() -> datetime:
    return datetime.now(CENTRAL)


def _market_timestamp() -> str:
    return _market_now().strftime("%Y-%m-%d %H:%M America/Chicago")


def _is_rth_open(now: datetime | None = None) -> bool:
    local = now or _market_now()
    if local.weekday() >= 5:
        return False
    return time(8, 30) <= local.time() <= time(15, 0)


def _latest_payload(path: str) -> dict[str, Any]:
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _latest(root: Path, filename: str) -> Path | None:
    if not root.exists():
        return None
    matches = list(root.glob(f"*/{filename}"))
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def _artifact_payload(path: Path | None) -> dict[str, str]:
    if path is None:
        return {"path": "", "status": ""}
    status = ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            status = str(payload.get("status", ""))
    except (OSError, json.JSONDecodeError):
        status = ""
    return {"path": str(path), "status": status}


def _suggest_next_step(latest: dict[str, dict[str, str]]) -> str:
    management_status = latest["live_management"]["status"]
    lifecycle_status = latest["lifecycle"]["status"]
    ticket_status = latest["live_ticket"]["status"]
    preview_status = latest["option_preview"]["status"]
    intent_status = latest["intent"]["status"]

    if management_status:
        return "review live management artifact"
    if lifecycle_status == "lifecycle_started":
        return "run live management monitor"
    if lifecycle_status in {"pending_entry_reconcile", "protection_failed_exit_pending", "critical_unprotected"}:
        return f"resolve lifecycle risk state: {lifecycle_status}"
    if ticket_status == "live_ticket_approved":
        return "approved live ticket exists; submit from Trader Desk or review lifecycle state"
    if preview_status == "option_preview_ready":
        return "approve or reject live ticket"
    if intent_status in {"live_intent_ready", "shadow_intent_ready"}:
        return "build option preview with underlying stop"
    if intent_status in {"operator_watch", "operator_pass", "blocked"}:
        return "wait for a fresh setup"
    if latest["consultation"]["path"]:
        return "record take/pass decision"
    return "consult when the chart setup appears"


class _SimulatedChainService:
    def __init__(self, symbol: str, direction: str) -> None:
        self.symbol = symbol
        self.contract_type = "PUT" if direction == "short" else "CALL"

    async def get_chain(self, symbol: str, **kwargs):
        return [
            OptionContractSnapshot(
                option_symbol=f"{symbol}260515{self.contract_type[0]}00475000",
                underlying_symbol=symbol,
                contract_type=self.contract_type,
                expiration_date="2026-05-15",
                dte=0,
                strike=475.0,
                delta=-0.31 if self.contract_type == "PUT" else 0.31,
                bid=2.70,
                ask=2.90,
                open_interest=500,
            )
        ]

    async def close(self):
        return None


class _SimulatedOrderManager:
    async def get_option_quote(self, option_symbol: str):
        return PublicQuote(
            symbol=option_symbol,
            bid=2.70,
            ask=2.90,
            last=2.80,
            open_interest=500,
            outcome="SIMULATED",
        )

    async def close(self):
        return None
