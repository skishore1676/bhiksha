"""Record shadow option outcomes for playbook option previews."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bhiksha.execution.order_manager import OrderManager


@dataclass(frozen=True, slots=True)
class PlaybookShadowOutcomeResult:
    status: str
    packet_id: str
    packet_version: int
    symbol: str
    direction: str
    timestamp: str
    selected_management_policy_id: str
    option_preview_artifact: str
    option_symbol: str
    quantity: int
    entry_price: float
    exit_price: float
    exit_timestamp: str
    exit_reason: str
    gross_pnl_usd: float
    pnl_pct: float
    pnl_r: float
    block_reasons: list[str]
    artifact_json: str
    artifact_md: str


async def record_playbook_shadow_outcome(
    *,
    option_preview_artifact: Path,
    exit_timestamp: str,
    exit_reason: str = "end_of_day_mark",
    exit_price: float | None = None,
    order_manager: OrderManager | None = None,
    out_root: Path = Path("artifacts/playbook/shadow_outcomes"),
) -> PlaybookShadowOutcomeResult:
    """Record simulated option PnL for a previewed playbook trade."""
    preview = _load_json(option_preview_artifact)
    block_reasons = _preview_blocks(preview)
    resolved_exit_price = exit_price
    option_symbol = str(preview.get("option_symbol", ""))
    if not block_reasons and resolved_exit_price is None:
        if order_manager is None:
            block_reasons.append("exit_price_or_quote_required")
        else:
            quote = await order_manager.get_option_quote(option_symbol)
            resolved_exit_price = quote.exit_reference_price
            if resolved_exit_price is None:
                block_reasons.append("exit_quote_missing_price")

    entry_price = _safe_float(preview.get("estimated_entry_price"))
    quantity = _safe_int(preview.get("quantity"))
    if entry_price <= 0:
        block_reasons.append("entry_price_missing")
    if quantity <= 0:
        block_reasons.append("quantity_missing")
    if resolved_exit_price is None or resolved_exit_price < 0:
        block_reasons.append("exit_price_missing")

    gross_pnl = 0.0
    pnl_pct = 0.0
    pnl_r = 0.0
    if not block_reasons:
        gross_pnl = round((float(resolved_exit_price) - entry_price) * quantity * 100.0, 2)
        entry_debit = entry_price * quantity * 100.0
        pnl_pct = round((float(resolved_exit_price) - entry_price) / entry_price, 6)
        pnl_r = round(gross_pnl / entry_debit, 6) if entry_debit > 0 else 0.0

    artifact_dir = out_root / _outcome_id(preview, exit_timestamp)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    result = PlaybookShadowOutcomeResult(
        status="shadow_outcome_recorded" if not block_reasons else "blocked",
        packet_id=str(preview.get("packet_id", "")),
        packet_version=int(preview.get("packet_version", 0) or 0),
        symbol=str(preview.get("symbol", "")),
        direction=str(preview.get("direction", "")),
        timestamp=str(preview.get("timestamp", "")),
        selected_management_policy_id=str(preview.get("selected_management_policy_id", "")),
        option_preview_artifact=str(option_preview_artifact),
        option_symbol=option_symbol,
        quantity=quantity,
        entry_price=entry_price,
        exit_price=float(resolved_exit_price or 0.0),
        exit_timestamp=exit_timestamp,
        exit_reason=exit_reason,
        gross_pnl_usd=gross_pnl,
        pnl_pct=pnl_pct,
        pnl_r=pnl_r,
        block_reasons=block_reasons,
        artifact_json=str(artifact_dir / "playbook_shadow_outcome.json"),
        artifact_md=str(artifact_dir / "PLAYBOOK_SHADOW_OUTCOME.md"),
    )
    _write_outcome_artifacts(result)
    return result


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object at {path}")
    return payload


def _preview_blocks(preview: dict[str, Any]) -> list[str]:
    blocks: list[str] = []
    if preview.get("status") != "option_preview_ready":
        blocks.append(f"preview_status_not_ready:{preview.get('status')}")
    if preview.get("preview_ready") is not True:
        blocks.append("preview_not_ready")
    if preview.get("order_submission_allowed") is not False:
        blocks.append("preview_order_submission_boundary_missing")
    return blocks


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _outcome_id(preview: dict[str, Any], exit_timestamp: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    raw = f"{preview.get('packet_id', 'unknown_packet')}_{preview.get('symbol', 'UNK')}_{preview.get('direction', 'unknown')}_{exit_timestamp}"
    slug = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_")[:96]
    return f"{stamp}_{slug}"


def _write_outcome_artifacts(result: PlaybookShadowOutcomeResult) -> None:
    payload = asdict(result) | {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "lane": "shadow",
        "feedback_target": "mala_playbook_review",
    }
    Path(result.artifact_json).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    Path(result.artifact_md).write_text(_outcome_markdown(payload), encoding="utf-8")


def _outcome_markdown(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Playbook Shadow Outcome",
            "",
            f"- status: `{payload['status']}`",
            f"- lane: `{payload['lane']}`",
            f"- packet: `{payload['packet_id']}` v`{payload['packet_version']}`",
            f"- symbol: `{payload['symbol']}`",
            f"- direction: `{payload['direction']}`",
            f"- option_symbol: `{payload['option_symbol']}`",
            f"- quantity: `{payload['quantity']}`",
            f"- entry_price: `{payload['entry_price']}`",
            f"- exit_price: `{payload['exit_price']}`",
            f"- exit_reason: `{payload['exit_reason']}`",
            f"- gross_pnl_usd: `{payload['gross_pnl_usd']}`",
            f"- pnl_pct: `{payload['pnl_pct']}`",
            f"- pnl_r: `{payload['pnl_r']}`",
            f"- block_reasons: `{', '.join(payload['block_reasons'])}`",
            "",
        ]
    )
