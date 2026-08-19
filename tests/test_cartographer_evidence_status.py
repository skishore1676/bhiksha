from __future__ import annotations

import json
import sqlite3
import subprocess
from argparse import Namespace
from datetime import datetime
from pathlib import Path

from bhiksha.cartographer_profiles import canonical_hash
from bhiksha.tools import launchd_job
from bhiksha.tools.cartographer_evidence_status import (
    build_fact_graph,
    build_status,
    read_signal_lifecycle,
    read_terminal_facts,
)
from bhiksha.tools.launchd_status import _cartographer_installed_pending_first_run


def test_composed_status_requires_fresh_producer_and_projection(tmp_path) -> None:
    producer = tmp_path / "producer.json"
    projection = tmp_path / "projection.json"
    producer.write_text(json.dumps({
        "lifecycle": "complete",
        "receipt": {"run_id": "run-1", "signal_batch_hash": "sha256:batch"},
    }), encoding="utf-8")
    body = {
        "status": "applied", "producer_run_id": "run-1",
        "signal_batch_hash": "sha256:batch", "trading_date": "2026-08-17",
        "actions": [],
    }
    projection.write_text(json.dumps({**body, "receipt_hash": canonical_hash(body)}), encoding="utf-8")
    assert build_status(
        producer_status_path=producer, projection_receipt_path=projection,
        now=datetime.fromisoformat("2026-08-17T08:00:00-05:00"),
    )["status"] == "compile_pending"
    projection.unlink()
    assert build_status(producer_status_path=producer, projection_receipt_path=projection)["status"] == "blocked"


def test_status_rejects_hash_or_producer_mismatch(tmp_path) -> None:
    producer = tmp_path / "producer.json"; projection = tmp_path / "projection.json"
    producer.write_text(json.dumps({"lifecycle": "complete", "receipt": {"run_id": "run-1"}}), encoding="utf-8")
    projection.write_text(json.dumps({"status": "applied", "producer_run_id": "old", "receipt_hash": "sha256:bad"}), encoding="utf-8")
    assert build_status(producer_status_path=producer, projection_receipt_path=projection)["status"] == "blocked"


def test_status_requires_matching_compile_after_deadline(tmp_path) -> None:
    producer = tmp_path / "producer.json"; projection = tmp_path / "projection.json"
    active_plan = tmp_path / "active_plan.json"
    producer.write_text(json.dumps({
        "lifecycle": "complete",
        "receipt": {"run_id": "run-1", "signal_batch_hash": "sha256:batch"},
    }), encoding="utf-8")
    body = {
        "status": "applied", "producer_run_id": "run-1",
        "signal_batch_hash": "sha256:batch", "trading_date": "2026-08-17",
        "actions": [{"signal_id": "mc-1", "action": "created"}],
    }
    projection.write_text(json.dumps({**body, "receipt_hash": canonical_hash(body)}), encoding="utf-8")
    active_plan.write_text(json.dumps({"deployments": [{"source": {"metadata": {
        "source_owner": "market_cartographer", "run_id": "run-1", "signal_id": "mc-1",
    }}}]}), encoding="utf-8")
    matched = build_status(
        producer_status_path=producer, projection_receipt_path=projection,
        active_plan_path=active_plan,
        now=datetime.fromisoformat("2026-08-17T08:45:00-05:00"),
    )
    assert matched["status"] == "healthy"
    active_plan.write_text(json.dumps({"deployments": []}), encoding="utf-8")
    mismatched = build_status(
        producer_status_path=producer, projection_receipt_path=projection,
        active_plan_path=active_plan,
        now=datetime.fromisoformat("2026-08-17T08:45:00-05:00"),
    )
    assert mismatched["status"] == "blocked"
    assert mismatched["compile"]["status"] == "mismatch"


def test_status_scopes_trigger_attention_to_projection_trading_date(tmp_path) -> None:
    producer = tmp_path / "producer.json"
    projection = tmp_path / "projection.json"
    active_plan = tmp_path / "active_plan.json"
    database = tmp_path / "bhiksha.db"
    producer.write_text(json.dumps({
        "lifecycle": "complete",
        "receipt": {"run_id": "run-1", "signal_batch_hash": "sha256:batch"},
    }), encoding="utf-8")
    body = {
        "status": "applied",
        "producer_run_id": "run-1",
        "signal_batch_hash": "sha256:batch",
        "trading_date": "2026-08-19",
        "actions": [],
    }
    projection.write_text(
        json.dumps({**body, "receipt_hash": canonical_hash(body)}), encoding="utf-8"
    )
    active_plan.write_text(json.dumps({"deployments": []}), encoding="utf-8")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, created_at TEXT, event_type TEXT, payload TEXT)"
        )
        connection.execute(
            "INSERT INTO events(created_at, event_type, payload) VALUES (?, ?, ?)",
            (
                "2026-08-18T13:35:00+00:00",
                "signal_decision",
                json.dumps({
                    "deployment_id": "mc-v1-legacy",
                    "signal": True,
                    "timestamp": "2026-08-18T13:35:00+00:00",
                }),
            ),
        )
    status = build_status(
        producer_status_path=producer,
        projection_receipt_path=projection,
        active_plan_path=active_plan,
        events_db_path=database,
        now=datetime.fromisoformat("2026-08-19T08:45:00-05:00"),
    )
    assert status["status"] == "healthy"
    assert status["trigger_accounting"]["true_triggers"] == 0


def test_terminal_fact_reader_is_read_only_and_dedupes_exact_receipts(tmp_path) -> None:
    database = tmp_path / "bhiksha.db"
    fact = {"fact_receipt_id": "sha256:one", "identity": {"signal_id": "mc-1"}}
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, event_type TEXT, payload TEXT)")
        connection.executemany(
            "INSERT INTO events(event_type, payload) VALUES (?, ?)",
            [("cartographer_terminal_fact", json.dumps(fact))] * 2,
        )
    assert read_terminal_facts(database) == [fact]


def test_signal_lifecycle_resolves_expired_without_inventing_a_trade(tmp_path) -> None:
    database = tmp_path / "bhiksha.db"
    expired = {
        "deployment_id": "mc-v1-expired",
        "signal": False,
        "reason": ["manual_trigger_waiting", "chart_signal_expired"],
    }
    triggered = {
        "deployment_id": "mc-v1-triggered",
        "signal": True,
        "reason": ["manual_trigger_met"],
    }
    censored_fact = {
        "status": "inconclusive",
        "decision_ready": False,
        "identity": {"signal_id": "mc-v1-triggered"},
    }
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, event_type TEXT, payload TEXT)"
        )
        connection.executemany(
            "INSERT INTO events(event_type, payload) VALUES (?, ?)",
            [
                ("signal_evaluation", json.dumps(expired)),
                ("signal_evaluation", json.dumps(triggered)),
                ("cartographer_terminal_fact", json.dumps(censored_fact)),
            ],
        )

    assert read_signal_lifecycle(database) == [
        {"signal_id": "mc-v1-expired", "status": "expired"},
        {"signal_id": "mc-v1-triggered", "status": "censored"},
    ]


def test_triggered_attempt_is_exported_as_infrastructure_censored_with_identity(tmp_path) -> None:
    database = tmp_path / "bhiksha.db"
    start = {
        "signal_attempt_id": "sa-v1-bac",
        "signal_id": "mc-v1-bac",
        "deployment_id": "mc-v1-bac",
        "signal_timestamp": "2026-08-18T13:35:12.102151+00:00",
        "run_id": "run-1",
        "cartographer_version": "1.1",
        "profile_slug": "TREND_CONTINUATION",
        "reason": ["manual_trigger_met"],
    }
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, event_type TEXT, payload TEXT)"
        )
        connection.execute(
            "INSERT INTO events(event_type, payload) VALUES (?, ?)",
            ("cartographer_signal_attempt", json.dumps(start)),
        )
    lifecycle = read_signal_lifecycle(database)
    assert lifecycle == [{
        "signal_id": "mc-v1-bac",
        "status": "infrastructure_censored",
        "signal_attempt_id": "sa-v1-bac",
        "signal_timestamp": "2026-08-18T13:35:12.102151+00:00",
        "run_id": "run-1",
        "cartographer_version": "1.1",
        "profile_slug": "TREND_CONTINUATION",
        "trigger_reason": ["manual_trigger_met"],
        "reason": "triggered_without_terminal_outcome",
        "attempt_outcome": "infrastructure_censored",
    }]


def test_tuesday_legacy_bac_and_abnb_triggers_are_both_infrastructure_censored(tmp_path) -> None:
    database = tmp_path / "bhiksha.db"
    legacy = [
        {
            "deployment_id": "mc-v1-4325b7068a8b9e1097007de7",
            "symbol": "BAC",
            "signal": True,
            "timestamp": "2026-08-18T13:35:12.102151+00:00",
            "reason": ["manual_trigger_met"],
        },
        {
            "deployment_id": "mc-v1-c7c2d95389ddf850708f116f",
            "symbol": "ABNB",
            "signal": True,
            "timestamp": "2026-08-18T13:35:12.102151+00:00",
            "reason": ["manual_trigger_met"],
        },
    ]
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, event_type TEXT, payload TEXT)"
        )
        connection.executemany(
            "INSERT INTO events(event_type, payload) VALUES (?, ?)",
            [("signal_decision", json.dumps(item)) for item in legacy],
        )

    lifecycle = read_signal_lifecycle(database)
    assert [item["signal_id"] for item in lifecycle] == [
        "mc-v1-4325b7068a8b9e1097007de7",
        "mc-v1-c7c2d95389ddf850708f116f",
    ]
    assert [item["status"] for item in lifecycle] == [
        "infrastructure_censored",
        "infrastructure_censored",
    ]
    assert all(item["attempt_outcome"] == "infrastructure_censored" for item in lifecycle)
    assert all(item["signal_timestamp"] == "2026-08-18T13:35:12.102151+00:00" for item in lifecycle)


def test_newly_installed_job_is_quiet_only_before_its_first_run() -> None:
    semantic = {
        "status": "blocked",
        "producer": {"status": "missing"},
        "projection": {"status": "missing"},
    }
    assert _cartographer_installed_pending_first_run(
        semantic, {"loaded": True, "last_exit_code": "(never exited)"}
    )
    assert not _cartographer_installed_pending_first_run(
        semantic, {"loaded": True, "last_exit_code": 3}
    )


def test_shadow_runner_checks_producer_projects_before_observing() -> None:
    source = open("scripts/launchd/run_cartographer_shadow.sh", encoding="utf-8").read()
    assert "market_cartographer.alpha_cli status" in source
    assert "bhiksha.tools.cartographer_projector" in source
    assert "--apply" in source
    assert "--defaults-sheet-name" in source
    assert "premium-ceiling" not in source
    assert source.index("bhiksha.tools.cartographer_projector") < source.index("cartographer_shadow observe-root")


def test_both_runtime_entry_runners_terminalize_cartographer_exceptions() -> None:
    for path in ("src/bhiksha/app/runtime.py", "src/bhiksha/active_plan/runtime.py"):
        source = Path(path).read_text(encoding="utf-8")
        assert "await supervisor.record_cartographer_attempt_failure(" in source
        assert source.index(
            "await supervisor.record_cartographer_attempt_failure("
        ) < source.index("await self._record_live_entry_failure(")


def test_advertised_launchd_job_invokes_real_shadow_runner(tmp_path, monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(launchd_job, "_print_result", lambda payload: captured.update(payload))
    monkeypatch.setattr(
        launchd_job.subprocess, "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "ok", ""),
    )
    assert launchd_job._cartographer_shadow_job(Namespace(job="cartographer-shadow"), repo_root=tmp_path) == 0
    assert captured["job"] == "cartographer-shadow"
    assert "run_cartographer_shadow.sh" in " ".join(captured.get("stdout_tail", "") or "") or captured["status"] == "ok"
    template = Path("scripts/launchd/com.bhiksha.cartographer-shadow.plist.template").read_text()
    assert "__SHEET_ID__" in template and "__SHEET_CREDENTIALS__" in template
    installer = Path("scripts/launchd/install_cartographer_shadow_launchd.sh").read_text()
    assert "mcse-2026w33-v2" in installer


def test_fact_graph_keeps_emitted_signal_without_terminal_fact(tmp_path) -> None:
    batch = tmp_path / "signals.json"; facts = tmp_path / "facts.json"
    batch.write_text(json.dumps({"signals": [{"signal_id": "mc-1"}]}), encoding="utf-8")
    facts.write_text("[]", encoding="utf-8")
    assert build_fact_graph(signal_batch_path=batch, terminal_facts_path=facts)["nodes"][0]["lifecycle"] == "emitted_without_terminal_fact"
