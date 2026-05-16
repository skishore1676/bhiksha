from __future__ import annotations

import json
from pathlib import Path

import pytest

from bhiksha.packets.operator_decision import record_playbook_operator_decision
from bhiksha.tools.decide_playbook_trade import main as decide_main


def test_operator_decision_records_pass_without_management_policy(tmp_path: Path) -> None:
    consultation_path = _write_consultation(tmp_path, policy="pass")

    result = record_playbook_operator_decision(
        consultation_artifact=consultation_path,
        decision="pass",
        operator_note="Not clean enough for live options risk.",
        out_root=tmp_path / "intents",
    )

    assert result.status == "operator_pass"
    assert result.execution_ready is False
    assert result.order_submission_allowed is False
    assert result.block_reasons == []
    payload = json.loads(Path(result.artifact_json).read_text(encoding="utf-8"))
    assert payload["decision"] == "pass"
    assert payload["safety_boundary"] == "shadow_intent_only_no_order_submission"
    assert Path(result.artifact_md).exists()


def test_operator_decision_take_requires_allowed_management_policy(tmp_path: Path) -> None:
    consultation_path = _write_consultation(tmp_path, policy="take")

    result = record_playbook_operator_decision(
        consultation_artifact=consultation_path,
        decision="take",
        operator_note="Taking with the fast fixed-risk management row.",
        selected_management_policy_id="not_allowed",
        out_root=tmp_path / "intents",
    )

    assert result.status == "blocked"
    assert result.execution_ready is False
    assert result.block_reasons == ["management_policy_not_allowed:not_allowed"]


def test_operator_decision_take_creates_shadow_intent(tmp_path: Path) -> None:
    consultation_path = _write_consultation(tmp_path, policy="take")

    result = record_playbook_operator_decision(
        consultation_artifact=consultation_path,
        decision="take",
        operator_note="Taking; clean rejection and policy row is manageable.",
        selected_management_policy_id="reversal_extreme__fixed_1r",
        out_root=tmp_path / "intents",
    )

    assert result.status == "shadow_intent_ready"
    assert result.execution_ready is True
    assert result.execution_mode == "shadow"
    assert result.order_submission_allowed is False
    assert result.warning_reasons == []
    payload = json.loads(Path(result.artifact_json).read_text(encoding="utf-8"))
    assert payload["selected_management_policy_id"] == "reversal_extreme__fixed_1r"


def test_operator_decision_records_mala_policy_override_warning(tmp_path: Path) -> None:
    consultation_path = _write_consultation(tmp_path, policy="pass")

    result = record_playbook_operator_decision(
        consultation_artifact=consultation_path,
        decision="take",
        operator_note="Taking despite card pass because live chart is cleaner than analog bucket.",
        selected_management_policy_id="reversal_extreme__fixed_1r",
        out_root=tmp_path / "intents",
    )

    assert result.status == "shadow_intent_ready"
    assert result.warning_reasons == ["operator_overrode_mala_policy"]


def test_operator_decision_blocks_if_consultation_packet_was_not_clear(tmp_path: Path) -> None:
    consultation_path = _write_consultation(
        tmp_path,
        policy="take",
        compile_block_reasons=["capability_manifest_missing"],
    )

    result = record_playbook_operator_decision(
        consultation_artifact=consultation_path,
        decision="take",
        operator_note="Would take, but packet compile was not clear.",
        selected_management_policy_id="reversal_extreme__fixed_1r",
        out_root=tmp_path / "intents",
    )

    assert result.status == "blocked"
    assert "compile_block:capability_manifest_missing" in result.block_reasons


def test_decide_playbook_trade_cli_returns_block_code_for_invalid_take(tmp_path: Path) -> None:
    consultation_path = _write_consultation(tmp_path, policy="take")

    code = decide_main(
        [
            "--consultation-artifact",
            str(consultation_path),
            "--decision",
            "take",
            "--operator-note",
            "Trying to take without selecting management.",
            "--out-root",
            str(tmp_path / "intents"),
        ]
    )

    assert code == 2


def test_operator_decision_requires_note(tmp_path: Path) -> None:
    consultation_path = _write_consultation(tmp_path)

    with pytest.raises(ValueError, match="operator_note is required"):
        record_playbook_operator_decision(
            consultation_artifact=consultation_path,
            decision="pass",
            operator_note=" ",
        )


def _write_consultation(
    tmp_path: Path,
    *,
    policy: str = "take",
    compile_block_reasons: list[str] | None = None,
    compile_eligibility: str = "eligible",
) -> Path:
    payload = {
        "status": "consulted",
        "packet_id": "execution.mean_reversion_at_extremes.iwm_qqq",
        "packet_version": 1,
        "runtime_mode": "shadow",
        "symbol": "IWM",
        "direction": "short",
        "timestamp": "2026-05-11 09:40 America/Chicago",
        "compile_eligibility": compile_eligibility,
        "compile_decision": "take",
        "compile_block_reasons": compile_block_reasons or [],
        "verdict": "constructive",
        "policy": policy,
        "allowed_management_policy_ids": [
            "reversal_extreme__fixed_1r",
            "immediate_entry_bar_failure__fixed_2r",
        ],
    }
    path = tmp_path / "consultation_bridge.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path
