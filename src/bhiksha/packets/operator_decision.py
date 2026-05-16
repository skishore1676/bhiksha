"""Record operator decisions from Bhiksha playbook consultations."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal


OperatorDecision = Literal["take", "pass"]


@dataclass(frozen=True, slots=True)
class PlaybookOperatorDecisionResult:
    status: str
    decision: OperatorDecision
    execution_ready: bool
    execution_mode: str
    packet_id: str
    packet_version: int
    symbol: str
    direction: str
    timestamp: str
    consultation_artifact: str
    mala_verdict: str
    mala_policy: str
    selected_management_policy_id: str
    allowed_management_policy_ids: list[str]
    operator_note: str
    warning_reasons: list[str]
    block_reasons: list[str]
    order_submission_allowed: bool
    artifact_json: str
    artifact_md: str


def record_playbook_operator_decision(
    *,
    consultation_artifact: Path,
    decision: str,
    operator_note: str,
    selected_management_policy_id: str | None = None,
    out_root: Path = Path("artifacts/playbook/intents"),
) -> PlaybookOperatorDecisionResult:
    """Turn a consultation artifact into an operator take/pass intent."""
    consultation = _load_consultation(consultation_artifact)
    normalized_decision = _normalize_decision(decision)
    note = operator_note.strip()
    if not note:
        raise ValueError("operator_note is required for playbook operator decisions")

    block_reasons = _consultation_blocks(consultation)
    warning_reasons: list[str] = []
    allowed_policy_ids = [
        str(policy_id)
        for policy_id in consultation.get("allowed_management_policy_ids", [])
        if str(policy_id)
    ]
    selected_policy = (selected_management_policy_id or "").strip()
    execution_ready = False
    status = "operator_pass"

    execution_mode = str(consultation.get("runtime_mode", "shadow"))

    if normalized_decision == "take":
        status = "live_intent_ready" if execution_mode == "live_approval_gated" else "shadow_intent_ready"
        if not selected_policy:
            block_reasons.append("management_policy_required_for_take")
        elif selected_policy not in allowed_policy_ids:
            block_reasons.append(f"management_policy_not_allowed:{selected_policy}")
        if str(consultation.get("policy", "")).lower() != "take":
            warning_reasons.append("operator_overrode_mala_policy")
        execution_ready = not block_reasons
    elif selected_policy and selected_policy not in allowed_policy_ids:
        block_reasons.append(f"management_policy_not_allowed:{selected_policy}")

    if block_reasons:
        status = "blocked"
        execution_ready = False

    artifact_dir = out_root / _intent_id(consultation, normalized_decision)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    result = PlaybookOperatorDecisionResult(
        status=status,
        decision=normalized_decision,
        execution_ready=execution_ready,
        execution_mode=execution_mode,
        packet_id=str(consultation.get("packet_id", "")),
        packet_version=int(consultation.get("packet_version", 0) or 0),
        symbol=str(consultation.get("symbol", "")),
        direction=str(consultation.get("direction", "")),
        timestamp=str(consultation.get("timestamp", "")),
        consultation_artifact=str(consultation_artifact),
        mala_verdict=str(consultation.get("verdict", "")),
        mala_policy=str(consultation.get("policy", "")),
        selected_management_policy_id=selected_policy,
        allowed_management_policy_ids=allowed_policy_ids,
        operator_note=note,
        warning_reasons=warning_reasons,
        block_reasons=block_reasons,
        order_submission_allowed=False,
        artifact_json=str(artifact_dir / "playbook_operator_decision.json"),
        artifact_md=str(artifact_dir / "PLAYBOOK_OPERATOR_DECISION.md"),
    )
    _write_decision_artifacts(result)
    return result


def _load_consultation(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("consultation artifact must be a JSON object")
    return payload


def _consultation_blocks(consultation: dict[str, Any]) -> list[str]:
    blocks: list[str] = []
    if consultation.get("status") != "consulted":
        blocks.append(f"consultation_status_not_consulted:{consultation.get('status')}")
    compile_eligibility = consultation.get("compile_eligibility")
    if compile_eligibility is not None:
        if compile_eligibility != "eligible":
            blocks.append(f"packet_compile_not_eligible:{compile_eligibility}")
    elif consultation.get("compile_decision") != "take":
        blocks.append(f"packet_compile_not_take:{consultation.get('compile_decision')}")
    if consultation.get("runtime_mode") not in {"shadow", "live_approval_gated"}:
        blocks.append(f"runtime_mode_not_supported:{consultation.get('runtime_mode')}")
    compile_blocks = consultation.get("compile_block_reasons", [])
    if compile_blocks:
        blocks.extend(f"compile_block:{reason}" for reason in compile_blocks)
    return blocks


def _normalize_decision(decision: str) -> OperatorDecision:
    normalized = decision.strip().lower()
    if normalized not in {"take", "pass"}:
        raise ValueError("decision must be take or pass")
    return normalized  # type: ignore[return-value]


def _intent_id(consultation: dict[str, Any], decision: OperatorDecision) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    packet_id = str(consultation.get("packet_id", "unknown_packet"))
    symbol = str(consultation.get("symbol", "UNK"))
    direction = str(consultation.get("direction", "unknown"))
    timestamp = re.sub(r"[^A-Za-z0-9]+", "_", str(consultation.get("timestamp", ""))).strip("_")[:48]
    return f"{stamp}_{packet_id}_{symbol}_{direction}_{decision}_{timestamp}"


def _write_decision_artifacts(result: PlaybookOperatorDecisionResult) -> None:
    payload = asdict(result) | {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "safety_boundary": "shadow_intent_only_no_order_submission",
    }
    Path(result.artifact_json).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    Path(result.artifact_md).write_text(_decision_markdown(payload), encoding="utf-8")


def _decision_markdown(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Playbook Operator Decision",
            "",
            f"- status: `{payload['status']}`",
            f"- decision: `{payload['decision']}`",
            f"- execution_ready: `{payload['execution_ready']}`",
            f"- execution_mode: `{payload['execution_mode']}`",
            f"- order_submission_allowed: `{payload['order_submission_allowed']}`",
            f"- packet: `{payload['packet_id']}` v`{payload['packet_version']}`",
            f"- symbol: `{payload['symbol']}`",
            f"- direction: `{payload['direction']}`",
            f"- timestamp: `{payload['timestamp']}`",
            f"- mala_verdict: `{payload['mala_verdict']}`",
            f"- mala_policy: `{payload['mala_policy']}`",
            f"- selected_management_policy_id: `{payload['selected_management_policy_id']}`",
            f"- warning_reasons: `{', '.join(payload['warning_reasons'])}`",
            f"- block_reasons: `{', '.join(payload['block_reasons'])}`",
            "",
            "## Operator Note",
            "",
            str(payload["operator_note"]),
            "",
        ]
    )
