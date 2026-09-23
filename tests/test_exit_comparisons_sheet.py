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
        "clean_candidate_delta_pnl_usd": {"flash_reversal_fast_snap": delta} if delta is not None else {},
        "named_exit_outcomes": {"trend_continuation_balanced": {"exit_rule": "initial_stop",
                                "exit_timestamp": "2026-09-22T14:05:00+00:00", "realized_pnl_usd": -375},
                                "flash_reversal_fast_snap": {"exit_rule": "initial_stop",
                                "exit_timestamp": "2026-09-22T14:04:00+00:00", "realized_pnl_usd": -330}},
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
    assert rows[6]["values"][0]["userEnteredValue"]["stringValue"] == "SIGNAL CAPTURE"
    assert rows[8]["values"][2]["userEnteredValue"]["numberValue"] == 2
    assert any(row["values"][0]["userEnteredValue"].get("stringValue", "").startswith("EXIT CHOICES") for row in rows)
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


def test_gap_case_cannot_become_clean_winner_and_censor_is_retained():
    result = sheet._exit_review([_case("clean", delta=45), _case("affected", gap=True, censor=True)],
                                date(2026, 9, 22))
    assert result["registered"] == 2 and result["clean"] == 1 and result["censored"] == 1
    assert result["rows"][0][3] == "Flash Reversal Fast Snap"
    assert result["rows"][0][4] == "1 / 2"
    assert "provisional" in result["rows"][0][7].lower()
    assert result["detail"][0][6] == "Historical censor; unchanged"


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
