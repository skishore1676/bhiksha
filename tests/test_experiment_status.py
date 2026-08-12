from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bhiksha.ops.experiment_status import (
    STATUS_SCHEMA,
    ExperimentStatusError,
    build_app_experiment_status,
    collect_read_only_facts,
    validate_app_experiment_status,
)


def _plan() -> dict[str, object]:
    return {
        "active_plan_id": "active-plan-test",
        "trading_date": "2026-08-12",
        "generated_at": "2026-08-12T20:00:00+00:00",
        "plan_revision_id": "revision-1",
        "deployments": [
            {
                "deployment_id": "sheet-row-shadow",
                "enabled": True,
                "symbol": "SPY",
                "strategy": {"key": "test", "version": 1, "params": {"x": 1}},
                "execution": {"shadow_only": True},
                "source": {
                    "origin": "google_sheets_control_plane",
                    "metadata": {
                        "authorization_mode": "shadow",
                        "configuration_identity": "sheet-config-v1",
                    },
                },
            }
        ],
    }


def test_empty_sheet_shadow_is_honest_collecting_status() -> None:
    status = build_app_experiment_status(
        _plan(), source_status="ok", as_of="2026-08-12T20:05:00+00:00"
    )
    experiment = status["experiments"][0]

    assert status["schema"] == STATUS_SCHEMA
    assert experiment["experiment_id"] == "sheet-row-shadow"
    assert experiment["stage"] == "shadow"
    assert experiment["configuration_identity"] == "sheet-config-v1"
    assert experiment["health"] == "collecting"
    assert experiment["closed"] == 0
    assert "no_closed_sample" in experiment["limitations"]
    assert status["effects"] == {
        "sheet_write": False,
        "stage_change": False,
        "broker_action": False,
        "order_action": False,
    }


@pytest.mark.parametrize(
    ("source_status", "expected_health", "limitation"),
    [
        ("partial", "inconclusive", "partial_source"),
        ("stale", "inconclusive", "stale_source"),
    ],
)
def test_partial_and_stale_sources_do_not_look_like_zero_performance(
    source_status: str, expected_health: str, limitation: str
) -> None:
    status = build_app_experiment_status(
        _plan(),
        source_status=source_status,
        facts_by_deployment={
            "sheet-row-shadow": {
                "counts": {"observations": 2, "opportunities": 2, "entries": 1, "closed": 0}
            }
        },
        as_of="2026-08-12T20:05:00+00:00",
    )
    experiment = status["experiments"][0]
    assert experiment["health"] == expected_health
    assert limitation in experiment["limitations"]
    assert experiment["metrics"] == {}


@pytest.mark.parametrize("pnl, expected_health", [(1.25, "ready_for_review"), (-0.75, "ready_for_review")])
def test_closed_status_preserves_positive_and_negative_outcomes(
    pnl: float, expected_health: str
) -> None:
    status = build_app_experiment_status(
        _plan(),
        facts_by_deployment={
            "sheet-row-shadow": {
                "counts": {"observations": 4, "opportunities": 3, "entries": 2, "closed": 2},
                "metrics": {"closed_net_r": pnl},
                "observation_window": {
                    "start": "2026-08-11T13:30:00+00:00",
                    "end": "2026-08-12T20:00:00+00:00",
                },
            }
        },
        as_of="2026-08-12T20:05:00+00:00",
    )
    experiment = status["experiments"][0]
    assert experiment["health"] == expected_health
    assert experiment["closed"] == 2
    assert experiment["metrics"]["closed_net_r"] == pnl
    assert "no_closed_sample" not in experiment["limitations"]


def test_status_validator_rejects_effectful_packet_and_preserves_unknown_fields() -> None:
    status = build_app_experiment_status(_plan(), as_of="2026-08-12T20:05:00+00:00")
    status["future_app_field"] = {"ignored_by_tradelab": True}
    validate_app_experiment_status(status)
    status["effects"]["stage_change"] = True
    with pytest.raises(ExperimentStatusError, match="stage_change"):
        validate_app_experiment_status(status)


def test_collect_read_only_facts_reads_db_without_creating_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "facts.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, created_at TEXT, event_type TEXT, payload TEXT)"
        )
        connection.execute(
            "INSERT INTO events VALUES (1, ?, ?, ?)",
            (
                "2026-08-12T15:00:00+00:00",
                "signal_decision",
                json.dumps({"deployment_id": "sheet-row-shadow", "signal": True}),
            ),
        )
        connection.commit()
    before = db_path.stat().st_mtime_ns

    facts, source_status = collect_read_only_facts(db_path)

    assert source_status == "ok"
    assert facts["sheet-row-shadow"]["opportunities"] == 1
    assert db_path.stat().st_mtime_ns == before


def test_status_adapter_source_has_no_runtime_or_writer_imports() -> None:
    source = Path(__file__).parents[1] / "src/bhiksha/ops/experiment_status.py"
    text = source.read_text(encoding="utf-8")
    assert "build_runtime" not in text
    assert "from bhiksha.app" not in text
    assert "from bhiksha.execution" not in text
    assert "from bhiksha.persistence.sqlite" not in text
