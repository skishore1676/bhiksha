"""Create approval-gated live tickets from playbook option previews."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal


LiveDecision = Literal["approve", "reject"]
APPROVAL_PHRASE = "APPROVE_LIVE_PLAYBOOK_TICKET"


@dataclass(frozen=True, slots=True)
class PlaybookLiveTicketResult:
    status: str
    decision: LiveDecision
    packet_id: str
    packet_version: int
    symbol: str
    direction: str
    timestamp: str
    selected_management_policy_id: str
    management_spec: dict[str, Any]
    option_preview_artifact: str
    option_symbol: str
    quantity: int
    limit_price: float
    operator: str
    operator_note: str
    order_submission_allowed: bool
    live_approval_required: bool
    block_reasons: list[str]
    artifact_json: str
    artifact_md: str


def create_playbook_live_ticket(
    *,
    option_preview_artifact: Path,
    decision: str,
    operator: str,
    operator_note: str,
    approval_phrase: str | None = None,
    out_root: Path = Path("artifacts/playbook/live_tickets"),
) -> PlaybookLiveTicketResult:
    """Approve or reject a preview for the later live-submit lane."""
    preview = _load_json(option_preview_artifact)
    normalized_decision = _normalize_decision(decision)
    actor = operator.strip()
    note = operator_note.strip()
    if not actor:
        raise ValueError("operator is required for live ticket decisions")
    if not note:
        raise ValueError("operator_note is required for live ticket decisions")

    block_reasons = _preview_blocks(preview)
    order_allowed = False
    if normalized_decision == "approve":
        if approval_phrase != APPROVAL_PHRASE:
            block_reasons.append("live_approval_phrase_missing")
        order_allowed = not block_reasons

    status = "live_ticket_approved" if order_allowed else "live_ticket_rejected"
    if block_reasons:
        status = "blocked"

    artifact_dir = out_root / _ticket_id(preview, normalized_decision)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    result = PlaybookLiveTicketResult(
        status=status,
        decision=normalized_decision,
        packet_id=str(preview.get("packet_id", "")),
        packet_version=int(preview.get("packet_version", 0) or 0),
        symbol=str(preview.get("symbol", "")),
        direction=str(preview.get("direction", "")),
        timestamp=str(preview.get("timestamp", "")),
        selected_management_policy_id=str(preview.get("selected_management_policy_id", "")),
        management_spec=dict(preview.get("management_spec", {}) or {}),
        option_preview_artifact=str(option_preview_artifact),
        option_symbol=str(preview.get("option_symbol", "")),
        quantity=int(preview.get("quantity", 0) or 0),
        limit_price=float(preview.get("estimated_entry_price", 0.0) or 0.0),
        operator=actor,
        operator_note=note,
        order_submission_allowed=order_allowed,
        live_approval_required=not order_allowed,
        block_reasons=block_reasons,
        artifact_json=str(artifact_dir / "playbook_live_ticket.json"),
        artifact_md=str(artifact_dir / "PLAYBOOK_LIVE_TICKET.md"),
    )
    _write_live_ticket_artifacts(result)
    return result


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object at {path}")
    return payload


def _normalize_decision(decision: str) -> LiveDecision:
    normalized = decision.strip().lower()
    if normalized not in {"approve", "reject"}:
        raise ValueError("decision must be approve or reject")
    return normalized  # type: ignore[return-value]


def _preview_blocks(preview: dict[str, Any]) -> list[str]:
    blocks: list[str] = []
    if preview.get("status") != "option_preview_ready":
        blocks.append(f"preview_status_not_ready:{preview.get('status')}")
    if preview.get("preview_ready") is not True:
        blocks.append("preview_not_ready")
    if preview.get("live_approval_required") is not True:
        blocks.append("preview_live_approval_boundary_missing")
    if preview.get("order_submission_allowed") is not False:
        blocks.append("preview_order_submission_boundary_missing")
    if not str(preview.get("option_symbol", "")).strip():
        blocks.append("option_symbol_missing")
    if int(preview.get("quantity", 0) or 0) <= 0:
        blocks.append("quantity_missing")
    if float(preview.get("estimated_entry_price", 0.0) or 0.0) <= 0:
        blocks.append("limit_price_missing")
    return blocks


def _ticket_id(preview: dict[str, Any], decision: LiveDecision) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    raw = f"{preview.get('packet_id', 'unknown_packet')}_{preview.get('symbol', 'UNK')}_{preview.get('direction', 'unknown')}_{decision}"
    slug = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_")[:96]
    return f"{stamp}_{slug}"


def _write_live_ticket_artifacts(result: PlaybookLiveTicketResult) -> None:
    payload = asdict(result) | {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "lane": "live",
        "submitter_status": "not_submitted",
    }
    Path(result.artifact_json).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    Path(result.artifact_md).write_text(_ticket_markdown(payload), encoding="utf-8")


def _ticket_markdown(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Playbook Live Ticket",
            "",
            f"- status: `{payload['status']}`",
            f"- lane: `{payload['lane']}`",
            f"- order_submission_allowed: `{payload['order_submission_allowed']}`",
            f"- submitter_status: `{payload['submitter_status']}`",
            f"- packet: `{payload['packet_id']}` v`{payload['packet_version']}`",
            f"- symbol: `{payload['symbol']}`",
            f"- direction: `{payload['direction']}`",
            f"- management_policy_id: `{payload['selected_management_policy_id']}`",
            f"- management_stop_anchor: `{payload['management_spec'].get('stop_anchor', '')}`",
            f"- option_symbol: `{payload['option_symbol']}`",
            f"- quantity: `{payload['quantity']}`",
            f"- limit_price: `{payload['limit_price']}`",
            f"- operator: `{payload['operator']}`",
            f"- block_reasons: `{', '.join(payload['block_reasons'])}`",
            "",
            "## Operator Note",
            "",
            str(payload["operator_note"]),
            "",
        ]
    )
