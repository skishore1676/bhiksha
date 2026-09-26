"""A session review in the existing operator Sheet, backed by recorded evidence."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, date, datetime, time
import json
import os
from pathlib import Path
import sqlite3
from statistics import fmean
from typing import Any
from zoneinfo import ZoneInfo

from bhiksha.integrations.google_sheets import GoogleSheetTableClient
from bhiksha.market_data.trading_calendar import is_trading_day, previous_trading_day
from bhiksha.ops.exit_edge_lab import ProspectiveQuoteTapeRepository, analyze_prospective_repository

CENTRAL = ZoneInfo("America/Chicago")
TAB = "Exit_Comparisons"
DETAIL_LIMIT = 8
VISIBLE_COLUMNS = 8
SHEET_COLUMNS = 11
SIGNAL_HEADERS = ["Strategy", "Lane", "Signals", "Captured", "Missed", "Pending / unknown", "Main miss reason", "Default exit", "Deployments"]
EXIT_HEADERS = ["Strategy", "Lane / entry", "Default exit", "Observed leader", "Clean / registered", "Sessions", "Δ $ / trade", "Why / evidence", "Frozen evaluator", "Frozen hash", "Deployments"]
CUMULATIVE_HEADERS = ["Strategy", "Lane / entry", "Default exit", "Candidate", "Clean pairs", "Sessions", "Mean Δ $", "Worst Δ $", "Frozen evaluator", "Frozen hash", "Window"]
GAP_HEADERS = ["Trade", "Strategy", "Lane / entry", "Last good quote CT", "Resumed CT", "Reason", "Evidence state", "", "Full trade ID"]


def _ct(value: str | None) -> str:
    if not value:
        return ""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(CENTRAL).strftime("%Y-%m-%d %H:%M:%S")


def _label(value: str | None) -> str:
    return str(value or "Unknown").replace("_", " ").strip().title()


def _reason_label(value: str) -> str:
    lowered = value.lower()
    if "no contracts matched" in lowered:
        return "Contract selection"
    if lowered.startswith("lifecycle_blocked"):
        return "Existing position"
    return {"paper_limit_expired": "Limit not filled", "dte_out_of_range": "DTE out of range",
            "open_interest_below_min": "Open interest below minimum", "selection_failure": "Contract selection"}.get(lowered, _label(value))


def _day(value: date | str | None) -> date:
    if value:
        return date.fromisoformat(value) if isinstance(value, str) else value
    now = datetime.now(CENTRAL)
    if not is_trading_day(now.date()) or now.time() < time(8, 30):
        return previous_trading_day(now.date())
    return now.date()


def _plan(path: str | Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    try:
        deployments = json.loads(Path(path).read_text(encoding="utf-8"))["deployments"]
    except (OSError, ValueError, KeyError):
        return {}
    result = {}
    for item in deployments:
        source = item.get("source") or {}
        meta = source.get("metadata") or {}
        execution = item.get("execution") or {}
        strategy = item.get("strategy") or {}
        strategy_key = strategy.get("key") if isinstance(strategy, dict) else strategy
        result[item["deployment_id"]] = {
            "strategy": str(meta.get("strategy_class") or meta.get("strategy_family") or strategy_key or "unclassified"),
            "lane": "Shadow" if execution.get("shadow_only") else "Live",
            "default_exit": str((item.get("exit") or {}).get("exit_policy_id") or "unknown"),
            "entry_profile": str(execution.get("entry_execution_profile") or "legacy (implicit)"),
            "dte_min": execution.get("dte_min"), "dte_max": execution.get("dte_max"),
            "dte_fallback_max": execution.get("dte_fallback_max"),
            "source_owner": str(meta.get("source_owner") or ""),
        }
    return result


def _signals(db_path: str | Path | None, day: date, plan: dict[str, dict[str, str]]) -> dict[str, Any]:
    if db_path is None:
        raise ValueError("signal event database path unavailable")
    start = datetime.combine(day, time.min, CENTRAL).astimezone(UTC).isoformat()
    end = datetime.combine(date.fromordinal(day.toordinal() + 1), time.min, CENTRAL).astimezone(UTC).isoformat()
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        events = conn.execute(
            "SELECT created_at,event_type,payload FROM events WHERE created_at>=? AND created_at<? "
            "AND event_type IN ('signal_decision','signal_outcome') ORDER BY id", (start, end)
        ).fetchall()
    signals: dict[str, dict[str, Any]] = {}
    latest = ""
    for created_at, event_type, raw in events:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if event_type == "signal_decision" and payload.get("signal") is not True:
            continue
        timestamp = str(payload.get("timestamp") or created_at)
        try:
            if datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(CENTRAL).date() != day:
                continue
        except ValueError:
            continue
        deployment = str(payload.get("deployment_id") or "unknown")
        key = str(payload.get("signal_id") or f"{deployment}:{timestamp}:{payload.get('direction') or 'none'}")
        row = signals.setdefault(key, {"deployment": deployment, "timestamp": timestamp})
        latest = max(latest, timestamp)
        if event_type == "signal_outcome":
            # A pending event can be followed by a fill or terminal rejection.
            outcome = str(payload.get("outcome") or "unknown")
            previous = row.get("outcome")
            if previous != "filled" and (outcome == "filled" or previous in {None, "pending_execution", "unknown"} or outcome != "pending_execution"):
                row.update(outcome=outcome, reasons=payload.get("rejection_reasons") or [], mode=payload.get("mode"),
                           policy=(payload.get("evidence_identity") or {}).get("exit_policy_id"),
                           trade_id=payload.get("trade_id"))
        elif "decision" not in row:
            row["decision"] = True
            row["policy"] = (payload.get("evidence_identity") or {}).get("exit_policy_id")
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in signals.values():
        config = plan.get(row["deployment"], {})
        strategy = str(config.get("strategy") or ("manual_trigger" if row["deployment"].startswith("mc-v1-") else "unclassified"))
        lane = "Shadow" if row.get("mode") == "shadow" else "Live" if row.get("mode") == "live" else config.get("lane", "Unknown")
        row["default_exit"] = row.get("policy") or config.get("default_exit") or "unknown"
        groups[(strategy, lane)].append(row)
    rows = []
    for (strategy, lane), members in sorted(groups.items()):
        captured = sum(item.get("outcome") == "filled" for item in members)
        pending = sum(item.get("outcome") in {None, "pending_execution", "unknown"} for item in members)
        misses = [item for item in members if item.get("outcome") not in {None, "filled", "pending_execution", "unknown"}]
        reasons = Counter(
            str(reason).split(":", 1)[0] for item in misses
            for reason in (item.get("reasons") or [item.get("outcome")])
        )
        reason = ", ".join(f"{_reason_label(name)} ({count})" for name, count in reasons.most_common(2)) or "—"
        policies = Counter(str(item["default_exit"]) for item in members)
        default = ", ".join(_label(name) for name, _ in policies.most_common(2))
        rows.append([_label(strategy), lane, len(members), captured, len(misses), pending, reason, default,
                     ", ".join(sorted({item["deployment"] for item in members}))])
    return {"rows": rows, "recorded": len(signals), "captured": sum(row.get("outcome") == "filled" for row in signals.values()),
            "missed": sum(row.get("outcome") not in {None, "filled", "pending_execution", "unknown"} for row in signals.values()),
            "pending": sum(row.get("outcome") in {None, "pending_execution", "unknown"} for row in signals.values()),
            "latest": latest or None,
            "filled_trade_ids": sorted({str(row["trade_id"]) for row in signals.values()
                                        if row.get("outcome") == "filled" and row.get("trade_id")})}


def _entry_policy(plan: dict[str, dict[str, Any]]) -> str:
    cartographer = [row for row in plan.values() if row.get("source_owner") == "market_cartographer"]
    if not cartographer:
        return "No Cartographer rows in current plan"
    settings = {(row.get("entry_profile"), row.get("dte_min"), row.get("dte_max"),
                 row.get("dte_fallback_max")) for row in cartographer}
    if len(settings) != 1:
        return f"{len(cartographer)} Cartographer rows · mixed entry settings; inspect active plan"
    profile, low, high, fallback = settings.pop()
    return (f"Cartographer · {len(cartographer)} rows · {profile} patience · preferred {low}–{high} DTE · "
            f"fallback ceiling {fallback if fallback is not None else 'none'} DTE · "
            "control: Operator_Defaults_v1/profile__trend_continuation")


def _strategy_entry_policy(plan: dict[str, dict[str, Any]]) -> str:
    profiles = Counter(
        row.get("entry_profile") or "legacy (implicit)"
        for row in plan.values() if row.get("source_owner") != "market_cartographer"
    )
    if not profiles:
        return "No strategy rows in current plan"
    return "Strategy entry patience · " + " · ".join(
        f"{count} {_label(profile).lower()}" for profile, count in profiles.most_common()
    )


def _registration_review(
    db_path: str | Path, filled_trade_ids: list[str], cases: list[dict[str, Any]],
) -> dict[str, Any]:
    registered = {str(case["trade_id"]) for case in cases}
    missing = [trade_id for trade_id in filled_trade_ids if trade_id not in registered]
    reasons: dict[str, str] = {}
    if missing:
        with sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                "SELECT trade_id,outcome,reason FROM exit_edge_registration_attempts "
                f"WHERE trade_id IN ({','.join('?' for _ in missing)})", missing,
            ).fetchall()
        reasons = {trade_id: f"{outcome}: {reason or 'unspecified'}" for trade_id, outcome, reason in rows}
    return {"filled": len(filled_trade_ids), "registered": len(filled_trade_ids) - len(missing),
            "missing": len(missing), "detail": [f"{trade_id[:12]} {reasons.get(trade_id, 'no registration attempt')}"
                                               for trade_id in missing]}


def _cumulative_exit_review(cases: list[dict[str, Any]], day: date) -> dict[str, Any]:
    start = day
    for _ in range(9):
        start = previous_trading_day(start)
    groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        entry_day = datetime.fromisoformat(case["entry_timestamp"].replace("Z", "+00:00")).astimezone(CENTRAL).date()
        if not start <= entry_day <= day:
            continue
        dims, spec = case.get("cohort_dimensions") or {}, case.get("experiment_spec") or {}
        profiles = spec.get("named_profiles") or []
        if not profiles:
            continue
        key = (str(dims.get("strategy_class") or "unknown"), str(dims.get("runtime_mode") or "unknown"),
               str(dims.get("entry_fill_kind") or "unknown"), str(profiles[0]["policy_id"]),
               str(case.get("experiment_spec_hash") or "unknown"))
        groups[key].append(case)
    rows: list[list[Any]] = []
    for (strategy, mode, kind, primary, frozen_hash), members in sorted(groups.items()):
        candidates = sorted({name for case in members for name in
                             (case.get("clean_candidate_delta_pnl_usd") or {})})
        for candidate in candidates:
            paired = [(case, float(case["clean_candidate_delta_pnl_usd"][candidate]))
                      for case in members if candidate in (case.get("clean_candidate_delta_pnl_usd") or {})]
            deltas = [value for _, value in paired]
            sessions = len({datetime.fromisoformat(case["entry_timestamp"].replace("Z", "+00:00"))
                            .astimezone(CENTRAL).date() for case, _ in paired})
            rows.append([_label(strategy), f"{_label(mode)} / {'Broker confirmed' if kind == 'broker_confirmed' else 'Modeled entry'}",
                         _label(primary), _label(candidate), len(paired), sessions,
                         round(fmean(deltas), 2), round(min(deltas), 2),
                         str((members[0].get("experiment_spec") or {}).get("evaluator_version") or "unknown"),
                         frozen_hash, f"{start}–{day}"])
    return {"rows": rows, "start": start.isoformat(), "end": day.isoformat()}


def _exit_review(cases: list[dict[str, Any]], day: date) -> dict[str, Any]:
    groups: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    latest = ""
    for case in cases:
        if not case.get("entry_timestamp") or datetime.fromisoformat(case["entry_timestamp"].replace("Z", "+00:00")).astimezone(CENTRAL).date() != day:
            continue
        dims = case.get("cohort_dimensions") or {}
        spec = case.get("experiment_spec") or {}
        primary = (spec.get("named_profiles") or [{}])[0].get("policy_id") or case.get("management_exit") or "control"
        key = (str(dims.get("strategy_class") or "unclassified"), str(dims.get("runtime_mode") or "unknown"),
               str(dims.get("entry_fill_kind") or "unknown"), str(primary), str(case.get("experiment_spec_hash") or "unknown"))
        groups[key].append(case)
        latest = max(latest, case.get("latest_quote_at") or "")
    rows, detail = [], []
    clean_total = 0
    fully_clean_total = 0
    censored_total = 0
    collecting_total = 0
    unusable_total = 0
    reasons_total: Counter[str] = Counter()
    for (strategy, mode, entry_kind, primary, spec_hash), members in sorted(groups.items()):
        candidates: dict[str, list[tuple[dict[str, Any], float]]] = defaultdict(list)
        for case in members:
            for name, delta in (case.get("clean_candidate_delta_pnl_usd") or {}).items():
                candidates[name].append((case, float(delta)))
        best_name = max(candidates, key=lambda name: fmean(delta for _, delta in candidates[name])) if candidates else None
        paired = candidates[best_name] if best_name else []
        clean_total += len({case["trade_id"] for pairs in candidates.values() for case, _ in pairs})
        fully_clean_total += sum(case.get("status") == "paired" for case in members)
        affected = [case for case in members if case.get("observation_gaps") or str(case.get("insufficient_reason") or "").startswith("persisted_censor:")]
        censored_total += len(affected)
        for case in members:
            if case in affected or case.get("clean_candidate_delta_pnl_usd"):
                continue
            reason = str(case.get("insufficient_reason") or "")
            if reason.startswith("right_censored:") or reason == "quote_tape_too_short_for_next_tick_fill":
                collecting_total += 1
            else:
                unusable_total += 1
        for case in affected:
            for gap in case.get("observation_gaps") or [None]:
                reason = (gap or {}).get("reason") or str(case.get("insufficient_reason") or "unknown")
                reasons_total[reason] += 1
                detail.append([str(case.get("trade_id") or "")[:12], _label(strategy),
                               f"{_label(mode)} / {_label(entry_kind)}", _ct((gap or {}).get("last_received_at") or case.get("latest_quote_at")),
                               _ct((gap or {}).get("first_received_at")), _label(reason),
                               "Historical censor; unchanged" if str(case.get("insufficient_reason") or "").startswith("persisted_censor:")
                               else "Diagnostic continuation; assumes survival through gap", "", case.get("trade_id") or ""])
        lane = f"{_label(mode)} / {'Broker confirmed' if entry_kind == 'broker_confirmed' else 'Modeled entry'}"
        sessions = len({_ct(case.get("entry_timestamp"))[:10] for case, _ in paired})
        if not paired:
            leader, delta, why = "No clean comparison", "", "Open or censored exits; no winner established."
        else:
            mean_delta = round(fmean(delta for _, delta in paired), 2)
            # Do not rank different candidates on different trade sets.
            comparable = all({case["trade_id"] for case, _ in pairs} == {case["trade_id"] for case, _ in paired}
                             for pairs in candidates.values())
            leader = _label(best_name) if comparable and mean_delta > 0 else _label(primary) if comparable else "No common winner"
            delta = mean_delta if comparable else ""
            example = paired[0][0]
            outcomes = example.get("named_exit_outcomes") or {}
            p = outcomes.get(primary) or {}
            c = outcomes.get(best_name) or {}
            mechanics = ""
            if p and c and p.get("realized_pnl_usd") is not None and c.get("realized_pnl_usd") is not None:
                mechanics = (f" Primary {_label(p.get('exit_rule'))} at {_ct(p.get('exit_timestamp'))[-8:-3]} "
                             f"(${p.get('realized_pnl_usd'):+,.0f}); candidate {_label(c.get('exit_rule'))} "
                             f"at {_ct(c.get('exit_timestamp'))[-8:-3]} (${c.get('realized_pnl_usd'):+,.0f}).")
            why = (f"{len(paired)} same-trade pair(s); {sessions} session(s). "
                   f"{'One-session observation; provisional.' if sessions < 2 else 'Observed gross result.'}{mechanics}") if comparable else "Candidate trade sets differ; no common ranking."
        version = str((members[0].get("experiment_spec") or {}).get("evaluator_version") or "unknown")
        rows.append([_label(strategy), lane, _label(primary), leader, f"{len(paired)} / {len(members)}",
                     sessions, delta, why, version, spec_hash,
                     ", ".join(sorted({case["deployment_id"] for case in members}))])
    return {"rows": rows, "detail": detail[:DETAIL_LIMIT], "registered": sum(len(m) for m in groups.values()),
            "clean": clean_total, "fully_clean": fully_clean_total,
            "censored": censored_total, "collecting": collecting_total,
            "unusable": unusable_total, "latest": latest or None,
            "reasons": ", ".join(f"{_label(k)} ({v})" for k, v in reasons_total.most_common(2)) or "None recorded"}


def build_exit_comparisons_scorecard(
    db_path: str | Path, *, signal_db_path: str | Path | None = None,
    active_plan_path: str | Path | None = None, trading_date: date | str | None = None,
) -> dict[str, Any]:
    day = _day(trading_date)
    plan = _plan(active_plan_path)
    signals = _signals(signal_db_path, day, plan)
    repository = ProspectiveQuoteTapeRepository(db_path, read_only=True, write_timeout_seconds=0.25)
    cases = analyze_prospective_repository(repository)["cases"]
    exits = _exit_review(cases, day)
    return {"schema": "bhiksha.exit_comparisons_sheet.v2", "trading_date": day.isoformat(),
            "generated_at": datetime.now(UTC).isoformat(), "signals": signals, "exits": exits,
            "entry_policy": _entry_policy(plan),
            "strategy_entry_policy": _strategy_entry_policy(plan),
            "registration": _registration_review(db_path, signals["filled_trade_ids"], cases),
            "cumulative": _cumulative_exit_review(cases, day)}


def _cell(value: Any) -> dict[str, Any]:
    if isinstance(value, str) and value.startswith("="):
        return {"userEnteredValue": {"formulaValue": value}}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {"userEnteredValue": {"numberValue": value}}
    return {"userEnteredValue": {"stringValue": str(value or "")}}


def publish_exit_comparisons_scorecard(
    scorecard: dict[str, Any], *, spreadsheet_id: str, credentials_path: str | Path,
    service: Any | None = None,
) -> dict[str, Any]:
    client = GoogleSheetTableClient(spreadsheet_id, TAB, Path(credentials_path), service=service)
    api = client.service.spreadsheets()
    metadata = api.get(spreadsheetId=client.spreadsheet_id, fields="sheets.properties").execute()
    match = next((item["properties"] for item in metadata.get("sheets", []) if item.get("properties", {}).get("title") == TAB), None)
    if match is None:
        created = api.batchUpdate(spreadsheetId=client.spreadsheet_id, body={"requests": [{"addSheet": {
            "properties": {"title": TAB, "gridProperties": {"rowCount": 100, "columnCount": SHEET_COLUMNS}}}}]}).execute()
        match = created["replies"][0]["addSheet"]["properties"]
    sheet_id = int(match["sheetId"])
    signals, exits = scorecard["signals"], scorecard["exits"]
    registration = scorecard.get("registration") or {}
    cumulative = scorecard.get("cumulative") or {}
    values: list[list[Any]] = [
        [f"Bhiksha session review · {scorecard['trading_date']}"],
        ["Recorded signals", signals["recorded"], "Captured", signals["captured"], "Missed", signals["missed"], "Pending / unknown", signals["pending"]],
        ["Published CT", _ct(scorecard["generated_at"]), "Age (hours)", '=ROUND((NOW()-DATEVALUE(LEFT(B3,10))-TIMEVALUE(MID(B3,12,8)))*24,1)',
         "Last signal CT", _ct(signals["latest"]), "Last exit quote CT", _ct(exits["latest"])],
        [f"Exit evidence: {exits['registered']} registered · {exits.get('fully_clean', 0)} fully complete · "
         f"{exits['clean']} with a clean pair · {exits['collecting']} collecting · "
         f"{exits['censored']} gap/censored · {exits['unusable']} other unusable; pair and gap counts may overlap"],
        [f"Fill → comparison: {registration.get('filled', signals['captured'])} filled · {registration.get('registered', exits['registered'])} registered · "
         f"{registration.get('missing', 0)} missing | {'; '.join(registration.get('detail') or []) or 'No missing cohorts'}"],
        ["One-session leaders are provisional. Recorded signals only; downtime opportunities unknown. Shadow captures are modeled."],
        [scorecard.get("entry_policy") or "Entry patience and DTE: active-plan readback unavailable"],
        [scorecard.get("strategy_entry_policy") or "Strategy entry patience: active-plan readback unavailable"],
        [], ["SIGNAL CAPTURE"], SIGNAL_HEADERS, *signals["rows"], [],
        ["EXIT CHOICES · same-trade clean pairs"], EXIT_HEADERS, *exits["rows"], [],
        [f"ROLLING 10 SESSIONS · {cumulative.get('start', '')} to {cumulative.get('end', '')} · gross modeled matched pairs"],
        CUMULATIVE_HEADERS, *(cumulative.get("rows") or []), [],
        [f"EVIDENCE GAPS · {exits['reasons']}"], GAP_HEADERS, *exits["detail"], [],
        ["Method: gross modeled natural-bid exits, before fees and extra slippage. No automatic policy promotion; frozen version/hash are in hidden columns."],
    ]
    sections = [i for i, row in enumerate(values) if row and isinstance(row[0], str) and
                (row[0] == "SIGNAL CAPTURE" or row[0].startswith("EXIT CHOICES")
                 or row[0].startswith("ROLLING 10 SESSIONS") or row[0].startswith("EVIDENCE GAPS"))]
    headers = [i + 1 for i in sections]
    height = len(values)
    old_rows = int(match.get("gridProperties", {}).get("rowCount", 100))
    old_cols = int(match.get("gridProperties", {}).get("columnCount", SHEET_COLUMNS))
    requests: list[dict[str, Any]] = []
    if height > old_rows or SHEET_COLUMNS > old_cols:
        requests.append({"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {
            "rowCount": max(height, old_rows), "columnCount": max(SHEET_COLUMNS, old_cols)}},
            "fields": "gridProperties.rowCount,gridProperties.columnCount"}})
    requests.append({"updateCells": {"range": {"sheetId": sheet_id, "startRowIndex": 0,
        "endRowIndex": max(height, old_rows), "startColumnIndex": 0, "endColumnIndex": max(SHEET_COLUMNS, old_cols)},
        "rows": [{"values": [_cell(v) for v in (row + [""] * (max(SHEET_COLUMNS, old_cols) - len(row)))]} for row in values],
        "fields": "userEnteredValue,userEnteredFormat"}})
    dark = {"backgroundColor": {"red": .12, "green": .20, "blue": .31},
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}}
    pale = {"backgroundColor": {"red": .90, "green": .94, "blue": .98}, "textFormat": {"bold": True}}
    for index in [0, *sections]:
        requests.append({"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": index, "endRowIndex": index + 1,
            "startColumnIndex": 0, "endColumnIndex": VISIBLE_COLUMNS}, "cell": {"userEnteredFormat": dark}, "fields": "userEnteredFormat"}})
    for index in headers:
        requests.append({"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": index, "endRowIndex": index + 1,
            "startColumnIndex": 0, "endColumnIndex": VISIBLE_COLUMNS}, "cell": {"userEnteredFormat": pale}, "fields": "userEnteredFormat"}})
    requests.append({"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 3,
        "startColumnIndex": 0, "endColumnIndex": VISIBLE_COLUMNS}, "cell": {"userEnteredFormat": pale}, "fields": "userEnteredFormat"}})
    for section, following, column in ((sections[0], sections[1], 6), (sections[1], sections[2], 7),
                                       (sections[2], sections[3], 7)):
        if section + 2 < following - 1:
            requests.append({"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": section + 2,
                "endRowIndex": following - 1, "startColumnIndex": column, "endColumnIndex": column + 1},
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}}, "fields": "userEnteredFormat.wrapStrategy"}})
    requests.extend([
        {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 2, "endRowIndex": 3, "startColumnIndex": 3, "endColumnIndex": 4},
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0.0"}}},
                        "fields": "userEnteredFormat.numberFormat"}},
        {"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {
            "frozenRowCount": 3, "frozenColumnCount": 1, "hideGridlines": True}},
            "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount,gridProperties.hideGridlines"}},
        {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                                       "properties": {"pixelSize": 220}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 6},
                                       "properties": {"pixelSize": 145}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 6, "endIndex": 7},
                                       "properties": {"pixelSize": 220}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 7, "endIndex": 8},
                                       "properties": {"pixelSize": 420}, "fields": "pixelSize"}},
        {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 8, "endIndex": max(SHEET_COLUMNS, old_cols)},
                                       "properties": {"hiddenByUser": True}, "fields": "hiddenByUser"}},
        {"autoResizeDimensions": {"dimensions": {"sheetId": sheet_id, "dimension": "ROWS", "startIndex": 7,
                                                     "endIndex": height}}},
    ])
    api.batchUpdate(spreadsheetId=client.spreadsheet_id, body={"requests": requests}).execute()
    return {"status": "ok", "sheet_id": sheet_id, "spreadsheet_id": client.spreadsheet_id,
            "published_at": scorecard["generated_at"], "trading_date": scorecard["trading_date"],
            "recorded_signals": signals["recorded"], "captured_signals": signals["captured"],
            "registered_cohorts": exits["registered"], "clean_trades": exits["clean"]}


def publish_exit_comparisons_best_effort(
    *, db_path: str | Path, receipt_dir: str | Path, signal_db_path: str | Path | None = None,
    active_plan_path: str | Path | None = None, trading_date: date | str | None = None,
) -> dict[str, Any]:
    """Publishing failure leaves the last successful Sheet and trading untouched."""
    target = Path(receipt_dir)
    target.mkdir(parents=True, exist_ok=True)
    try:
        sheet_id = os.environ["GOOGLE_SHEET_ID"]
        credentials = os.environ["GOOGLE_API_CREDENTIALS_PATH"]
        scorecard = build_exit_comparisons_scorecard(db_path, signal_db_path=signal_db_path,
            active_plan_path=active_plan_path, trading_date=trading_date)
        receipt = publish_exit_comparisons_scorecard(scorecard, spreadsheet_id=sheet_id, credentials_path=credentials)
        _atomic_json(target / "last_success.json", receipt)
    except Exception as exc:
        previous = _read_json(target / "last_success.json")
        receipt = {"status": "failed", "error_type": type(exc).__name__,
                   "last_success_at": previous.get("published_at"), "attempted_at": datetime.now(UTC).isoformat()}
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
