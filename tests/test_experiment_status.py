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
from bhiksha.tools import experiment_status as experiment_status_tool


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
    assert experiment["paper_live"] == "paper"
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


def test_status_preserves_human_strategy_name_and_live_mode() -> None:
    plan = _plan()
    deployment = plan["deployments"][0]
    deployment["execution"]["shadow_only"] = False
    deployment["source"]["metadata"].update(
        {
            "authorization_mode": "live",
            "catalog_symbol": "SPY",
            "direction": "long",
            "playbook_summary": {
                "mala_evidence": {"strategy_name": "Opening Drive"}
            },
        }
    )

    experiment = build_app_experiment_status(
        plan, as_of="2026-08-12T20:05:00+00:00"
    )["experiments"][0]

    assert experiment["stage"] == "live"
    assert experiment["paper_live"] == "live"
    assert experiment["strategy_name"] == "Opening Drive"
    assert experiment["display_name"] == "SPY Long — Opening Drive"


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
        connection.execute(
            "INSERT INTO events VALUES (2, ?, ?, ?)",
            (
                "2026-08-12T15:01:00+00:00",
                "irrelevant_large_payload",
                json.dumps(
                    {"deployment_id": "sheet-row-shadow", "signal": True, "noise": "x" * 1000}
                ),
            ),
        )
        connection.commit()
    before = db_path.stat().st_mtime_ns

    facts, source_status = collect_read_only_facts(db_path)

    assert source_status == "ok"
    assert facts["sheet-row-shadow"]["opportunities"] == 1
    assert db_path.stat().st_mtime_ns == before


def test_cli_applies_as_of_as_the_database_cutoff(monkeypatch, capsys) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(experiment_status_tool, "load_active_plan", lambda _: _plan())

    def collect(*args, **kwargs):
        captured.update(kwargs)
        return {}, "ok"

    monkeypatch.setattr(experiment_status_tool, "collect_read_only_facts", collect)

    assert experiment_status_tool.main(
        [
            "--active-plan",
            "unused.json",
            "--db-path",
            "unused.sqlite3",
            "--as-of",
            "2026-08-07",
        ]
    ) == 0
    capsys.readouterr()
    assert captured["through"] == "2026-08-07"


def test_collect_read_only_facts_respects_cutoff_and_hides_future_close(tmp_path: Path) -> None:
    db_path = tmp_path / "facts.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, created_at TEXT, event_type TEXT, payload TEXT)"
        )
        connection.execute(
            "CREATE TABLE trade_sessions (trade_id TEXT, deployment_id TEXT, status TEXT, entry_timestamp TEXT, exit_filled_at TEXT, entry_price REAL, exit_price REAL, exit_filled_quantity INTEGER, quantity INTEGER)"
        )
        connection.executemany(
            "INSERT INTO events VALUES (?, ?, ?, ?)",
            [
                (1, "2026-08-07T15:00:00+00:00", "signal_decision", json.dumps({"deployment_id": "sheet-row-shadow", "signal": True})),
                (2, "2026-08-08T15:00:00+00:00", "signal_decision", json.dumps({"deployment_id": "sheet-row-shadow", "signal": True})),
            ],
        )
        connection.executemany(
            "INSERT INTO trade_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("closed-before", "sheet-row-shadow", "closed", "2026-08-07T14:00:00+00:00", "2026-08-07T15:00:00+00:00", 1.0, 1.5, 1, 1),
                ("closed-after", "sheet-row-shadow", "closed", "2026-08-07T14:00:00+00:00", "2026-08-08T15:00:00+00:00", 1.0, 2.0, 1, 1),
            ],
        )
        connection.commit()

    facts, source_status = collect_read_only_facts(db_path, through="2026-08-07")

    assert source_status == "ok"
    assert facts["sheet-row-shadow"]["opportunities"] == 1
    assert facts["sheet-row-shadow"]["entries"] == 2
    assert facts["sheet-row-shadow"]["closed"] == 1
    assert facts["sheet-row-shadow"]["realized_pnl_usd"] == 50.0

    status = build_app_experiment_status(
        _plan(), facts_by_deployment=facts, as_of="2026-08-07"
    )
    assert status["experiments"][0]["metrics"]["realized_pnl_usd"] == 50.0


def test_trade_session_pnl_overrides_empty_shadow_event_placeholder(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "facts.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, created_at TEXT, event_type TEXT, payload TEXT)"
        )
        connection.execute(
            "CREATE TABLE trade_sessions (trade_id TEXT, deployment_id TEXT, status TEXT, entry_timestamp TEXT, exit_filled_at TEXT, entry_price REAL, exit_price REAL, exit_filled_quantity INTEGER, quantity INTEGER)"
        )
        connection.execute(
            "INSERT INTO events VALUES (1, ?, ?, ?)",
            (
                "2026-08-07T14:30:00+00:00",
                "shadow_mark",
                json.dumps(
                    {
                        "deployment_id": "sheet-row-shadow",
                        "trade_id": "live-trade",
                        "mark_price": 1.25,
                    }
                ),
            ),
        )
        connection.execute(
            "INSERT INTO trade_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "live-trade",
                "sheet-row-shadow",
                "closed",
                "2026-08-07T14:00:00+00:00",
                "2026-08-07T15:00:00+00:00",
                1.0,
                1.5,
                1,
                1,
            ),
        )
        connection.commit()

    facts, _ = collect_read_only_facts(db_path, through="2026-08-07")
    status = build_app_experiment_status(
        _plan(), facts_by_deployment=facts, as_of="2026-08-07"
    )

    assert status["experiments"][0]["metrics"]["realized_pnl_usd"] == 50.0


def test_status_adapter_source_has_no_runtime_or_writer_imports() -> None:
    source = Path(__file__).parents[1] / "src/bhiksha/ops/experiment_status.py"
    text = source.read_text(encoding="utf-8")
    assert "build_runtime" not in text
    assert "from bhiksha.app" not in text
    assert "from bhiksha.execution" not in text
    assert "from bhiksha.persistence.sqlite" not in text
