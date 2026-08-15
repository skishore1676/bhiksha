from __future__ import annotations

import json

from bhiksha.tools.cartographer_evidence_status import build_fact_graph, build_status


def test_composed_status_requires_fresh_producer_and_projection(tmp_path) -> None:
    producer = tmp_path / "producer.json"
    projection = tmp_path / "projection.json"
    producer.write_text(json.dumps({"lifecycle": "complete"}), encoding="utf-8")
    projection.write_text(json.dumps({"status": "applied"}), encoding="utf-8")
    assert build_status(producer_status_path=producer, projection_receipt_path=projection)["status"] == "healthy"
    projection.unlink()
    assert build_status(producer_status_path=producer, projection_receipt_path=projection)["status"] == "blocked"


def test_shadow_runner_checks_producer_projects_before_observing() -> None:
    source = open("scripts/launchd/run_cartographer_shadow.sh", encoding="utf-8").read()
    assert "market_cartographer.alpha_cli status" in source
    assert "bhiksha.tools.cartographer_projector" in source
    assert "--apply" in source
    assert source.index("bhiksha.tools.cartographer_projector") < source.index("cartographer_shadow observe-root")


def test_fact_graph_keeps_emitted_signal_without_terminal_fact(tmp_path) -> None:
    batch = tmp_path / "signals.json"; facts = tmp_path / "facts.json"
    batch.write_text(json.dumps({"signals": [{"signal_id": "mc-1"}]}), encoding="utf-8")
    facts.write_text("[]", encoding="utf-8")
    assert build_fact_graph(signal_batch_path=batch, terminal_facts_path=facts)["nodes"][0]["lifecycle"] == "emitted_without_terminal_fact"
