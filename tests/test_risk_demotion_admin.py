from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path

from bhiksha.risk.demotion_store import DemotionStore
from bhiksha.tools.risk_demotion_admin import main


def _demoted_store(path: Path) -> DemotionStore:
    store = DemotionStore(path)
    for deployment_id in ("iwm-live", "qqq-live"):
        store.record_demotion(
            deployment_id=deployment_id,
            reason="rolling_window_negative_expectancy",
            window_n=10,
            mean_pnl_usd=-20.0,
            threshold_usd=0.0,
            trade_ids=["t1"],
            now=datetime(2026, 7, 16, 13, 0, tzinfo=UTC),
        )
    return store


def test_repromote_requires_exact_confirmation(tmp_path, capsys) -> None:
    path = tmp_path / "demotions.json"
    _demoted_store(path)

    result = main(
        [
            "--store",
            str(path),
            "repromote",
            "--deployment-id",
            "iwm-live",
            "--reason",
            "fresh trial",
            "--approved-by",
            "suman",
            "--pid-path",
            str(tmp_path / "missing.pid"),
            "--confirm-live-state-change",
            "yes",
        ]
    )

    assert result == 2
    assert "must be exactly REPROMOTE" in capsys.readouterr().err
    assert set(DemotionStore(path).load()) == {"iwm-live", "qqq-live"}


def test_repromote_refuses_running_runtime(tmp_path, monkeypatch, capsys) -> None:
    path = tmp_path / "demotions.json"
    _demoted_store(path)
    monkeypatch.setattr(
        "bhiksha.tools.risk_demotion_admin._runtime_status",
        lambda _: {"running": True, "pid": 123},
    )
    monkeypatch.setattr("bhiksha.tools.risk_demotion_admin._runtime_process_pids", lambda: [])

    result = main(
        [
            "--store",
            str(path),
            "repromote",
            "--deployment-id",
            "iwm-live",
            "--reason",
            "fresh trial",
            "--approved-by",
            "suman",
            "--confirm-live-state-change",
            "REPROMOTE",
        ]
    )

    assert result == 2
    assert "runtime is running" in capsys.readouterr().err
    assert set(DemotionStore(path).load()) == {"iwm-live", "qqq-live"}


def test_repromote_updates_batch_atomically_and_emits_receipt(tmp_path, capsys) -> None:
    path = tmp_path / "demotions.json"
    _demoted_store(path)

    result = main(
        [
            "--store",
            str(path),
            "repromote",
            "--deployment-id",
            "iwm-live",
            "--deployment-id",
            "qqq-live",
            "--reason",
            "fresh trial after accounting fix",
            "--approved-by",
            "suman",
            "--pid-path",
            str(tmp_path / "missing.pid"),
            "--confirm-live-state-change",
            "REPROMOTE",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    store = DemotionStore(path)
    assert result == 0
    assert payload["before_active_demotion_ids"] == ["iwm-live", "qqq-live"]
    assert payload["after_active_demotion_ids"] == []
    assert payload["before_sha256"] != payload["after_sha256"]
    assert store.load() == {}
    assert set(store.load_repromotions()) == {"iwm-live", "qqq-live"}
    assert payload["runtime"]["stopped_proof"] == "control_lock_and_pid_process_scan_clear"
    assert payload["runtime"]["control_lock_path"].endswith("missing.control.lock")


def test_repromote_rejects_unknown_id_without_partial_batch_change(tmp_path, capsys) -> None:
    path = tmp_path / "demotions.json"
    _demoted_store(path)
    before = path.read_bytes()

    result = main(
        [
            "--store",
            str(path),
            "repromote",
            "--deployment-id",
            "iwm-live",
            "--deployment-id",
            "unknown",
            "--reason",
            "fresh trial",
            "--approved-by",
            "suman",
            "--pid-path",
            str(tmp_path / "missing.pid"),
            "--confirm-live-state-change",
            "REPROMOTE",
        ]
    )

    assert result == 2
    assert "not actively demoted" in capsys.readouterr().err
    assert path.read_bytes() == before


def test_schema_v1_remains_readable_and_is_upgraded_on_write(tmp_path) -> None:
    path = tmp_path / "demotions.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "demotions": {
                    "iwm-live": {
                        "deployment_id": "iwm-live",
                        "demoted_at": "2026-07-16T13:00:00+00:00",
                        "reason": "rolling_window_negative_expectancy",
                        "window_n": 10,
                        "mean_pnl_usd": -27.94,
                        "threshold_usd": 0.0,
                        "trade_ids": ["t1"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    store = DemotionStore(path)

    assert store.load()["iwm-live"].mean_pnl_usd == -27.94
    store.repromote_many(
        ["iwm-live"],
        reason="fresh trial",
        approved_by="suman",
        now=datetime(2026, 7, 16, 21, 0, tzinfo=UTC),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["demotions"] == {}
    assert payload["repromotions"]["iwm-live"][0]["prior_demotion"]["mean_pnl_usd"] == -27.94


def test_future_demotion_preserves_repromotion_audit_record(tmp_path) -> None:
    path = tmp_path / "demotions.json"
    store = _demoted_store(path)
    first = store.repromote_many(
        ["iwm-live"],
        reason="fresh trial",
        approved_by="suman",
        now=datetime(2026, 7, 16, 21, 0, tzinfo=UTC),
    )["iwm-live"]

    store.record_demotion(
        deployment_id="iwm-live",
        reason="rolling_window_negative_expectancy",
        window_n=10,
        mean_pnl_usd=-12.0,
        threshold_usd=0.0,
        trade_ids=["new1"],
        now=datetime(2026, 7, 20, 21, 0, tzinfo=UTC),
    )

    assert store.load()["iwm-live"].mean_pnl_usd == -12.0
    assert store.load_repromotions()["iwm-live"] == first


def test_repeated_repromotion_preserves_append_only_history(tmp_path) -> None:
    path = tmp_path / "demotions.json"
    store = _demoted_store(path)
    first = store.repromote_many(
        ["iwm-live"],
        reason="first trial",
        approved_by="suman",
        now=datetime(2026, 7, 16, 21, 0, tzinfo=UTC),
    )["iwm-live"]
    store.record_demotion(
        deployment_id="iwm-live",
        reason="fresh_window_negative",
        window_n=10,
        mean_pnl_usd=-12.0,
        threshold_usd=0.0,
        trade_ids=["new1"],
        now=datetime(2026, 7, 20, 21, 0, tzinfo=UTC),
    )
    second = store.repromote_many(
        ["iwm-live"],
        reason="second trial",
        approved_by="suman",
        now=datetime(2026, 7, 21, 21, 0, tzinfo=UTC),
    )["iwm-live"]

    assert store.load_repromotion_history()["iwm-live"] == [first, second]
    assert store.load_repromotions()["iwm-live"] == second


def test_missing_pid_file_still_refuses_detected_runtime_process(
    tmp_path, monkeypatch, capsys
) -> None:
    path = tmp_path / "demotions.json"
    _demoted_store(path)
    monkeypatch.setattr(
        "bhiksha.tools.risk_demotion_admin._runtime_process_pids", lambda: [456]
    )

    result = main(
        [
            "--store",
            str(path),
            "repromote",
            "--deployment-id",
            "iwm-live",
            "--reason",
            "fresh trial",
            "--approved-by",
            "suman",
            "--pid-path",
            str(tmp_path / "missing.pid"),
            "--confirm-live-state-change",
            "REPROMOTE",
        ]
    )

    assert result == 2
    assert "process_scan_pids=[456]" in capsys.readouterr().err
    assert "iwm-live" in DemotionStore(path).load()


def test_process_scan_recognizes_direct_and_bionic_runtime_surfaces(monkeypatch) -> None:
    class Result:
        stdout = """101 python -m bhiksha.tools.trade_session --live
102 python -m bhiksha.tools.dry_run_live_loop --live
103 python -m bhiksha.tools.bionic_session run --live
104 python -m unrelated.tool
"""

    monkeypatch.setattr(
        "bhiksha.tools.risk_demotion_admin.subprocess.run", lambda *args, **kwargs: Result()
    )

    from bhiksha.tools.risk_demotion_admin import _runtime_process_pids

    assert _runtime_process_pids() == [101, 102, 103]
