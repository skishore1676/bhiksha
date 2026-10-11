from copy import deepcopy
import pytest
from bhiksha.ops.strategy_exit_report import summarize, summarize_daily, attach_inferences, report_digest


def test_daily_cohorts_preserve_economics_and_use_central_date_and_monday():
    s = source()
    s['window'] = {'start': '2026-10-04', 'end': '2026-10-06'}
    s['trades'] = [trade('sunday', entry_timestamp='2026-10-05T02:00:00Z'),
                   trade('monday', entry_timestamp='2026-10-05T14:00:00Z', realized_pnl_usd=20)]
    s['cases'] = [case('sunday'), case('monday', a=30, b=40)]
    s['events'] = [event('signal_evaluation', signal=False)]
    report = summarize_daily(s)
    rows = {r['date']: r for r in report['rows']}
    assert rows['2026-10-04']['week_start'] == '2026-09-28'
    assert rows['2026-10-05']['week_start'] == '2026-10-05'
    assert rows['2026-10-04']['signals_fired'] is None
    assert rows['2026-10-05']['signals_fired'] == 0
    assert rows['2026-10-04']['recorded_pnl_usd'] == 10
    assert rows['2026-10-05']['recorded_pnl_usd'] == 20
    assert rows['2026-10-04']['modeled_pnl'] == {'A': 10, 'B': 20}
    assert rows['2026-10-05']['modeled_pnl'] == {'A': 30, 'B': 40}
    assert len({r['row_id'] for r in report['rows']}) == 2


def test_daily_empty_window_does_not_invent_dated_activity():
    report = summarize_daily(source())
    assert report['rows'] == []
    assert report['signals'] == []
    assert report['trades'] == []


def source():
    return {"window": {"start": "2026-10-05", "end": "2026-10-09"},
        "configs": [{"deployment_id": "dep", "revision": "v1", "strategy": "impulse", "lane": "shadow"}],
        "events": [], "trades": [], "cases": []}


def event(kind, **extra):
    return {"event_type": kind, "created_at": "2026-10-05T14:00:00+00:00",
        "payload": {"deployment_id": "dep", "evidence_identity": {"plan_revision_id": "v1"},
                    "signal_id": "signal-1", "timestamp": "2026-10-05T14:00:00+00:00", **extra}}


def trade(trade_id="t", **extra):
    return {"trade_id": trade_id, "deployment_id": "dep", "plan_revision_id": "v1",
        "lane": "shadow", "fill_confirmed": True, "is_open": False,
        "realized_pnl_usd": 10, **extra}


def case(trade_id, a=10, b=20, status_b="complete_clean", frozen="frozen1"):
    return {"trade_id": trade_id, "deployment_id": "dep", "experiment_spec_hash": frozen,
        "experiment_spec": {"named_profiles": [{"policy_id": "A"}, {"policy_id": "B"}]},
        "named_exit_outcomes": {"A": {"realized_pnl_usd": a}, "B": {"realized_pnl_usd": b}},
        "arm_evidence_status": {"A": "complete_clean", "B": status_b}}


def test_unknown_monitoring_is_not_zero_but_observed_zero_is_numeric():
    s = source()
    assert summarize(s)["rows"][0]["signals_fired"] is None
    s["events"] = [event("signal_evaluation", signal=False)]
    r = summarize(s)["rows"][0]
    assert r["signals_fired"] == 0 and r["coverage"] == "partial"


def test_signal_transitions_are_deduplicated_capture_is_not_fill():
    s = source()
    s["events"] = [event("signal_evaluation", signal=True), event("signal_decision", signal=True),
                   event("signal_outcome", outcome="selection_failure"), event("signal_outcome", outcome="pending_execution")]
    r = summarize(s)["rows"][0]
    assert (r["signals_fired"], r["signals_captured"], r["filled"], r["blocked"]) == (1, 1, 0, 1)


def test_unverified_fill_and_missing_closed_economics_stay_visible():
    s = source()
    s["events"] = [event("signal_outcome", outcome="filled", trade_id="not-confirmed")]
    s["trades"] = [trade(), trade("missing", realized_pnl_usd=None)]
    r = summarize(s)["rows"][0]
    assert r["unresolved"] == 1 and r["recorded_pnl_usd"] is None and r["missing_economics"] == 1


def test_exit_columns_use_common_trades_not_different_samples_and_zero_fees():
    s = source()
    s["trades"] = [trade("paired"), trade("gap")]
    s["cases"] = [case("paired", a=10, b=20), case("gap", a=100, b=900, status_b="gap_affected_diagnostic")]
    r = summarize(s)
    assert r["fees_usd"] == 0
    row = r["rows"][0]
    assert row["common_comparisons"] == 1 and row["modeled_pnl"] == {"A": 10, "B": 20}
    assert len(r["exits"]) == 4 and r["exits"][-1]["pnl_usd"] is None


def test_frozen_experiments_and_historical_revisions_never_pool():
    s = source()
    s["trades"] = [trade("t"), trade("u")]
    s["cases"] = [case("t"), case("u", frozen="frozen2")]
    r = summarize(s)
    assert len(r["rows"]) == 2 and len({x["row_id"] for x in r["rows"]}) == 2
    s["trades"].append(trade("old", plan_revision_id="v0", lane="live"))
    r = summarize(s)
    old = next(x for x in r["rows"] if x["revision"] == "v0")
    assert old["strategy"] == "unknown" and old["lane"] == "live"


def test_no_clean_pairs_never_show_modeled_zero():
    s = source()
    s["trades"] = [trade()]
    s["cases"] = [case("t", status_b="still_collecting")]
    r = summarize(s)["rows"][0]
    assert r["common_comparisons"] == 0 and r["modeled_pnl"] == {"A": None, "B": None}


def test_inferences_are_authored_and_bound_to_evidence_not_position():
    report = summarize(source())
    notes = {"author": "Codex", "written_at": "2026-10-11T14:00:00Z",
        "report_digest": report_digest(report), "rows": [{"row_id": report["rows"][0]["row_id"],
        "assessment": "evidence blocked", "reason": "No evaluation receipts."}]}
    attach_inferences(report, notes)
    assert report["rows"][0]["inference"]["next_step"] == "None"
    changed = deepcopy(report)
    changed["rows"][0]["signals_fired"] = 5
    with pytest.raises(ValueError, match="digest"):
        attach_inferences(changed, notes)
    notes["rows"][0]["assessment"] = "promote live"
    with pytest.raises(ValueError, match="assessment"):
        attach_inferences(report, notes)


def test_identical_frozen_deployments_can_consolidate_without_relabeling_history():
    s = source()
    s["configs"][0]["config_fingerprint"] = "same"
    s["configs"].append({**s["configs"][0], "revision": "v2"})
    s["events"] = [event("signal_evaluation", signal=False),
        event("signal_evaluation", signal=False, evidence_identity={"plan_revision_id": "v2"})]
    r = summarize(s)["rows"]
    assert len(r) == 1 and r[0]["evaluation_count"] == 2 and r[0]["revisions"] == ["v1", "v2"]


def test_real_sqlite_extraction_and_missing_source_are_read_only(tmp_path):
    import sqlite3
    import json
    from datetime import date
    from bhiksha.ops.strategy_exit_report import extract
    from bhiksha.ops.exit_edge_lab import ProspectiveQuoteTapeRepository
    db, edge = tmp_path / "trading.db", tmp_path / "edge.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE events(id INTEGER PRIMARY KEY,created_at TEXT,event_type TEXT,payload TEXT)")
        startup = {"active_plan": {"plan_revision_id": "v1"}, "deployments": [{
            "deployment_id": "dep", "symbol": "SPY", "strategy": {"key": "impulse", "version": 1},
            "execution": {"shadow_only": False}}]}
        c.execute("INSERT INTO events VALUES(1,?,?,?)", ("2026-10-05T13:00:00Z", "startup_config", json.dumps(startup)))
        e = event("signal_evaluation", signal=True)
        c.execute("INSERT INTO events VALUES(2,?,?,?)", (e["created_at"], e["event_type"], json.dumps(e["payload"])))
        c.execute("CREATE TABLE trade_sessions(trade_id TEXT,deployment_id TEXT,symbol TEXT,option_symbol TEXT,"
                  "plan_revision_id TEXT,quantity INTEGER,entry_price REAL,entry_timestamp TEXT,status TEXT,"
                  "entry_order_id TEXT,exit_price REAL,exit_filled_at TEXT)")
        c.execute("INSERT INTO trade_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
            "t","dep","SPY","SPY_OPTION","v1",1,2,"2026-10-05T14:00:00+00:00","closed","BROKER",3,"2026-10-05T15:00:00+00:00"))
    ProspectiveQuoteTapeRepository(edge).initialize()
    before = db.read_bytes(), edge.read_bytes()
    r = summarize(extract(db, edge, date(2026,10,5), date(2026,10,9)))
    assert r["trades"][0]["realized_pnl_usd"] == 100
    assert r["trades"][0]["fill_confirmed"] is True
    assert before == (db.read_bytes(), edge.read_bytes())
    missing = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        extract(missing, edge, date(2026,10,5), date(2026,10,9))
    assert not missing.exists()
