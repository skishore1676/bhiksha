from __future__ import annotations

import asyncio
import json
from pathlib import Path

from bhiksha.execution.order_manager import PublicQuote
from bhiksha.packets.live_ticket import APPROVAL_PHRASE, create_playbook_live_ticket
from bhiksha.packets.shadow_outcome import record_playbook_shadow_outcome
from bhiksha.tools.create_playbook_live_ticket import main as live_ticket_main
from bhiksha.tools.record_playbook_shadow_outcome import main as shadow_outcome_main


class StubOrderManager:
    async def get_option_quote(self, option_symbol: str):
        return PublicQuote(
            symbol=option_symbol,
            bid=3.40,
            ask=3.60,
            last=3.50,
            open_interest=500,
            outcome="SUCCESS",
        )


def test_shadow_outcome_records_manual_eod_pnl(tmp_path: Path) -> None:
    preview_path = _write_preview(tmp_path)

    result = asyncio.run(
        record_playbook_shadow_outcome(
            option_preview_artifact=preview_path,
            exit_timestamp="2026-05-11 15:55 America/New_York",
            exit_reason="end_of_day_mark",
            exit_price=3.40,
            out_root=tmp_path / "shadow_outcomes",
        )
    )

    assert result.status == "shadow_outcome_recorded"
    assert result.gross_pnl_usd == 50.0
    assert result.pnl_pct == 0.172414
    assert result.pnl_r == 0.172414
    payload = json.loads(Path(result.artifact_json).read_text(encoding="utf-8"))
    assert payload["lane"] == "shadow"
    assert payload["feedback_target"] == "mala_playbook_review"
    assert Path(result.artifact_md).exists()


def test_shadow_outcome_can_use_current_quote_for_exit_price(tmp_path: Path) -> None:
    preview_path = _write_preview(tmp_path)

    result = asyncio.run(
        record_playbook_shadow_outcome(
            option_preview_artifact=preview_path,
            exit_timestamp="2026-05-11 15:55 America/New_York",
            order_manager=StubOrderManager(),
            out_root=tmp_path / "shadow_outcomes",
        )
    )

    assert result.status == "shadow_outcome_recorded"
    assert result.exit_price == 3.40
    assert result.gross_pnl_usd == 50.0


def test_shadow_outcome_blocks_non_ready_preview(tmp_path: Path) -> None:
    preview_path = _write_preview(tmp_path, status="blocked", preview_ready=False)

    result = asyncio.run(
        record_playbook_shadow_outcome(
            option_preview_artifact=preview_path,
            exit_timestamp="2026-05-11 15:55 America/New_York",
            exit_price=3.40,
            out_root=tmp_path / "shadow_outcomes",
        )
    )

    assert result.status == "blocked"
    assert "preview_status_not_ready:blocked" in result.block_reasons


def test_live_ticket_requires_exact_approval_phrase(tmp_path: Path) -> None:
    preview_path = _write_preview(tmp_path)

    result = create_playbook_live_ticket(
        option_preview_artifact=preview_path,
        decision="approve",
        operator="Suman",
        operator_note="Approve one contract only.",
        approval_phrase="APPROVE",
        out_root=tmp_path / "live_tickets",
    )

    assert result.status == "blocked"
    assert result.order_submission_allowed is False
    assert result.block_reasons == ["live_approval_phrase_missing"]


def test_live_ticket_approval_writes_order_allowed_ticket_without_submission(tmp_path: Path) -> None:
    preview_path = _write_preview(tmp_path)

    result = create_playbook_live_ticket(
        option_preview_artifact=preview_path,
        decision="approve",
        operator="Suman",
        operator_note="Approve one contract only; submitter still must execute separately.",
        approval_phrase=APPROVAL_PHRASE,
        out_root=tmp_path / "live_tickets",
    )

    assert result.status == "live_ticket_approved"
    assert result.order_submission_allowed is True
    assert result.live_approval_required is False
    payload = json.loads(Path(result.artifact_json).read_text(encoding="utf-8"))
    assert payload["lane"] == "live"
    assert payload["submitter_status"] == "not_submitted"
    assert payload["management_spec"]["stop_anchor"] == "underlying_reversal_extreme"


def test_live_ticket_reject_records_non_submittable_ticket(tmp_path: Path) -> None:
    preview_path = _write_preview(tmp_path)

    result = create_playbook_live_ticket(
        option_preview_artifact=preview_path,
        decision="reject",
        operator="Suman",
        operator_note="Rejecting because spread changed.",
        out_root=tmp_path / "live_tickets",
    )

    assert result.status == "live_ticket_rejected"
    assert result.order_submission_allowed is False
    assert result.live_approval_required is True


def test_shadow_outcome_cli_returns_block_code_without_exit_price(tmp_path: Path) -> None:
    preview_path = _write_preview(tmp_path)

    code = shadow_outcome_main(
        [
            "--option-preview-artifact",
            str(preview_path),
            "--exit-timestamp",
            "2026-05-11 15:55 America/New_York",
            "--out-root",
            str(tmp_path / "shadow_outcomes"),
        ]
    )

    assert code == 2


def test_live_ticket_cli_returns_block_code_for_missing_phrase(tmp_path: Path) -> None:
    preview_path = _write_preview(tmp_path)

    code = live_ticket_main(
        [
            "--option-preview-artifact",
            str(preview_path),
            "--decision",
            "approve",
            "--operator",
            "Suman",
            "--operator-note",
            "Approve without the exact phrase should block.",
            "--out-root",
            str(tmp_path / "live_tickets"),
        ]
    )

    assert code == 2


def _write_preview(
    tmp_path: Path,
    *,
    status: str = "option_preview_ready",
    preview_ready: bool = True,
) -> Path:
    payload = {
        "status": status,
        "preview_ready": preview_ready,
        "packet_id": "execution.mean_reversion_at_extremes.iwm_qqq",
        "packet_version": 1,
        "symbol": "IWM",
        "direction": "short",
        "timestamp": "2026-05-11 09:40 America/Chicago",
        "selected_management_policy_id": "reversal_extreme__fixed_1r",
        "management_spec": {
            "policy_id": "reversal_extreme__fixed_1r",
            "stop_family": "reversal_extreme",
            "stop_anchor": "underlying_reversal_extreme",
            "exit_family": "fixed_1r",
            "target_model": "fixed_r",
            "target_r": 1.0,
            "hard_flat_time_et": "15:55",
            "option_stop_fallback_pct": 0.45,
            "target_order_mode": "virtual_or_broker",
            "source_config_id": "cfg_1",
        },
        "option_symbol": "IWM260330P00558000",
        "quantity": 1,
        "estimated_entry_price": 2.90,
        "underlying_entry_price": 286.38,
        "underlying_stop_price": 287.10,
        "risk_reasons": ["approved"],
        "block_reasons": [],
        "order_submission_allowed": False,
        "live_approval_required": True,
    }
    path = tmp_path / "playbook_option_preview.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path
