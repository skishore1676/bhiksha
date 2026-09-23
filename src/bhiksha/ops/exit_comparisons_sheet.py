"""Publish read-only Exit Edge evidence into the existing operator workbook."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
import json
import os
from pathlib import Path
from statistics import fmean
from typing import Any
from zoneinfo import ZoneInfo

from bhiksha.integrations.google_sheets import GoogleSheetTableClient
from bhiksha.ops.exit_edge_lab import (
    ProspectiveQuoteTapeRepository,
    analyze_prospective_repository,
)

CENTRAL = ZoneInfo("America/Chicago")
TAB = "Exit_Comparisons"
DETAIL_LIMIT = 12
HEADERS = [
    "Mode", "Strategy class", "Deployment", "Entry", "Primary exit", "Candidate exit",
    "Registered", "Clean paired", "Still collecting", "Gap affected", "Other unusable",
    "Sessions", "Paired sessions", "Mean candidate − primary $", "Cost basis",
    "Latest quote CT", "Leading gap reasons", "Policy version", "Frozen spec hash",
]
DETAIL_HEADERS = ["Trade", "Mode", "Strategy class", "Last trustworthy quote CT",
                  "First resumed quote CT", "Gap or censor reason", "Evidence state"]


def _ct(value: str | None) -> str:
    if not value:
        return ""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(CENTRAL).strftime("%Y-%m-%d %H:%M:%S")


def build_exit_comparisons_scorecard(db_path: str | Path) -> dict[str, Any]:
    repository = ProspectiveQuoteTapeRepository(db_path, read_only=True, write_timeout_seconds=0.25)
    report = analyze_prospective_repository(repository)
    groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for case in report["cases"]:
        dims = case.get("cohort_dimensions") or {}
        key = (str(dims.get("runtime_mode") or "unknown"),
               str(dims.get("strategy_class") or "unclassified"),
               str(case["deployment_id"]),
               str(dims.get("entry_fill_kind") or "unknown"),
               str(case["experiment_spec_hash"]))
        groups[key].append(case)

    rows: list[list[Any]] = []
    detail: list[dict[str, Any]] = []
    for (mode, strategy, deployment, entry_kind, spec_hash), cases in sorted(groups.items()):
        experiment = cases[0]["experiment_spec"]
        if "named_profiles" in experiment:
            primary = experiment["named_profiles"][0]["policy_id"]
            candidates = [policy["policy_id"] for policy in experiment["named_profiles"][1:]]
        else:
            primary = "control"
            candidates = [arm["candidate_id"] for arm in experiment["risk_envelope"]["arms"]
                          if arm["candidate_id"] != "control"] + ["legacy"]
        all_sessions = {
            _ct(case.get("entry_timestamp"))[:10]
            for case in cases if case.get("entry_timestamp")
        }
        latest = max((case.get("latest_quote_at") or "" for case in cases), default="")
        for candidate in candidates:
            paired = [case for case in cases if candidate in (case.get("clean_candidate_delta_pnl_usd") or {})]
            gap_affected = [case for case in cases if case.get("observation_gaps")
                            and case not in paired and not str(case.get("insufficient_reason") or "").startswith("persisted_censor:")]
            still = [case for case in cases if case not in paired and case not in gap_affected
                     and case.get("status") == "insufficient_data"
                     and (str(case.get("insufficient_reason") or "").startswith("right_censored:")
                          or case.get("insufficient_reason") == "quote_tape_too_short_for_next_tick_fill")]
            other = len(cases) - len(paired) - len(gap_affected) - len(still)
            reasons = Counter(
                gap["reason"] for case in gap_affected for gap in case.get("observation_gaps") or []
            )
            reasons.update(
                str(case.get("insufficient_reason") or "unknown")
                for case in cases if case not in paired and case not in gap_affected and case not in still
            )
            clean_deltas = [float(case["clean_candidate_delta_pnl_usd"][candidate]) for case in paired]
            paired_sessions = {_ct(case.get("entry_timestamp"))[:10] for case in paired}
            rows.append([
                mode, strategy, deployment, entry_kind, primary, candidate,
                len(cases), len(paired), len(still), len(gap_affected), other,
                len(all_sessions), len(paired_sessions),
                round(fmean(clean_deltas), 2) if clean_deltas else "",
                "Gross modeled natural-bid; before fees and extra slippage",
                _ct(latest), ", ".join(f"{name} ({count})" for name, count in reasons.most_common(2)),
                str(experiment.get("evaluator_version") or "unknown"), spec_hash,
            ])
        for case in cases:
            historical_censor = str(case.get("insufficient_reason") or "").startswith("persisted_censor:")
            for gap in case.get("observation_gaps") or []:
                detail.append({"entry_timestamp": case.get("entry_timestamp"),
                               "trade_id": case["trade_id"], "mode": mode, "strategy": strategy,
                               "last": _ct(gap.get("last_received_at")),
                               "first": _ct(gap.get("first_received_at")),
                               "reason": gap["reason"],
                               "state": ("historical censor; unchanged" if historical_censor
                                         else "diagnostic continuation—assumes survival through the gap")})
            if not case.get("observation_gaps") and historical_censor:
                detail.append({"entry_timestamp": case.get("entry_timestamp"),
                               "trade_id": case["trade_id"], "mode": mode, "strategy": strategy,
                               "last": _ct(case.get("latest_quote_at")), "first": "",
                               "reason": case["insufficient_reason"], "state": "historical censor; unchanged"})
    detail.sort(key=lambda item: str(item.get("entry_timestamp") or ""), reverse=True)
    return {"schema": "bhiksha.exit_comparisons_sheet.v1",
            "generated_at": datetime.now(UTC).isoformat(),
            "latest_quote_at": max((case.get("latest_quote_at") or "" for case in report["cases"]), default="") or None,
            "registered_cohorts": len(report["cases"]),
            "headers": HEADERS, "rows": rows,
            "detail_headers": DETAIL_HEADERS, "detail": detail[:DETAIL_LIMIT]}


def _cell(value: Any) -> dict[str, Any]:
    if isinstance(value, str) and value.startswith("="):
        return {"userEnteredValue": {"formulaValue": value}}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {"userEnteredValue": {"numberValue": value}}
    return {"userEnteredValue": {"stringValue": str(value or "")}}


def _row(values: list[Any]) -> dict[str, Any]:
    return {"values": [_cell(value) for value in values]}


def publish_exit_comparisons_scorecard(
    scorecard: dict[str, Any], *, spreadsheet_id: str, credentials_path: str | Path,
    service: Any | None = None,
) -> dict[str, Any]:
    client = GoogleSheetTableClient(spreadsheet_id, TAB, Path(credentials_path), service=service)
    api = client.service.spreadsheets()
    metadata = api.get(spreadsheetId=client.spreadsheet_id, fields="sheets.properties").execute()
    match = next((item["properties"] for item in metadata.get("sheets", [])
                  if item.get("properties", {}).get("title") == TAB), None)
    if match is None:
        created = api.batchUpdate(spreadsheetId=client.spreadsheet_id, body={"requests": [{
            "addSheet": {"properties": {"title": TAB, "gridProperties": {
                "rowCount": 100, "columnCount": len(HEADERS), "frozenRowCount": 6,
            }}}
        }]}).execute()
        match = created["replies"][0]["addSheet"]["properties"]
    sheet_id = int(match["sheetId"])
    detail_rows = [
        [item["trade_id"], item["mode"], item["strategy"], item["last"], item["first"],
         item["reason"], item["state"]]
        for item in scorecard["detail"]
    ]
    values: list[list[Any]] = [
        ["Exit comparisons", "Published CT", _ct(scorecard["generated_at"])],
        ["Latest observed quote CT", _ct(scorecard.get("latest_quote_at"))],
        ["Publication age (hours)", '=IF(C1="","",ROUND((NOW()-DATEVALUE(LEFT(C1,10))-TIMEVALUE(MID(C1,12,8)))*24,1))'],
        ["Clean paired results use identical trades. Diagnostic continuation assumes survival through a gap."],
        ["Cost basis: gross modeled natural-bid fills; fees, size and additional slippage excluded."],
        [],
        scorecard["headers"],
        *scorecard["rows"],
        [],
        ["Affected trades and gaps (latest 12)"],
        scorecard["detail_headers"],
        *detail_rows,
    ]
    width = len(HEADERS)
    height = len(values)
    old_rows = int(match.get("gridProperties", {}).get("rowCount", 100))
    old_cols = int(match.get("gridProperties", {}).get("columnCount", width))
    requests: list[dict[str, Any]] = []
    if height > old_rows or width > old_cols:
        requests.append({"updateSheetProperties": {"properties": {"sheetId": sheet_id,
            "gridProperties": {"rowCount": max(height, old_rows), "columnCount": max(width, old_cols)}},
            "fields": "gridProperties.rowCount,gridProperties.columnCount"}})
    requests.append({"updateCells": {"range": {"sheetId": sheet_id,
        "startRowIndex": 0, "endRowIndex": max(height, old_rows),
        "startColumnIndex": 0, "endColumnIndex": width},
        "rows": [_row(row + [""] * (width - len(row))) for row in values],
        "fields": "userEnteredValue"}})
    requests.extend([
        {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 6,
            "endRowIndex": 7, "startColumnIndex": 0, "endColumnIndex": width},
            "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.14, "green": 0.23, "blue": 0.34},
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                "wrapStrategy": "WRAP"}}, "fields": "userEnteredFormat"}},
        {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 9 + len(scorecard["rows"]),
            "endRowIndex": 10 + len(scorecard["rows"]), "startColumnIndex": 0,
            "endColumnIndex": len(DETAIL_HEADERS)},
            "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.91, "green": 0.94, "blue": 0.97},
                "textFormat": {"bold": True}}}, "fields": "userEnteredFormat"}},
        {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 2,
            "endRowIndex": 3, "startColumnIndex": 1, "endColumnIndex": 2},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0.0"}}},
            "fields": "userEnteredFormat.numberFormat"}},
        {"updateSheetProperties": {"properties": {"sheetId": sheet_id,
            "gridProperties": {"frozenRowCount": 7, "hideGridlines": True}},
            "fields": "gridProperties.frozenRowCount,gridProperties.hideGridlines"}},
        {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
            "startIndex": 0, "endIndex": width - 1},
            "properties": {"pixelSize": 150}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
            "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 210}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
            "startIndex": 2, "endIndex": 3},
            "properties": {"pixelSize": 320}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
            "startIndex": 4, "endIndex": 6},
            "properties": {"pixelSize": 255}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
            "startIndex": 14, "endIndex": 15},
            "properties": {"pixelSize": 345}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
            "startIndex": 16, "endIndex": 17},
            "properties": {"pixelSize": 245}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
            "startIndex": width - 1, "endIndex": width},
            "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}},
    ])
    api.batchUpdate(spreadsheetId=client.spreadsheet_id, body={"requests": requests}).execute()
    return {"status": "ok", "sheet_id": sheet_id, "spreadsheet_id": client.spreadsheet_id,
            "published_at": scorecard["generated_at"], "registered_cohorts": scorecard["registered_cohorts"],
            "comparison_rows": len(scorecard["rows"]), "detail_rows": len(detail_rows)}


def publish_exit_comparisons_best_effort(
    *, db_path: str | Path, receipt_dir: str | Path,
) -> dict[str, Any]:
    """A Sheet failure never changes report or trading success."""
    target = Path(receipt_dir)
    target.mkdir(parents=True, exist_ok=True)
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    credentials = os.getenv("GOOGLE_API_CREDENTIALS_PATH")
    try:
        if not sheet_id or not credentials:
            raise ValueError("operator Sheet id or credentials path unavailable")
        scorecard = build_exit_comparisons_scorecard(db_path)
        receipt = publish_exit_comparisons_scorecard(
            scorecard, spreadsheet_id=sheet_id, credentials_path=credentials,
        )
        _atomic_json(target / "last_success.json", receipt)
    except Exception as exc:
        previous = _read_json(target / "last_success.json")
        receipt = {"status": "failed", "error_type": type(exc).__name__,
                   "last_success_at": previous.get("published_at"),
                   "attempted_at": datetime.now(UTC).isoformat()}
    _atomic_json(target / "last_attempt.json", receipt)
    return receipt


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
