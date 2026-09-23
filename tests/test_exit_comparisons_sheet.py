from __future__ import annotations

import json

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


def _scorecard():
    return {"generated_at": "2026-09-23T15:00:00+00:00",
            "latest_quote_at": "2026-09-23T14:59:45+00:00",
            "registered_cohorts": 2, "headers": sheet.HEADERS,
            "rows": [["shadow", "trend", "dep", "modeled_ask_touch", "base", "patient",
                      2, 1, 0, 1, 0, 1, 1, 10.0, "gross", "2026-09-23 09:59:45",
                      "observation_interval_exceeded (1)", "evaluator-v1", "hash"]],
            "detail": [{"trade_id": "T1", "mode": "shadow", "strategy": "trend",
                        "last": "2026-09-23 09:54:45", "first": "2026-09-23 09:59:45",
                        "reason": "observation_interval_exceeded", "state": "gap-affected diagnostic"}],
            "detail_headers": sheet.DETAIL_HEADERS}


def test_scorecard_sheet_write_is_scoped_and_keeps_live_age_formula():
    service = _Service()
    receipt = sheet.publish_exit_comparisons_scorecard(
        _scorecard(), spreadsheet_id="sheet-id", credentials_path="/tmp/unused.json", service=service,
    )
    assert receipt["status"] == "ok" and receipt["registered_cohorts"] == 2
    requests = service.api.writes[0]["requests"]
    assert all("deleteSheet" not in request and "clearBasicFilter" not in request for request in requests)
    block = next(request["updateCells"] for request in requests if "updateCells" in request)
    assert block["range"]["sheetId"] == 42
    assert block["rows"][6]["values"][0]["userEnteredValue"]["stringValue"] == "Mode"
    assert block["rows"][7]["values"][7]["userEnteredValue"]["numberValue"] == 1
    assert "formulaValue" in block["rows"][2]["values"][1]["userEnteredValue"]


def test_failed_sheet_publication_retains_last_success_receipt(tmp_path, monkeypatch):
    target = tmp_path / "receipts"
    target.mkdir()
    prior = {"status": "ok", "published_at": "2026-09-22T20:00:00+00:00"}
    (target / "last_success.json").write_text(json.dumps(prior))
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet-id")
    monkeypatch.setenv("GOOGLE_API_CREDENTIALS_PATH", "/tmp/unused.json")
    monkeypatch.setattr(sheet, "build_exit_comparisons_scorecard", lambda path: _scorecard())

    def fail(*args, **kwargs):
        raise RuntimeError("simulated publication failure")

    monkeypatch.setattr(sheet, "publish_exit_comparisons_scorecard", fail)
    result = sheet.publish_exit_comparisons_best_effort(db_path=tmp_path / "edge.db", receipt_dir=target)
    assert result["status"] == "failed"
    assert result["last_success_at"] == prior["published_at"]
    assert json.loads((target / "last_success.json").read_text()) == prior


def test_scorecard_keeps_entry_provenance_and_pre_gap_pairs_separate(tmp_path, monkeypatch):
    from bhiksha.ops.exit_edge_lab import ProspectiveQuoteTapeRepository

    db = tmp_path / "edge.db"
    ProspectiveQuoteTapeRepository(db).initialize()
    policy = {"named_profiles": [{"policy_id": "primary"}, {"policy_id": "patient"}],
              "evaluator_version": "named-exits.v2"}
    def case(trade, kind, *, delta=None, gaps=None, reason=None):
        return {"trade_id": trade, "deployment_id": "dep", "cohort_dimensions": {
            "runtime_mode": "shadow", "strategy_class": "trend", "entry_fill_kind": kind},
            "experiment_spec_hash": "frozen-policy", "experiment_spec": policy,
            "entry_timestamp": "2026-09-23T14:00:00+00:00",
            "latest_quote_at": "2026-09-23T14:05:00+00:00",
            "clean_candidate_delta_pnl_usd": {"patient": delta} if delta is not None else {},
            "observation_gaps": gaps or [], "insufficient_reason": reason,
            "status": "gap_affected" if gaps else "insufficient_data"}
    gap = {"last_received_at": "2026-09-23T14:02:00+00:00",
           "first_received_at": "2026-09-23T14:07:00+00:00",
           "reason": "observation_interval_exceeded"}
    source = {"cases": [case("broker-pre-gap", "broker_confirmed", delta=12.0, gaps=[gap]),
                        case("broker-affected", "broker_confirmed", gaps=[gap]),
                        case("modeled", "modeled_ask_touch", delta=-4.0),
                        case("historical-censor", "broker_confirmed", gaps=[gap],
                             reason="persisted_censor:restart_gap_unobserved_quotes")]}
    monkeypatch.setattr(sheet, "analyze_prospective_repository", lambda repo: source)
    result = sheet.build_exit_comparisons_scorecard(db)
    broker = next(row for row in result["rows"] if row[3] == "broker_confirmed")
    modeled = next(row for row in result["rows"] if row[3] == "modeled_ask_touch")
    assert broker[6:11] == [3, 1, 0, 1, 1]
    assert broker[13] == 12.0
    assert modeled[6:11] == [1, 1, 0, 0, 0]
    assert modeled[13] == -4.0
