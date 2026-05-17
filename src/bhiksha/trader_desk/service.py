"""Service layer for the Bhiksha Trader Desk sidecar."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
from typing import Any

from bhiksha.app.bootstrap import build_runtime
from bhiksha.domain.models import OptionContractSnapshot
from bhiksha.execution.order_manager import PublicQuote
from bhiksha.packets.consultation_bridge import consult_mala_playbook
from bhiksha.packets.live_ticket import APPROVAL_PHRASE, create_playbook_live_ticket
from bhiksha.packets.operator_decision import record_playbook_operator_decision
from bhiksha.packets.option_preview import build_playbook_option_preview
from bhiksha.packets.runtime_compile import compile_packet_for_runtime, load_legacy_retirement_report
from bhiksha.shared_kernel import ensure_kernel_on_path

ensure_kernel_on_path()
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


@dataclass(frozen=True, slots=True)
class TraderDeskConfig:
    packet: Path = DEFAULT_PACKET
    mala_repo: Path = DEFAULT_MALA_REPO
    capability_manifest: Path = DEFAULT_CAPABILITY_MANIFEST
    legacy_retirement_report: Path = DEFAULT_LEGACY_REPORT
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT
    update_mala_log: bool = False


class TraderDeskService:
    """Small orchestration surface over Bhiksha packet operations."""

    def __init__(self, config: TraderDeskConfig) -> None:
        self.config = config

    def status(self, *, include_health: bool = False) -> dict[str, Any]:
        compile_payload = self.preflight()
        latest = self.latest_artifacts()
        payload: dict[str, Any] = {
            "desk": "bhiksha_trader_desk",
            "safety_boundary": "no_live_submission_from_ui_v0",
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
                        "approve_live_ticket",
                        "emergency_flatten",
                        "feedback",
                    ],
                    "machine_actions": [
                        "runtime_preflight",
                        "mala_consultation",
                        "option_preview",
                        "risk_gate",
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

    def health(self) -> dict[str, Any]:
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
        result = consult_mala_playbook(
            packet_path=self.config.packet,
            symbol=_required_text(payload, "symbol").upper(),
            direction=_required_text(payload, "direction").lower(),
            timestamp=_required_text(payload, "timestamp"),
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
            chain_service = _SimulatedChainService(symbol, direction) if preview_mode == "simulated" else None
            order_manager = _SimulatedOrderManager() if preview_mode == "simulated" else None
            result = await build_playbook_option_preview(
                intent_artifact=Path(_required_text(payload, "intent_artifact")),
                packet_path=self.config.packet,
                chain_service=chain_service,
                order_manager=order_manager,
                out_root=self.config.artifact_root / "option_previews",
                underlying_price=_optional_float(payload, "underlying_price"),
                underlying_stop_price=_optional_float(payload, "underlying_stop_price"),
            )
            return asdict(result) | {"preview_mode": preview_mode}

        return asyncio.run(_run())

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
        return "approved live ticket exists; broker submit remains outside UI v0"
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
