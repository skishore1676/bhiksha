from __future__ import annotations

from datetime import date
import json
import sqlite3

from bhiksha.ops import exit_comparisons_sheet as sheet


class _Request:
    def __init__(self, value):
        self.value = value

    def execute(self, **kwargs):
        del kwargs
        return self.value


class _Api:
    def __init__(self):
        self.writes = []

    def get(self, **kwargs):
        del kwargs
        return _Request({"sheets": [{"properties": {"title": sheet.TAB, "sheetId": 42,
                                                    "gridProperties": {"rowCount": 100, "columnCount": 19}}}]})

    def batchUpdate(self, **kwargs):
        self.writes.append(kwargs["body"])
        return _Request({"replies": [{}]})


class _Service:
    def __init__(self):
        self.api = _Api()

    def spreadsheets(self):
        return self.api


def _case(trade, *, delta=None, gap=False, censor=False):
    return {"trade_id": trade, "deployment_id": "dep", "cohort_dimensions": {
        "runtime_mode": "shadow", "strategy_class": "market_impulse", "entry_fill_kind": "modeled_ask_touch"},
        "experiment_spec_hash": "frozen-policy", "experiment_spec": {
            "named_profiles": [{"policy_id": "trend_continuation_balanced"},
                               {"policy_id": "flash_reversal_fast_snap"}], "evaluator_version": "named-exits.v2"},
        "entry_timestamp": "2026-09-22T14:00:00+00:00", "latest_quote_at": "2026-09-22T14:05:00+00:00",
        "common_entry_risk_usd": 100,
        "clean_candidate_delta_pnl_usd": {"flash_reversal_fast_snap": delta} if delta is not None else {},
        "named_exit_outcomes": {"trend_continuation_balanced": {"exit_rule": "initial_stop",
                                "exit_timestamp": "2026-09-22T14:05:00+00:00", "realized_pnl_usd": -375,
                                "legs": [{"quantity": 1}]},
                                "flash_reversal_fast_snap": {"exit_rule": "initial_stop",
                                "exit_timestamp": "2026-09-22T14:04:00+00:00", "realized_pnl_usd": -330,
                                "legs": [{"quantity": 1}]}},
        "observation_gaps": ([{"last_received_at": "2026-09-22T14:02:00+00:00",
                               "first_received_at": "2026-09-22T14:07:00+00:00",
                               "reason": "observation_interval_exceeded"}] if gap else []),
        "insufficient_reason": "persisted_censor:restart_gap_unobserved_quotes" if censor else None}


def _scorecard():
    return {"trading_date": "2026-09-22", "generated_at": "2026-09-23T15:00:00+00:00",
            "signals": {"rows": [["Market Impulse", "Shadow", 2, 1, 1, 0,
                                  "Selection Failure (1)", "Trend Continuation"]],
                        "recorded": 2, "captured": 1, "missed": 1, "pending": 0,
                        "latest": "2026-09-22T14:00:00+00:00"},
            "exits": sheet._exit_review([_case("clean", delta=45), _case("censored", gap=True, censor=True)],
                                        date(2026, 9, 22))}


def test_sheet_is_scoped_report_and_clears_old_dump():
    service = _Service()
    receipt = sheet.publish_exit_comparisons_scorecard(
        _scorecard(), spreadsheet_id="sheet-id", credentials_path="/tmp/unused.json", service=service)
    assert receipt["status"] == "ok" and receipt["recorded_signals"] == 2
    requests = service.api.writes[0]["requests"]
    assert all("deleteSheet" not in request for request in requests)
    block = next(request["updateCells"] for request in requests if "updateCells" in request)
    assert block["range"]["sheetId"] == 42
    assert block["range"]["endColumnIndex"] == 19
    rows = block["rows"]
    assert any(row["values"][0]["userEnteredValue"].get("stringValue") == "SIGNAL CAPTURE" for row in rows)
    assert any(row["values"][2]["userEnteredValue"].get("numberValue") == 2 for row in rows)
    assert any(row["values"][0]["userEnteredValue"].get("stringValue", "").startswith("EXIT CHOICES") for row in rows)
    assert any(row["values"][0]["userEnteredValue"].get("stringValue", "").startswith("EXPERIMENT TO DATE") for row in rows)
    assert "formulaValue" in rows[2]["values"][3]["userEnteredValue"]
    assert any(request.get("updateDimensionProperties", {}).get("properties", {}).get("hiddenByUser") for request in requests)


def test_recorded_signal_count_deduplicates_transitions_and_explains_misses(tmp_path):
    db = tmp_path / "signals.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, created_at TEXT, event_type TEXT, payload TEXT)")
        def add(event_type, payload):
            conn.execute("INSERT INTO events(created_at,event_type,payload) VALUES(?,?,?)",
                         ("2026-09-22T14:00:00+00:00", event_type, json.dumps(payload)))
        base = {"deployment_id": "dep", "timestamp": "2026-09-22T14:00:00+00:00", "direction": "long"}
        add("signal_decision", {**base, "signal": True})
        add("signal_outcome", {**base, "outcome": "pending_execution", "mode": "shadow"})
        add("signal_outcome", {**base, "outcome": "filled", "mode": "shadow"})
        missed = {**base, "timestamp": "2026-09-22T14:05:00+00:00"}
        add("signal_decision", {**missed, "signal": True})
        add("signal_outcome", {**missed, "outcome": "selection_failure", "mode": "shadow",
                               "rejection_reasons": ["dte_out_of_range"]})
    result = sheet._signals(db, date(2026, 9, 22), {
        "dep": {"strategy": "market_impulse", "lane": "Shadow", "default_exit": "trend_continuation_balanced"}})
    assert (result["recorded"], result["captured"], result["missed"], result["pending"]) == (2, 1, 1, 0)
    assert result["rows"][0][6] == "DTE out of range (1)"


def test_positive_evaluations_and_independent_fills_expose_missing_outcomes(tmp_path):
    db = tmp_path / "facts.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, created_at TEXT, event_type TEXT, payload TEXT)")
        conn.execute("CREATE TABLE trade_sessions (trade_id TEXT, entry_order_id TEXT, status TEXT, "
                     "entry_timestamp TEXT, option_symbol TEXT, entry_price REAL, quantity INTEGER)")
        def add(kind, payload):
            conn.execute("INSERT INTO events(created_at,event_type,payload) VALUES(?,?,?)",
                         ("2026-09-22T14:00:00+00:00", kind, json.dumps(payload)))
        first = {"deployment_id": "live", "timestamp": "2026-09-22T14:00:00+00:00",
                 "direction": "long", "signal": True}
        add("signal_evaluation", first)
        add("signal_decision", first)
        add("signal_outcome", {**first, "signal_id": "live:2026-09-22T14:00:00+00:00:long",
                               "outcome": "filled", "trade_id": "broker-fill", "mode": "live"})
        add("signal_evaluation", {**first, "timestamp": "2026-09-22T14:01:00+00:00"})
        add("signal_evaluation", {**first, "deployment_id": "shadow", "timestamp": "2026-09-22T14:02:00+00:00"})
        add("signal_outcome", {**first, "timestamp": "2026-09-22T14:03:00+00:00",
                               "outcome": "filled", "trade_id": "unverified-fill", "mode": "live"})
        add("shadow_entry_modeled", {"trade_id": "modeled-fill"})
        for trade, order in (("broker-fill", "BROKER-1"), ("orphan-broker-fill", "BROKER-2"),
                             ("modeled-fill", "SHADOW_ENTRY"), ("assumed-shadow", "SHADOW_ENTRY")):
            conn.execute("INSERT INTO trade_sessions VALUES (?,?,?,?,?,?,?)",
                         (trade, order, "closed", "2026-09-22T14:00:00+00:00", "QQQ_OPTION", 2, 1))
    fills = sheet._actual_fills(db, date(2026, 9, 22))
    assert fills == {"ids": ["broker-fill", "modeled-fill", "orphan-broker-fill"],
                     "broker_confirmed": 2, "modeled": 1, "ambiguous_shadow": ["assumed-shadow"]}
    signals = sheet._signals(db, date(2026, 9, 22), {}, actual_fill_ids=set(fills["ids"]))
    assert (signals["recorded"], signals["evaluated"], signals["decided"],
            signals["captured"], signals["missing_outcome"],
            signals["unverified_fill_outcomes"]) == (4, 3, 1, 1, 2, 1)
    assert set(fills["ids"]) - set(signals["filled_trade_ids"]) == {"modeled-fill", "orphan-broker-fill"}


def test_active_plan_report_uses_strategy_key_and_effective_cartographer_policy(tmp_path):
    plan_path = tmp_path / "active_plan.json"
    plan_path.write_text(json.dumps({"deployments": [{
        "deployment_id": "mc-v1-example", "strategy": {"key": "manual_trigger", "params": {
            "cartographer_metadata": {"large_evidence": "must not appear in report"}}},
        "source": {"metadata": {"source_owner": "market_cartographer"}},
        "execution": {"shadow_only": True, "entry_execution_profile": None,
                      "dte_min": 3, "dte_max": 7, "dte_fallback_max": 21},
        "exit": {"exit_policy_id": "trend_continuation_balanced"},
    }]}))
    plan = sheet._plan(plan_path)
    assert plan["mc-v1-example"]["strategy"] == "manual_trigger"
    assert "large_evidence" not in str(plan)
    assert "legacy (implicit)" in sheet._entry_policy(plan)
    assert "fallback ceiling 21 DTE" in sheet._entry_policy(plan)
    plan["strategy-balanced"] = {"source_owner": "", "entry_profile": "balanced"}
    plan["strategy-patient"] = {"source_owner": "", "entry_profile": "patient"}
    assert sheet._strategy_entry_policy(plan) == "Strategy entry patience · 1 balanced · 1 patient"


def test_gap_case_cannot_become_clean_winner_and_censor_is_retained():
    result = sheet._exit_review([_case("clean", delta=45), _case("affected", gap=True, censor=True)],
                                date(2026, 9, 22))
    assert result["registered"] == 2 and result["clean"] == 1 and result["censored"] == 1
    assert result["rows"][0][3] == "Flash Reversal Fast Snap"
    assert result["rows"][0][4] == "1 / 2"
    assert "provisional" in result["rows"][0][7].lower()
    assert result["detail"][0][6] == "Historical censor; unchanged"


def test_registration_review_uses_filled_trade_ids_not_attempt_denominator(tmp_path):
    db = tmp_path / "edge.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE exit_edge_registration_attempts (trade_id TEXT, outcome TEXT, reason TEXT)")
        conn.execute("INSERT INTO exit_edge_registration_attempts VALUES ('failed','registration_persistence_failure','locked')")
    result = sheet._registration_review(db, ["clean", "failed", "silent"], [{"trade_id": "clean"}])
    assert (result["filled"], result["registered"], result["missing"]) == (3, 1, 2)
    assert any("locked" in reason for reason in result["detail"])
    assert any("no registration attempt" in reason for reason in result["detail"])


def test_cumulative_pairs_keep_frozen_versions_separate():
    first = _case("first", delta=45)
    second = _case("second", delta=-10)
    second["entry_timestamp"] = "2026-09-23T14:00:00+00:00"
    different = _case("new-policy", delta=100)
    different["experiment_spec_hash"] = "new-frozen-policy"
    result = sheet._cumulative_exit_review([first, second, different], date(2026, 9, 25))
    assert len(result["rows"]) == 2
    old = next(row for row in result["rows"] if row[9] == "frozen-policy")
    assert old[4:9] == [2, 2, 17.5, 0.175, 17.5]


def test_experiment_to_date_keeps_older_sessions_and_charges_extra_exit_leg():
    older = _case("older", delta=20)
    older["entry_timestamp"] = "2026-08-20T14:00:00+00:00"
    older["named_exit_outcomes"]["flash_reversal_fast_snap"]["legs"] = [
        {"quantity": .5}, {"quantity": .5},
    ]
    result = sheet._cumulative_exit_review([older], date(2026, 9, 25))
    row = result["rows"][0]
    assert row[4:9] == [1, 1, 19.0, 0.19, 20.0]
    assert row[10] == "2026-08-20–2026-09-25"


def test_experiment_to_date_keeps_deployments_separate_even_with_same_hash():
    first = _case("first", delta=20)
    second = _case("second", delta=200)
    second["deployment_id"] = "other-deployment"
    result = sheet._cumulative_exit_review([first, second], date(2026, 9, 25))
    assert len(result["rows"]) == 2
    assert {row[11] for row in result["rows"]} == {"dep", "other-deployment"}
    assert {row[6] for row in result["rows"]} == {20, 200}


def test_failed_publication_preserves_last_success(tmp_path, monkeypatch):
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    prior = {"status": "ok", "published_at": "2026-09-22T20:00:00+00:00"}
    (receipts / "last_success.json").write_text(json.dumps(prior))
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet-id")
    monkeypatch.setenv("GOOGLE_API_CREDENTIALS_PATH", "/tmp/unused.json")
    monkeypatch.setattr(sheet, "build_exit_comparisons_scorecard", lambda *a, **k: _scorecard())
    monkeypatch.setattr(sheet, "publish_exit_comparisons_scorecard", lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    result = sheet.publish_exit_comparisons_best_effort(db_path=tmp_path / "edge.db", receipt_dir=receipts)
    assert result["status"] == "failed" and result["last_success_at"] == prior["published_at"]
    assert json.loads((receipts / "last_success.json").read_text()) == prior
