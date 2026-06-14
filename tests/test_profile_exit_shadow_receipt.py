"""Tests for the fixture SHADOW profile-exit receipt driver.

Proves the driver: replays every profile's path through the real shadow
evaluator with the gate CLOSED, records shadow events, NEVER dispatches / places
an order / touches a broker, persists+clears supervisor-owned ladder state, and
writes an inspectable JSON+MD artifact.
"""

from __future__ import annotations

import json

from bhiksha.tools.profile_exit_shadow_receipt import main, run


def test_shadow_receipt_runs_with_no_dispatch_and_full_coverage(tmp_path) -> None:
    json_path, md_path, receipt = run(tmp_path)

    # Hard shadow contract: nothing reached the broker.
    assert receipt["no_dispatch_occurred"] is True
    assert receipt["total_dispatched_events"] == 0
    assert receipt["placed_orders"] == 0
    assert receipt["touched_broker_api"] is False
    assert receipt["gate_state"] == "CLOSED (shadow)"

    # All four operator profiles are covered.
    assert receipt["profiles_covered"] == [
        "EXHAUSTION_REVERSAL",
        "FLASH_REVERSAL",
        "RANGE_EXPANSION",
        "TREND_CONTINUATION",
    ]

    # The union of fired rungs covers the full terminal ladder + the partial.
    rungs = set(receipt["ladder_rungs_observed"])
    assert {
        "target_1_partial",
        "target_2_runner",
        "high_water_giveback",
        "no_progress",
        "max_hold",
        "eod_flat",
    } <= rungs

    # Breakeven (stop-to-breakeven) was emitted at least once.
    assert receipt["breakeven_actions_emitted"] >= 1

    # Every scenario reached its expected terminal.
    assert receipt["all_terminals_matched"] is True
    for s in receipt["scenarios"]:
        assert s["terminal_matched"] is True, s["scenario"]
        assert s["any_dispatched"] is False, s["scenario"]
        # Supervisor-owned ladder state existed across ticks and was cleared.
        assert s["state_persisted_then_cleared"] is True, s["scenario"]
        # Each recorded tick is a shadow record, never a dispatch.
        for tick in s["ticks"]:
            assert tick["dispatched"] is False


def test_shadow_receipt_writes_inspectable_artifacts(tmp_path) -> None:
    json_path, md_path, receipt = run(tmp_path)

    assert json_path.exists() and md_path.exists()
    # JSON is valid and round-trips the receipt.
    loaded = json.loads(json_path.read_text())
    assert loaded["artifact_label"] == "fixture shadow"
    assert loaded["no_dispatch_occurred"] is True
    assert loaded["raw_shadow_events"], "raw shadow events must be recorded"
    # Every raw event is a shadow_record (no live_dispatch), reinforcing the gate.
    for event in loaded["raw_shadow_events"]:
        assert event["mode"] == "shadow_record"
        assert event["dispatch_allowed"] is False

    md = md_path.read_text()
    assert "Profile-Exit Shadow Receipt (fixture shadow)" in md
    assert "no dispatch occurred: **True**" in md
    for profile in ("FLASH_REVERSAL", "EXHAUSTION_REVERSAL", "TREND_CONTINUATION", "RANGE_EXPANSION"):
        assert profile in md


def test_shadow_receipt_target1_partial_keeps_runner(tmp_path) -> None:
    # A target-1 partial banks part of the position; the runner keeps going to a
    # later terminal rung (the scenario does NOT stop at the partial).
    _, _, receipt = run(tmp_path)
    flash = next(
        s for s in receipt["scenarios"] if s["scenario"] == "flash_reversal_target2"
    )
    assert flash["target_1_partial_fired"] is True
    assert flash["breakeven_emitted"] is True
    assert flash["observed_terminal_rule"] == "target_2_runner"
    # The partial fired BEFORE the terminal (runner survived the bank).
    rules = [t["rule"] for t in flash["ticks"]]
    assert rules.index("target_1_partial") < rules.index("target_2_runner")


def test_shadow_receipt_main_returns_zero(tmp_path, capsys) -> None:
    rc = main(["--out-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no_dispatch_occurred=True" in out
    assert "artifact_json =" in out
    assert "artifact_md   =" in out
