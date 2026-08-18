from __future__ import annotations

from bhiksha.ops.cartographer_operations import owner_status


def _producer(status: str, lifecycle: str = "complete", attention: bool = False) -> dict[str, object]:
    return {"schema": "market_cartographer.alpha_owner_status.v1", "status": status, "lifecycle": lifecycle, "attention_required": attention, "reason": "fixture"}


def test_quiet_and_recovery_are_visible_without_human_attention() -> None:
    quiet = owner_status(_producer("no_plan"))
    assert quiet["last_run_status"] == "no_signal"
    assert quiet["attention_required"] is False
    recovering = owner_status(_producer("running", lifecycle="running"))
    assert recovering["lifecycle"] == "recovering"
    assert recovering["attention_required"] is False


def test_missing_evidence_and_projection_readback_failures_are_attention_items() -> None:
    missing = owner_status(_producer("terminal_evidence_missing", lifecycle="blocked", attention=True))
    assert missing["attention_required"] is True
    failed = owner_status(_producer("succeeded"), projection={"status": "failed"})
    assert failed["last_run_status"] == "projection_failed"
    assert failed["attention_required"] is True


def test_trigger_accounting_remainder_is_projected_as_semantic_attention() -> None:
    status = owner_status(
        _producer("succeeded"),
        projection={"status": "succeeded"},
        trigger_accounting={
            "status": "attention",
            "true_triggers": 2,
            "accounted": 1,
            "remainder": 1,
        },
    )
    assert status["lifecycle"] == "blocked"
    assert status["attention_required"] is True
    assert status["last"]["trigger_accounting"]["remainder"] == 1
