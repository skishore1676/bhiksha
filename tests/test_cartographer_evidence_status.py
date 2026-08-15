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
    read_terminal_facts,
)


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


def test_shadow_runner_checks_producer_projects_before_observing() -> None:
    source = open("scripts/launchd/run_cartographer_shadow.sh", encoding="utf-8").read()
    assert "market_cartographer.alpha_cli status" in source
    assert "bhiksha.tools.cartographer_projector" in source
    assert "--apply" in source
    assert source.index("bhiksha.tools.cartographer_projector") < source.index("cartographer_shadow observe-root")


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


def test_fact_graph_keeps_emitted_signal_without_terminal_fact(tmp_path) -> None:
    batch = tmp_path / "signals.json"; facts = tmp_path / "facts.json"
    batch.write_text(json.dumps({"signals": [{"signal_id": "mc-1"}]}), encoding="utf-8")
    facts.write_text("[]", encoding="utf-8")
    assert build_fact_graph(signal_batch_path=batch, terminal_facts_path=facts)["nodes"][0]["lifecycle"] == "emitted_without_terminal_fact"
