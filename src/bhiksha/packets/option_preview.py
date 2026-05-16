"""Build option previews from playbook operator intents."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bhiksha.config.models import DeploymentManifest
from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import SignalDecision, TradePlan
from bhiksha.execution.order_manager import OrderManager
from bhiksha.execution.planner import ExecutionPlanner
from bhiksha.options.chain_service import OptionChainService
from bhiksha.shared_kernel import ensure_kernel_on_path
from bhiksha.state.position_tracker import PositionTracker

ensure_kernel_on_path()
from mala_bhiksha_kernel import ExecutionPacket, read_packet_file  # noqa: E402


@dataclass(frozen=True, slots=True)
class PlaybookOptionPreviewResult:
    status: str
    preview_ready: bool
    packet_id: str
    packet_version: int
    symbol: str
    direction: str
    timestamp: str
    selected_management_policy_id: str
    intent_artifact: str
    option_symbol: str
    quantity: int
    estimated_entry_price: float
    underlying_entry_price: float | None
    risk_reasons: list[str]
    block_reasons: list[str]
    order_submission_allowed: bool
    live_approval_required: bool
    artifact_json: str
    artifact_md: str


async def build_playbook_option_preview(
    *,
    intent_artifact: Path,
    packet_path: Path,
    chain_service: OptionChainService | None = None,
    order_manager: OrderManager | None = None,
    out_root: Path = Path("artifacts/playbook/option_previews"),
    max_trade_premium_usd: float = 300.0,
    dte_min: int = 0,
    dte_max: int = 7,
    target_abs_delta_min: float = 0.20,
    target_abs_delta_max: float = 0.40,
    min_open_interest: int = 100,
    max_bid_ask_spread_pct: float = 0.20,
    underlying_price: float | None = None,
) -> PlaybookOptionPreviewResult:
    """Resolve an option candidate and write an approval-gated preview artifact."""
    intent = _load_json(intent_artifact)
    packet = read_packet_file(packet_path)
    if not isinstance(packet, ExecutionPacket):
        raise ValueError(f"expected execution packet, found {packet.kind.value}")

    block_reasons = _intent_blocks(intent)
    block_reasons.extend(_packet_blocks(intent, packet))

    plan: TradePlan | None = None
    if not block_reasons:
        deployment = _preview_deployment(
            intent,
            max_trade_premium_usd=max_trade_premium_usd,
            dte_min=dte_min,
            dte_max=dte_max,
            target_abs_delta_min=target_abs_delta_min,
            target_abs_delta_max=target_abs_delta_max,
            min_open_interest=min_open_interest,
            max_bid_ask_spread_pct=max_bid_ask_spread_pct,
        )
        decision = SignalDecision(
            deployment_id=deployment.deployment_id,
            symbol=deployment.symbol,
            timestamp=_parse_operator_timestamp(str(intent["timestamp"])),
            signal=True,
            direction=SignalDirection(str(intent["direction"])),
            reason=["playbook_operator_shadow_intent"],
            features={"close": underlying_price} if underlying_price is not None else {},
        )
        planner = ExecutionPlanner(
            chain_service=chain_service,
            order_manager=order_manager,
            position_tracker=PositionTracker(),
        )
        plan = await planner.plan_entry(deployment, decision, dry_run=True, simulate_only=True)
        if plan is None:
            block_reasons.append("planner_returned_no_trade_plan")
        elif plan.quantity <= 0:
            block_reasons.extend(plan.risk_reasons or ["option_preview_quantity_zero"])
        elif _has_blocking_risk(plan.risk_reasons):
            block_reasons.extend(plan.risk_reasons)

    status = "option_preview_ready" if not block_reasons else "blocked"
    artifact_dir = out_root / _preview_id(intent)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    result = PlaybookOptionPreviewResult(
        status=status,
        preview_ready=status == "option_preview_ready",
        packet_id=str(intent.get("packet_id", "")),
        packet_version=int(intent.get("packet_version", 0) or 0),
        symbol=str(intent.get("symbol", "")),
        direction=str(intent.get("direction", "")),
        timestamp=str(intent.get("timestamp", "")),
        selected_management_policy_id=str(intent.get("selected_management_policy_id", "")),
        intent_artifact=str(intent_artifact),
        option_symbol=plan.option_symbol if plan is not None else "",
        quantity=plan.quantity if plan is not None else 0,
        estimated_entry_price=plan.estimated_entry_price if plan is not None else 0.0,
        underlying_entry_price=plan.underlying_entry_price if plan is not None else underlying_price,
        risk_reasons=plan.risk_reasons if plan is not None else [],
        block_reasons=block_reasons,
        order_submission_allowed=False,
        live_approval_required=True,
        artifact_json=str(artifact_dir / "playbook_option_preview.json"),
        artifact_md=str(artifact_dir / "PLAYBOOK_OPTION_PREVIEW.md"),
    )
    _write_preview_artifacts(result)
    return result


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object at {path}")
    return payload


def _intent_blocks(intent: dict[str, Any]) -> list[str]:
    blocks: list[str] = []
    if intent.get("status") != "shadow_intent_ready":
        blocks.append(f"intent_status_not_shadow_ready:{intent.get('status')}")
    if intent.get("decision") != "take":
        blocks.append(f"intent_decision_not_take:{intent.get('decision')}")
    if intent.get("execution_ready") is not True:
        blocks.append("intent_execution_not_ready")
    if intent.get("order_submission_allowed") is not False:
        blocks.append("intent_order_submission_boundary_missing")
    if not str(intent.get("selected_management_policy_id", "")).strip():
        blocks.append("selected_management_policy_missing")
    return blocks


def _packet_blocks(intent: dict[str, Any], packet: ExecutionPacket) -> list[str]:
    blocks: list[str] = []
    if packet.packet_id != intent.get("packet_id") or packet.version != intent.get("packet_version"):
        blocks.append("intent_packet_mismatch")
    controls = packet.runtime_controls
    if controls.get("option_selection_preview_only") is not True:
        blocks.append("packet_option_preview_only_missing")
    if controls.get("live_automated_allowed") is not False:
        blocks.append("packet_live_automated_boundary_missing")
    if controls.get("shadow_only") is not True:
        blocks.append("packet_shadow_only_missing")
    selected_policy = str(intent.get("selected_management_policy_id", ""))
    allowed_policy_ids = [str(policy_id) for policy_id in controls.get("allowed_management_policy_ids", [])]
    if selected_policy not in allowed_policy_ids:
        blocks.append(f"selected_management_policy_not_allowed:{selected_policy}")
    return blocks


def _preview_deployment(
    intent: dict[str, Any],
    *,
    max_trade_premium_usd: float,
    dte_min: int,
    dte_max: int,
    target_abs_delta_min: float,
    target_abs_delta_max: float,
    min_open_interest: int,
    max_bid_ask_spread_pct: float,
) -> DeploymentManifest:
    return DeploymentManifest.model_validate(
        {
            "deployment_id": f"playbook_preview_{_slug(str(intent['packet_id']))}_{intent['symbol']}_{intent['direction']}",
            "enabled": True,
            "symbol": intent["symbol"],
            "strategy": {
                "key": "playbook_mean_reversion",
                "version": 1,
                "params": {
                    "packet_id": intent["packet_id"],
                    "packet_version": intent["packet_version"],
                    "selected_management_policy_id": intent["selected_management_policy_id"],
                },
            },
            "execution": {
                "profile": "single_leg_long_premium_v1",
                "option_mapping": {"long_signal": "CALL", "short_signal": "PUT"},
                "dte_min": dte_min,
                "dte_max": dte_max,
                "target_abs_delta_min": target_abs_delta_min,
                "target_abs_delta_max": target_abs_delta_max,
                "min_open_interest": min_open_interest,
                "max_bid_ask_spread_pct": max_bid_ask_spread_pct,
                "shadow_only": True,
            },
            "risk": {
                "profile": "conservative_day1",
                "max_trade_premium_usd": max_trade_premium_usd,
                "hard_flat_time_et": "15:55",
                "stop_loss_pct": 0.45,
            },
            "exit": {
                "profile": str(intent["selected_management_policy_id"]),
                "use_algorithmic_exit": False,
                "use_profit_target": False,
                "stop_loss_pct": 0.45,
                "hard_flat_time_et": "15:55",
                "thesis_exit_policy": str(intent["selected_management_policy_id"]),
            },
            "source": {
                "origin": "playbook_operator_intent",
                "artifact": intent.get("artifact_json"),
                "metadata": {
                    "intent_artifact": intent.get("artifact_json"),
                    "consultation_artifact": intent.get("consultation_artifact"),
                },
            },
        }
    )


def _parse_operator_timestamp(value: str) -> datetime:
    stripped = value.strip()
    for zone_name in ("America/Chicago", "America/New_York", "UTC"):
        suffix = f" {zone_name}"
        if stripped.endswith(suffix):
            raw = stripped[: -len(suffix)]
            return datetime.fromisoformat(raw).replace(tzinfo=ZoneInfo(zone_name))
    normalized = stripped.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _has_blocking_risk(reasons: list[str]) -> bool:
    return any(reason != "approved" for reason in reasons)


def _preview_id(intent: dict[str, Any]) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    timestamp = re.sub(r"[^A-Za-z0-9]+", "_", str(intent.get("timestamp", ""))).strip("_")[:48]
    return (
        f"{stamp}_{intent.get('packet_id', 'unknown_packet')}_"
        f"{intent.get('symbol', 'UNK')}_{intent.get('direction', 'unknown')}_{timestamp}"
    )


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()


def _write_preview_artifacts(result: PlaybookOptionPreviewResult) -> None:
    payload = asdict(result) | {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "safety_boundary": "option_preview_only_no_order_submission",
    }
    Path(result.artifact_json).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    Path(result.artifact_md).write_text(_preview_markdown(payload), encoding="utf-8")


def _preview_markdown(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Playbook Option Preview",
            "",
            f"- status: `{payload['status']}`",
            f"- preview_ready: `{payload['preview_ready']}`",
            f"- order_submission_allowed: `{payload['order_submission_allowed']}`",
            f"- live_approval_required: `{payload['live_approval_required']}`",
            f"- packet: `{payload['packet_id']}` v`{payload['packet_version']}`",
            f"- symbol: `{payload['symbol']}`",
            f"- direction: `{payload['direction']}`",
            f"- timestamp: `{payload['timestamp']}`",
            f"- selected_management_policy_id: `{payload['selected_management_policy_id']}`",
            f"- option_symbol: `{payload['option_symbol']}`",
            f"- quantity: `{payload['quantity']}`",
            f"- estimated_entry_price: `{payload['estimated_entry_price']}`",
            f"- risk_reasons: `{', '.join(payload['risk_reasons'])}`",
            f"- block_reasons: `{', '.join(payload['block_reasons'])}`",
            "",
        ]
    )
