"""Guided operator desk for the live-gated reversion playbook pilot."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from time import perf_counter
from typing import Any

from bhiksha.domain.models import OptionContractSnapshot
from bhiksha.execution.order_manager import PublicQuote
from bhiksha.execution.order_manager import OrderManager
from bhiksha.packets.consultation_bridge import consult_mala_playbook
from bhiksha.packets.live_ticket import APPROVAL_PHRASE, create_playbook_live_ticket
from bhiksha.packets.operator_decision import record_playbook_operator_decision
from bhiksha.packets.option_preview import build_playbook_option_preview
from bhiksha.packets.runtime_compile import compile_packet_for_runtime, load_legacy_retirement_report
from bhiksha.packets.playbook_lifecycle import submit_playbook_live_ticket
from bhiksha.persistence.sqlite import SQLiteBackend, SQLiteEventRepository, SQLiteTradeStateRepository
from bhiksha.state.lifecycle import TradeLifecycleStore
from bhiksha.shared_kernel import ensure_kernel_on_path

ensure_kernel_on_path()
from mala_bhiksha_kernel import CapabilityManifest  # noqa: E402


DEFAULT_PACKET = Path(
    "/Users/suman/code/mala_v2/packets/execution/"
    "execution.mean_reversion_at_extremes.iwm_qqq/v2.json"
)
DEFAULT_MALA_REPO = Path("/Users/suman/code/mala_v2")
DEFAULT_CAPABILITY_MANIFEST = Path("artifacts/capabilities/bhiksha_packet_capabilities_v1.json")
DEFAULT_LEGACY_REPORT = Path("artifacts/legacy_retirement/current.json")
DEFAULT_DB_PATH = "bhiksha.db"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    _add_common(subparsers.add_parser("preflight", help="Check whether the live packet is eligible."))
    latest = subparsers.add_parser("latest", help="Show latest pilot artifacts and likely next step.")
    latest.add_argument("--artifact-root", type=Path, default=Path("artifacts/playbook"))

    latency = subparsers.add_parser("latency-probe", help="Time the pilot desk legs and write a latency artifact.")
    _add_common(latency)
    latency.add_argument("--artifact-root", type=Path, default=Path("artifacts/playbook"))
    latency.add_argument("--symbol", default="QQQ")
    latency.add_argument("--direction", default="short")
    latency.add_argument("--timestamp", default="2024-07-19 08:45 America/Chicago")
    latency.add_argument(
        "--chart-read",
        default="Latency probe: historical sample event; not a real trade decision.",
    )
    latency.add_argument("--decision", choices=["take", "pass"], default="take")
    latency.add_argument("--management-policy", default="reversal_extreme__fixed_1r")
    latency.add_argument("--underlying-price", type=float, default=475.0)
    latency.add_argument("--underlying-stop-price", type=float, default=478.0)
    latency.add_argument(
        "--option-preview-mode",
        choices=["live", "simulated", "skip"],
        default="live",
        help="Use live provider calls, simulated quote/chain services, or skip the preview leg.",
    )
    latency.add_argument(
        "--update-mala-log",
        action="store_true",
        help="Let Mala update its consultation log during the probe. Default keeps the probe read-only to Mala logs.",
    )

    guided = subparsers.add_parser("guided", help="Run a prompt-driven Monday pilot flow.")
    _add_common(guided)
    guided.add_argument("--artifact-root", type=Path, default=Path("artifacts/playbook"))
    guided.add_argument("--db-path", default=DEFAULT_DB_PATH)
    guided.add_argument(
        "--allow-live-submit",
        action="store_true",
        help="Allow the guide to ask about broker submission. Without this, it stops at the approved ticket.",
    )
    guided.add_argument(
        "--no-update-mala-log",
        action="store_true",
        help="Do not ask Mala to append/update its consultation log.",
    )

    args = parser.parse_args(argv)
    if args.command == "preflight":
        return _preflight(args)
    if args.command == "latest":
        _print_latest(args.artifact_root)
        return 0
    if args.command == "latency-probe":
        return asyncio.run(_latency_probe(args))
    if args.command == "guided":
        return asyncio.run(_guided(args))
    raise AssertionError(f"unhandled command {args.command!r}")


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--packet", type=Path, default=DEFAULT_PACKET)
    parser.add_argument("--mala-repo", type=Path, default=DEFAULT_MALA_REPO)
    parser.add_argument("--capability-manifest", type=Path, default=DEFAULT_CAPABILITY_MANIFEST)
    parser.add_argument("--legacy-retirement-report", type=Path, default=DEFAULT_LEGACY_REPORT)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")


def _preflight(args: argparse.Namespace) -> int:
    result = _compile(args.packet, args.capability_manifest, args.legacy_retirement_report)
    payload = _compile_payload(result)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_section("PREFLIGHT")
        _print_key_values(payload)
        if result.executable:
            print("\nNext: run `guided` when the chart setup appears.")
        else:
            print("\nStop: packet is not eligible. Fix block_reasons before trading.")
    return 0 if result.executable else 2


def _compile(packet: Path, capability_manifest: Path, legacy_report: Path):
    manifest = CapabilityManifest.model_validate_json(capability_manifest.read_text(encoding="utf-8"))
    legacy = load_legacy_retirement_report(legacy_report)
    return compile_packet_for_runtime(
        packet,
        capability_manifest=manifest,
        legacy_retirement_report=legacy,
    )


def _compile_payload(result) -> dict[str, Any]:
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


async def _guided(args: argparse.Namespace) -> int:
    _print_section("PILOT PREFLIGHT")
    compile_result = _compile(args.packet, args.capability_manifest, args.legacy_retirement_report)
    _print_key_values(_compile_payload(compile_result))
    if not compile_result.executable:
        print("\nStop here. The packet is not eligible.")
        return 2

    symbol = _ask("Symbol", "IWM").upper()
    direction = _ask("Direction", "short").lower()
    timestamp = _ask("Timestamp", "2026-05-18 09:40 America/Chicago")
    print("\nChart read first. Write what you see before Mala speaks.")
    chart_read = _ask_required("Chart read")

    consultation = consult_mala_playbook(
        packet_path=args.packet,
        symbol=symbol,
        direction=direction,
        timestamp=timestamp,
        chart_read=chart_read,
        mala_repo=args.mala_repo,
        capability_manifest_path=args.capability_manifest,
        legacy_retirement_report_path=args.legacy_retirement_report,
        out_root=args.artifact_root / "consultations",
        update_mala_log=not args.no_update_mala_log,
    )
    _print_section("CONSULTATION")
    _print_key_values(
        {
            "status": consultation.status,
            "runtime_mode": consultation.runtime_mode,
            "verdict": consultation.verdict,
            "policy": consultation.policy,
            "selected_exit": consultation.selected_exit,
            "allowed_management_policy_ids": consultation.allowed_management_policy_ids,
            "artifact_md": consultation.artifact_md,
        }
    )

    decision = _ask("Decision", "pass").lower()
    selected_policy = ""
    if decision == "take":
        selected_policy = _ask("Management policy", (consultation.allowed_management_policy_ids or [""])[0])
    operator_note = _ask_required("Operator note")
    intent = record_playbook_operator_decision(
        consultation_artifact=Path(consultation.artifact_json),
        decision=decision,
        selected_management_policy_id=selected_policy or None,
        operator_note=operator_note,
        out_root=args.artifact_root / "intents",
    )
    _print_section("OPERATOR DECISION")
    _print_key_values(
        {
            "status": intent.status,
            "execution_ready": intent.execution_ready,
            "execution_mode": intent.execution_mode,
            "selected_management_policy_id": intent.selected_management_policy_id,
            "warning_reasons": intent.warning_reasons,
            "block_reasons": intent.block_reasons,
            "artifact_md": intent.artifact_md,
        }
    )
    if not intent.execution_ready:
        print("\nDone. No executable intent was created.")
        return 0 if intent.status == "operator_pass" else 2

    underlying_price = _ask_float("Underlying entry/reference price")
    underlying_stop = _ask_float("Underlying stop/invalidation price")
    preview = await build_playbook_option_preview(
        intent_artifact=Path(intent.artifact_json),
        packet_path=args.packet,
        out_root=args.artifact_root / "option_previews",
        underlying_price=underlying_price,
        underlying_stop_price=underlying_stop,
    )
    _print_section("OPTION PREVIEW")
    _print_key_values(
        {
            "status": preview.status,
            "option_symbol": preview.option_symbol,
            "quantity": preview.quantity,
            "estimated_entry_price": preview.estimated_entry_price,
            "underlying_stop_price": preview.underlying_stop_price,
            "risk_reasons": preview.risk_reasons,
            "block_reasons": preview.block_reasons,
            "artifact_md": preview.artifact_md,
        }
    )
    if not preview.preview_ready:
        print("\nStop. Preview is blocked.")
        return 2

    approve = _ask("Approve live ticket? yes/no", "no").lower()
    ticket_decision = "approve" if approve in {"y", "yes"} else "reject"
    approval_phrase = None
    if ticket_decision == "approve":
        print(f"Type exact approval phrase to continue: {APPROVAL_PHRASE}")
        approval_phrase = _ask_required("Approval phrase")
    ticket_note = _ask_required("Live ticket note")
    ticket = create_playbook_live_ticket(
        option_preview_artifact=Path(preview.artifact_json),
        decision=ticket_decision,
        operator="Suman",
        operator_note=ticket_note,
        approval_phrase=approval_phrase,
        out_root=args.artifact_root / "live_tickets",
    )
    _print_section("LIVE TICKET")
    _print_key_values(
        {
            "status": ticket.status,
            "order_submission_allowed": ticket.order_submission_allowed,
            "option_symbol": ticket.option_symbol,
            "quantity": ticket.quantity,
            "limit_price": ticket.limit_price,
            "block_reasons": ticket.block_reasons,
            "artifact_md": ticket.artifact_md,
        }
    )
    if not ticket.order_submission_allowed:
        return 0 if ticket.status == "live_ticket_rejected" else 2

    if not args.allow_live_submit:
        print("\nLive ticket is ready. Re-run with --allow-live-submit if you want this guide to submit.")
        return 0
    if _ask("Submit broker entry now? yes/no", "no").lower() not in {"y", "yes"}:
        print("\nStopped before broker submit.")
        return 0

    backend = SQLiteBackend(args.db_path)
    order_manager = OrderManager()
    try:
        lifecycle = await submit_playbook_live_ticket(
            live_ticket_artifact=Path(ticket.artifact_json),
            packet_path=args.packet,
            order_manager=order_manager,
            event_repository=SQLiteEventRepository(args.db_path, backend=backend),
            trade_state_repository=SQLiteTradeStateRepository(args.db_path, backend=backend),
            lifecycle_store=TradeLifecycleStore(),
            out_root=args.artifact_root / "lifecycle",
        )
    finally:
        await order_manager.close()
    _print_section("LIFECYCLE")
    _print_key_values(
        {
            "status": lifecycle.status,
            "lifecycle_started": lifecycle.lifecycle_started,
            "entry_order_id": lifecycle.entry_order_id,
            "stop_order_id": lifecycle.stop_order_id,
            "emergency_exit_order_id": lifecycle.emergency_exit_order_id,
            "trade_state": lifecycle.trade_state,
            "block_reasons": lifecycle.block_reasons,
            "artifact_md": lifecycle.artifact_md,
        }
    )
    if lifecycle.lifecycle_started:
        print("\nNext: run live management monitor in dry mode first, then with --execute if correct.")
        return 0
    return 2


async def _latency_probe(args: argparse.Namespace) -> int:
    """Time a consultation-to-preview pass without submitting any orders."""
    probe_root = args.artifact_root / "latency"
    probe_id = _now_id()
    probe_dir = probe_root / probe_id
    probe_dir.mkdir(parents=True, exist_ok=True)

    legs: list[dict[str, Any]] = []
    artifacts: dict[str, str] = {}

    compile_result, compile_leg = _time_sync(
        "preflight_compile",
        lambda: _compile(args.packet, args.capability_manifest, args.legacy_retirement_report),
    )
    legs.append(compile_leg | _compile_payload(compile_result))
    if not compile_result.executable:
        payload = _latency_payload(args, probe_id, legs, artifacts, status="blocked_preflight")
        payload = _write_latency_artifacts(probe_dir, payload)
        _print_latency(payload)
        return 2

    consultation, consultation_leg = _time_sync(
        "mala_consultation",
        lambda: consult_mala_playbook(
            packet_path=args.packet,
            symbol=args.symbol.upper(),
            direction=args.direction.lower(),
            timestamp=args.timestamp,
            chart_read=args.chart_read,
            mala_repo=args.mala_repo,
            capability_manifest_path=args.capability_manifest,
            legacy_retirement_report_path=args.legacy_retirement_report,
            out_root=args.artifact_root / "consultations",
            update_mala_log=args.update_mala_log,
        ),
    )
    legs.append(
        consultation_leg
        | {
            "status": consultation.status,
            "verdict": consultation.verdict,
            "policy": consultation.policy,
        }
    )
    artifacts["consultation_json"] = consultation.artifact_json
    artifacts["consultation_md"] = consultation.artifact_md

    selected_policy = args.management_policy if args.decision == "take" else None
    intent, decision_leg = _time_sync(
        "operator_decision_artifact",
        lambda: record_playbook_operator_decision(
            consultation_artifact=Path(consultation.artifact_json),
            decision=args.decision,
            selected_management_policy_id=selected_policy,
            operator_note="Latency probe artifact; not a real trade decision.",
            out_root=args.artifact_root / "intents",
        ),
    )
    legs.append(
        decision_leg
        | {
            "status": intent.status,
            "execution_ready": intent.execution_ready,
            "warning_reasons": intent.warning_reasons,
            "block_reasons": intent.block_reasons,
        }
    )
    artifacts["intent_json"] = intent.artifact_json
    artifacts["intent_md"] = intent.artifact_md

    if args.option_preview_mode == "skip" or not intent.execution_ready:
        preview_status = "skipped"
    else:
        chain_service = (
            _SimulatedChainService(args.symbol.upper(), args.direction.lower())
            if args.option_preview_mode == "simulated"
            else None
        )
        order_manager = _SimulatedOrderManager() if args.option_preview_mode == "simulated" else None
        preview_start = perf_counter()
        try:
            preview = await build_playbook_option_preview(
                intent_artifact=Path(intent.artifact_json),
                packet_path=args.packet,
                chain_service=chain_service,
                order_manager=order_manager,
                out_root=args.artifact_root / "option_previews",
                underlying_price=args.underlying_price,
                underlying_stop_price=args.underlying_stop_price,
            )
            preview_leg = {
                "leg": "option_preview",
                "ok": True,
                "elapsed_seconds": round(perf_counter() - preview_start, 3),
            }
            legs.append(
                preview_leg
                | {
                    "mode": args.option_preview_mode,
                    "status": preview.status,
                    "preview_ready": preview.preview_ready,
                    "option_symbol": preview.option_symbol,
                    "quantity": preview.quantity,
                    "block_reasons": preview.block_reasons,
                    "risk_reasons": preview.risk_reasons,
                }
            )
            artifacts["option_preview_json"] = preview.artifact_json
            artifacts["option_preview_md"] = preview.artifact_md
            preview_status = preview.status
        except Exception as exc:  # Provider/auth failures should become readiness evidence.
            legs.append(
                {
                    "leg": "option_preview",
                    "mode": args.option_preview_mode,
                    "ok": False,
                    "elapsed_seconds": round(perf_counter() - preview_start, 3),
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            preview_status = "failed"
        finally:
            if chain_service is not None:
                await chain_service.close()
            if order_manager is not None:
                await order_manager.close()

    status = "complete"
    if preview_status == "failed":
        status = "provider_or_auth_blocked"
    elif preview_status == "skipped":
        status = "preview_skipped"
    payload = _latency_payload(args, probe_id, legs, artifacts, status=status)
    payload = _write_latency_artifacts(probe_dir, payload)
    _print_latency(payload)
    return 0 if status in {"complete", "preview_skipped"} else 2


def _print_latest(root: Path) -> None:
    _print_section("LATEST PILOT ARTIFACTS")
    latest = {
        "consultation": _latest(root / "consultations", "consultation_bridge.json"),
        "intent": _latest(root / "intents", "playbook_operator_decision.json"),
        "option_preview": _latest(root / "option_previews", "playbook_option_preview.json"),
        "live_ticket": _latest(root / "live_tickets", "playbook_live_ticket.json"),
        "lifecycle": _latest(root / "lifecycle", "playbook_lifecycle_submission.json"),
        "live_management": _latest(root / "live_management", "playbook_live_management.json"),
    }
    _print_key_values({key: _latest_label(value) for key, value in latest.items()})
    print(f"\nSuggested next step: {_suggest_next_step(latest)}")


def _latest(root: Path, filename: str) -> Path | None:
    if not root.exists():
        return None
    matches = list(root.glob(f"*/{filename}"))
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def _suggest_next_step(latest: dict[str, Path | None]) -> str:
    management_status = _artifact_status(latest["live_management"])
    lifecycle_status = _artifact_status(latest["lifecycle"])
    ticket_status = _artifact_status(latest["live_ticket"])
    preview_status = _artifact_status(latest["option_preview"])
    intent_status = _artifact_status(latest["intent"])

    if management_status:
        return "review live management artifact"
    if lifecycle_status == "lifecycle_started":
        return "run live management monitor dry-run"
    if lifecycle_status in {"pending_entry_reconcile", "protection_failed_exit_pending", "critical_unprotected"}:
        return f"resolve lifecycle risk state: {lifecycle_status}"
    if ticket_status == "live_ticket_approved":
        return "submit approved live ticket only if you intend to enter"
    if ticket_status in {"live_ticket_rejected", "blocked"}:
        return "run guided consultation when a fresh setup appears"
    if preview_status == "option_preview_ready":
        return "approve/reject live ticket"
    if preview_status == "blocked":
        return "fix blocked option preview or pass the trade"
    if intent_status in {"live_intent_ready", "shadow_intent_ready"}:
        return "build option preview with underlying stop"
    if intent_status in {"operator_pass", "blocked"}:
        return "run guided consultation when a fresh setup appears"
    if latest["consultation"]:
        return "record take/pass decision"
    return "run guided consultation"


def _latest_label(path: Path | None) -> str:
    if path is None:
        return ""
    status = _artifact_status(path)
    suffix = f" [{status}]" if status else ""
    return f"{path}{suffix}"


def _artifact_status(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(payload.get("status", ""))


def _time_sync(leg: str, fn):
    start = perf_counter()
    result = fn()
    elapsed = perf_counter() - start
    return result, {"leg": leg, "ok": True, "elapsed_seconds": round(elapsed, 3)}


def _latency_payload(
    args: argparse.Namespace,
    probe_id: str,
    legs: list[dict[str, Any]],
    artifacts: dict[str, str],
    *,
    status: str,
) -> dict[str, Any]:
    total = round(sum(float(leg.get("elapsed_seconds", 0.0) or 0.0) for leg in legs), 3)
    return {
        "status": status,
        "probe_id": probe_id,
        "symbol": args.symbol.upper(),
        "direction": args.direction.lower(),
        "timestamp": args.timestamp,
        "decision": args.decision,
        "option_preview_mode": args.option_preview_mode,
        "total_measured_seconds": total,
        "legs": legs,
        "artifacts": artifacts,
        "operator_note": "No order submission is possible from this probe.",
    }


def _write_latency_artifacts(probe_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    json_path = probe_dir / "playbook_latency_probe.json"
    md_path = probe_dir / "PLAYBOOK_LATENCY_PROBE.md"
    payload = payload | {
        "artifact_json": str(json_path),
        "artifact_md": str(md_path),
    }
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(_latency_markdown(payload), encoding="utf-8")
    return payload


def _latency_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Playbook Latency Probe",
        "",
        f"- status: `{payload['status']}`",
        f"- symbol: `{payload['symbol']}`",
        f"- direction: `{payload['direction']}`",
        f"- timestamp: `{payload['timestamp']}`",
        f"- option_preview_mode: `{payload['option_preview_mode']}`",
        f"- total_measured_seconds: `{payload['total_measured_seconds']}`",
        "",
        "## Legs",
        "",
    ]
    for leg in payload["legs"]:
        detail = leg.get("status") or leg.get("eligibility") or ""
        lines.append(f"- `{leg['leg']}`: `{leg.get('elapsed_seconds', 0.0)}` sec `{detail}`")
        if leg.get("error"):
            lines.append(f"  - error: `{leg['error']}`")
    lines.extend(["", "## Artifacts", ""])
    for key, value in payload["artifacts"].items():
        lines.append(f"- {key}: `{value}`")
    lines.append("")
    return "\n".join(lines)


def _print_latency(payload: dict[str, Any]) -> None:
    _print_section("LATENCY PROBE")
    _print_key_values(
        {
            "status": payload["status"],
            "total_measured_seconds": payload["total_measured_seconds"],
            "option_preview_mode": payload["option_preview_mode"],
            "artifact_md": payload.get("artifact_md", ""),
        }
    )
    print("\nLegs:")
    for leg in payload["legs"]:
        status = leg.get("status") or leg.get("eligibility") or ""
        error = f" error={leg['error']}" if leg.get("error") else ""
        print(f"- {leg['leg']}: {leg.get('elapsed_seconds', 0.0)}s {status}{error}")


def _now_id() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


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


def _ask(label: str, default: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def _ask_required(label: str) -> str:
    while True:
        value = input(f"{label}: ").strip()
        if value:
            return value
        print("Required.")


def _ask_float(label: str) -> float:
    while True:
        raw = input(f"{label}: ").strip()
        try:
            return float(raw)
        except ValueError:
            print("Enter a number.")


def _print_section(title: str) -> None:
    print(f"\n== {title} ==")


def _print_key_values(payload: dict[str, Any]) -> None:
    for key, value in payload.items():
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value)
        print(f"{key}: {value}")


if __name__ == "__main__":
    raise SystemExit(main())
