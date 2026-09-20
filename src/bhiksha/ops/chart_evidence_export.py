"""Export a bounded, source-owned Bhiksha chart-evidence packet.

This module deliberately uses only SQLite's read-only URI and a snapshot
transaction.  It does not construct a runtime, load credentials, contact a
broker, or publish to another application.  The resulting JSON is an ordinary
Chart Workbench evidence-protocol v2 packet with a small publication envelope
that lets a consumer select the newest revision for a stable source/day stream.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, date as Date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


CENTRAL = ZoneInfo("America/Chicago")
DEFAULT_OUTPUT_DIR = Path("artifacts/chart-workbench/publications")
_SAFE_ID = re.compile(r"[^a-z0-9]+")
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_EVENT_LIMIT = 25_000
_CRITICAL_EVENT_TYPES = (
    "startup_config", "signal_decision", "trade_plan", "shadow_entry_assumed",
    "entry_fill_check", "entry_terminal_partial_fill_recovered", "exit_fill_enriched",
    "shadow_exit_assumed", "exit_submission", "lifecycle_transition", "lifecycle_entry_blocked",
    "runtime_issue", "entry_reconcile_released", "entry_reprice_blocked", "session_coverage",
    "session_complete",
)


@dataclass(frozen=True, slots=True)
class ExportResult:
    packet: dict[str, Any]
    path: Path
    reused: bool


def export_chart_evidence(
    db_path: str | Path,
    *,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    trading_date: str | Date | None = None,
    revision: int = 1,
    event_limit: int = _EVENT_LIMIT,
) -> ExportResult:
    """Read one bounded source snapshot and immutably write its packet."""

    if revision < 1:
        raise ValueError("revision must be a positive integer")
    day = _coerce_day(trading_date)
    snapshot = read_source_snapshot(db_path, trading_date=day, event_limit=event_limit)
    packet = build_chart_evidence_packet(snapshot, revision=revision)
    target, reused = write_immutable_packet(packet, output_dir)
    return ExportResult(packet=packet, path=target, reused=reused)


def read_source_snapshot(
    db_path: str | Path, *, trading_date: Date, event_limit: int = _EVENT_LIMIT
) -> dict[str, Any]:
    """Return a sanitized, bounded snapshot without opening a writable DB handle."""

    if event_limit < 1 or event_limit > 50_000:
        raise ValueError("event_limit must be between 1 and 50000")
    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Bhiksha SQLite source does not exist: {path}")
    uri = f"{path.as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=8)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 8000")
        connection.execute("BEGIN")
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        events, events_truncated = _read_events(
            connection, tables, trading_date, event_limit
        )
        deployments, startup_event_id = _deployments_from_startup(events)
        trades = _read_trades(connection, tables, trading_date)
        # A deterministic snapshot digest binds only the allowlisted projection,
        # never a database path, broker payload, account identifier, or secret.
        body = {
            "date": trading_date.isoformat(),
            "deployments": deployments,
            "events": events,
            "trades": trades,
            "events_truncated": events_truncated,
            "startup_event_id": startup_event_id,
        }
        return {**body, "snapshot_sha256": _sha(body)}
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()


def build_chart_evidence_packet(snapshot: dict[str, Any], *, revision: int) -> dict[str, Any]:
    """Build the public protocol packet from the allowlisted source projection."""

    day = _coerce_day(snapshot.get("date"))
    deployments = [item for item in snapshot.get("deployments", []) if isinstance(item, dict)]
    events = [item for item in snapshot.get("events", []) if isinstance(item, dict)]
    trades = [item for item in snapshot.get("trades", []) if isinstance(item, dict)]
    start, end = _session_bounds(day)
    by_deployment = {
        str(item.get("deployment_id")): item
        for item in deployments
        if item.get("deployment_id") and item.get("symbol") and item.get("enabled") is not False
    }
    instruments: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    case_by_deployment: dict[str, str] = {}
    for deployment_id, deployment in sorted(by_deployment.items()):
        symbol = _symbol(deployment["symbol"])
        if symbol is None:
            continue
        instrument_id = _safe_slug(f"bhiksha-{symbol}")
        if not any(item["id"] == instrument_id for item in instruments):
            instruments.append({"id": instrument_id, "kind": "stock", "symbol": symbol, "currency": "USD"})
        case_id = _safe_slug(f"bhiksha-{day.isoformat()}-{_short_hash(deployment_id)}")
        mode = _deployment_mode(deployment)
        strategy = _strategy(deployment)
        cases.append(
            {
                "id": case_id,
                "title": f"{symbol} {strategy} strategy day",
                "instrumentId": instrument_id,
                "startTime": start,
                "endTime": end,
                "observationTime": start,
                "extensions": {
                    "bhiksha:scope": {
                        "deploymentId": deployment_id,
                        "mode": mode,
                        "strategy": strategy,
                    }
                },
            }
        )
        case_by_deployment[deployment_id] = case_id

    provenance_ref = "bhiksha.sqlite:allowlisted-day-snapshot"
    records = _records(events, trades, by_deployment, case_by_deployment, provenance_ref)
    coverage = _coverage(
        cases, deployments, events, records, events_truncated=bool(snapshot.get("events_truncated"))
    )
    published_at = max(
        [record["knownAt"] for record in records] + [end]
    )
    stream_id = _safe_slug(f"bhiksha-{day.isoformat()}")
    # `publishedAt` is derived from source evidence rather than wall time so an
    # unchanged snapshot produces exactly the same packet and filename.
    packet: dict[str, Any] = {
        "schemaVersion": 2,
        "source": {"id": "bhiksha", "label": "Bhiksha runtime ledger"},
        "run": {"id": stream_id, "revision": revision},
        "provenance": [{"ref": provenance_ref, "sha256": str(snapshot["snapshot_sha256"])}],
        "instruments": instruments,
        "cases": cases,
        "records": records,
        "publication": {
            "schemaVersion": 1,
            "streamId": stream_id,
            "revision": revision,
            "publishedAt": published_at,
            "coverage": coverage,
        },
    }
    canonical = {key: value for key, value in packet.items() if key != "id"}
    packet["id"] = _safe_slug(f"bhiksha-{day.isoformat()}-{_sha(canonical)[:16]}")
    # Keep the standard envelope's customary key order irrelevant; JSON output
    # is canonicalized by the immutable writer below.
    return packet


def write_immutable_packet(packet: dict[str, Any], output_dir: str | Path) -> tuple[Path, bool]:
    """Write `<packet.id>.json`, reusing byte-identical canonical content only."""

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{packet['id']}.json"
    encoded = _canonical_json(packet)
    publication = packet["publication"]
    for existing_path in root.glob("*.json"):
        try:
            existing = json.loads(existing_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        existing_publication = existing.get("publication") if isinstance(existing, dict) else None
        if (
            isinstance(existing_publication, dict)
            and existing.get("source", {}).get("id") == packet["source"]["id"]
            and existing_publication.get("streamId") == publication["streamId"]
            and existing_publication.get("revision") == publication["revision"]
        ):
            if _canonical_json(existing) == encoded:
                return existing_path, True
            raise ValueError(
                "publication stream/revision already has immutable content; "
                "rerun with a higher --revision"
            )
    if target.exists():
        if target.read_bytes() == encoded:
            return target, True
        raise FileExistsError(f"immutable chart packet ID already has different content: {target}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=root)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # Hard-link creation prevents a racing exporter from replacing an
        # established immutable packet.
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != encoded:
                raise FileExistsError(f"immutable chart packet ID already has different content: {target}")
            return target, True
        return target, False
    finally:
        temporary.unlink(missing_ok=True)


def _read_events(
    connection: sqlite3.Connection, tables: set[str], day: Date, limit: int
) -> tuple[list[dict[str, Any]], bool]:
    if "events" not in tables:
        return [], False
    start, end = _central_day_bounds(day)
    placeholders = ",".join("?" for _ in _CRITICAL_EVENT_TYPES)
    critical_rows = connection.execute(
        "SELECT id, created_at, event_type, payload FROM events "
        f"WHERE created_at >= ? AND created_at < ? AND event_type IN ({placeholders}) ORDER BY id LIMIT ?",
        (start.isoformat(), end.isoformat(), *_CRITICAL_EVENT_TYPES, 50_001),
    ).fetchall()
    evaluation_rows = connection.execute(
        "SELECT id, created_at, event_type, payload FROM events "
        "WHERE created_at >= ? AND created_at < ? AND event_type = 'signal_evaluation' ORDER BY id LIMIT ?",
        (start.isoformat(), end.isoformat(), limit + 1),
    ).fetchall()
    critical_truncated = len(critical_rows) > 50_000
    evaluation_truncated = len(evaluation_rows) > limit
    rows = [*critical_rows[:50_000], *evaluation_rows[:limit]]
    rows.sort(key=lambda row: int(row["id"]))
    truncated = critical_truncated or evaluation_truncated
    output: list[dict[str, Any]] = []
    for row in rows[:limit]:
        payload = _json_object(row["payload"])
        # Retain only fields needed for chart evidence. The original payload can
        # contain broker response material and must never cross this boundary.
        item = {
            "id": int(row["id"]),
            "created_at": str(row["created_at"]),
            "event_type": str(row["event_type"]),
            "payload": _allowlisted_event_payload(payload),
        }
        output.append(item)
    return output, truncated


def _read_trades(connection: sqlite3.Connection, tables: set[str], day: Date) -> list[dict[str, Any]]:
    if "trade_sessions" not in tables:
        return []
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(trade_sessions)")}
    desired = [
        "trade_id", "deployment_id", "symbol", "option_symbol", "entry_timestamp",
        "underlying_entry_price", "status", "entry_order_id", "exit_price",
        "exit_filled_at", "exit_filled_quantity", "exit_order_status", "exit_order_id", "updated_at",
    ]
    selected = [name for name in desired if name in columns]
    if not {"trade_id", "deployment_id", "symbol"} <= set(selected):
        return []
    rows = connection.execute(f"SELECT {', '.join(selected)} FROM trade_sessions ORDER BY trade_id").fetchall()
    start, end = _central_day_bounds(day)
    output: list[dict[str, Any]] = []
    for row in rows:
        entry = _epoch(row["entry_timestamp"] if "entry_timestamp" in row.keys() else None)
        updated = _epoch(row["updated_at"] if "updated_at" in row.keys() else None)
        if entry is None and updated is None:
            continue
        observed = datetime.fromtimestamp(entry or updated or 0, UTC)
        if not start <= observed < end:
            continue
        trade_id = _identity(row["trade_id"])
        deployment_id = _identity(row["deployment_id"])
        symbol = _symbol(row["symbol"])
        if not trade_id or not deployment_id or not symbol:
            continue
        option_symbol = _identity(row["option_symbol"] if "option_symbol" in row.keys() else None)
        price = _positive_number(row["underlying_entry_price"] if "underlying_entry_price" in row.keys() else None)
        output.append({
            "trade_id": trade_id, "deployment_id": deployment_id, "symbol": symbol,
            "option_symbol": option_symbol, "entry_timestamp": entry,
            "underlying_entry_price": price, "status": _identity(row["status"] if "status" in row.keys() else None),
            "entry_order_id": _identity(row["entry_order_id"] if "entry_order_id" in row.keys() else None),
            "exit_price": _positive_number(row["exit_price"] if "exit_price" in row.keys() else None),
            "exit_filled_at": _epoch(row["exit_filled_at"] if "exit_filled_at" in row.keys() else None),
            "exit_filled_quantity": _positive_int(row["exit_filled_quantity"] if "exit_filled_quantity" in row.keys() else None),
            "exit_order_status": _identity(row["exit_order_status"] if "exit_order_status" in row.keys() else None),
            "exit_order_id": _identity(row["exit_order_id"] if "exit_order_id" in row.keys() else None),
            "updated_at": updated,
        })
    return output


def _deployments_from_startup(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int | None]:
    startup = next((item for item in reversed(events) if item["event_type"] == "startup_config"), None)
    if startup is None:
        return [], None
    raw_deployments = startup["payload"].get("deployments")
    if not isinstance(raw_deployments, list):
        return [], int(startup["id"])
    result: list[dict[str, Any]] = []
    for raw in raw_deployments:
        if not isinstance(raw, dict):
            continue
        deployment_id, symbol = _identity(raw.get("deployment_id")), _symbol(raw.get("symbol"))
        if not deployment_id or not symbol:
            continue
        execution = raw.get("execution") if isinstance(raw.get("execution"), dict) else {}
        strategy_raw = raw.get("strategy")
        strategy = strategy_raw.get("key") if isinstance(strategy_raw, dict) else strategy_raw
        result.append({
            "deployment_id": deployment_id, "symbol": symbol, "enabled": raw.get("enabled") is not False,
            "strategy": _identity(strategy) or "strategy-unavailable",
            "execution": {
                "shadow_only": execution.get("shadow_only") is True,
                "runtime_mode": _identity(execution.get("runtime_mode")),
            },
        })
    return result, int(startup["id"])


def _records(
    events: list[dict[str, Any]], trades: list[dict[str, Any]], deployments: dict[str, dict[str, Any]],
    case_by_deployment: dict[str, str], provenance_ref: str,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    confirmed_trade_exits = {
        (trade["trade_id"], _opaque_identity(trade.get("exit_order_id")))
        for trade in trades
        if _trade_has_confirmed_exit(trade)
    }
    confirmed_trade_entries = {
        (trade["deployment_id"], _opaque_identity(trade.get("entry_order_id")))
        for trade in trades
        if _opaque_identity(trade.get("entry_order_id")) is not None
        and _trade_fill_basis(trade, _deployment_mode(deployments.get(trade["deployment_id"], {}))) == "recorded"
    }
    seen_trade_sessions: set[str] = set()
    for event in events:
        payload = event["payload"]
        deployment_id = _identity(payload.get("deployment_id"))
        case_id = case_by_deployment.get(deployment_id or "")
        if case_id is None:
            continue
        known = _epoch(event["created_at"])
        if known is None:
            continue
        domain, basis, label = _event_shape(event["event_type"], payload)
        occurred = _epoch(payload.get("timestamp") or payload.get("entry_timestamp") or payload.get("exit_filled_at"))
        if occurred is not None and occurred > known:
            occurred = known
        record: dict[str, Any] = {
            "id": _safe_slug(f"bhiksha-event-{event['id']}"), "revision": 1,
            "caseId": case_id, "domain": domain, "basis": basis, "knownAt": known,
            "sourceRefs": [provenance_ref], "label": label,
            "explanation": "Recorded by the Bhiksha runtime ledger; only allowlisted source fields were exported.",
        }
        if domain == "fill" or occurred is not None:
            record["occurredAt"] = occurred if occurred is not None else known
        extension = _event_extension(payload)
        if extension:
            record["extensions"] = {"bhiksha:execution": extension}
        corroborates_trade_exit = (
            event["event_type"] == "exit_fill_enriched"
            and (extension.get("tradeId"), extension.get("exitReceiptId")) in confirmed_trade_exits
        )
        corroborates_trade_entry = (
            event["event_type"] == "entry_fill_check"
            and extension.get("entryReceiptId") is not None
            and (deployment_id, extension.get("entryReceiptId")) in confirmed_trade_entries
        )
        if corroborates_trade_exit:
            record["domain"] = "exit"
            record["label"] = "exit fill corroboration recorded"
            record["explanation"] = "This runtime receipt corroborates the matching retained trade-session exit; the trade session owns its single chart marker."
            record.setdefault("extensions", {})["bhiksha:corroboration"] = {
                "tradeId": extension.get("tradeId"),
                "exitReceiptId": extension.get("exitReceiptId"),
                "corroborates": "trade-session-exit",
            }
        if corroborates_trade_entry:
            record["domain"] = "management"
            record["label"] = "entry fill corroboration recorded"
            record["explanation"] = "This runtime fill-check receipt corroborates the matching retained trade-session entry; the trade session owns its exact entry marker."
            record.setdefault("extensions", {})["bhiksha:corroboration"] = {
                "entryReceiptId": extension.get("entryReceiptId"),
                "corroborates": "trade-session-entry",
            }
        underlying = _positive_number(payload.get("underlying_entry_price") or payload.get("underlying_price"))
        marker_time = record.get("occurredAt", known)
        # Underlying coordinate only. Option premium is retained in the private
        # source extension-free metadata and never drawn on an underlying chart.
        instrument = _safe_slug(f"bhiksha-{_symbol(deployments[deployment_id]['symbol'])}")
        # A false evaluation remains inspectable but deliberately has no chart
        # marker: there was no trigger at that underlying coordinate.
        if not corroborates_trade_exit and not corroborates_trade_entry and not (event["event_type"] == "signal_evaluation" and payload.get("signal") is False):
            marker = {"type": "marker", "instrumentId": instrument, "units": "USD", "priceBasis": "instrument_price", "time": marker_time}
            if underlying is not None:
                marker["price"] = underlying
            record["visuals"] = [marker]
        records.append(record)
    for trade in trades:
        case_id = case_by_deployment.get(trade["deployment_id"])
        if case_id is None or trade["trade_id"] in seen_trade_sessions:
            continue
        seen_trade_sessions.add(trade["trade_id"])
        known = trade.get("updated_at") or trade.get("entry_timestamp")
        if not isinstance(known, int):
            continue
        record = {
            "id": _safe_slug(f"bhiksha-trade-{_short_hash(trade['trade_id'])}"), "revision": 1,
            "caseId": case_id, "domain": "management", "basis": "recorded", "knownAt": known,
            "sourceRefs": [provenance_ref], "label": "trade session recorded",
            "explanation": "A trade-session row identifies the runtime trade but does not by itself certify an entry fill.",
            "extensions": {"bhiksha:trade": {k: v for k, v in {
                "tradeId": trade["trade_id"], "optionSymbol": trade.get("option_symbol"),
                "optionRight": _option_right(trade.get("option_symbol")), "status": trade.get("status"),
                "entryReceiptId": _opaque_identity(trade.get("entry_order_id")),
                "exitReceiptId": _opaque_identity(trade.get("exit_order_id")),
            }.items() if v is not None}},
            "visuals": [{
                "type": "marker", "instrumentId": _safe_slug(f"bhiksha-{trade['symbol']}"), "units": "USD",
                "priceBasis": "instrument_price", "time": trade.get("entry_timestamp") or known,
                **({"price": trade["underlying_entry_price"]} if trade.get("underlying_entry_price") else {}),
            }],
        }
        if trade.get("entry_timestamp"):
            record["occurredAt"] = trade["entry_timestamp"]
        records.append(record)
        deployment = deployments[trade["deployment_id"]]
        mode = _deployment_mode(deployment)
        fill_basis = _trade_fill_basis(trade, mode)
        if fill_basis is not None:
            records.append({
                "id": _safe_slug(f"bhiksha-trade-entry-{_short_hash(trade['trade_id'])}"), "revision": 1,
                "caseId": case_id, "domain": "fill", "basis": fill_basis, "knownAt": known,
                "occurredAt": trade.get("entry_timestamp") or known, "sourceRefs": [provenance_ref],
                "label": "confirmed entry fill recorded" if fill_basis == "recorded" else "shadow entry assumption retained",
                "explanation": (
                    "The terminal Bhiksha trade-session state follows a confirmed live entry fill."
                    if fill_basis == "recorded" else "Bhiksha recorded this as a shadow assumption, not a broker fill."
                ),
                "extensions": record["extensions"], "visuals": record["visuals"],
            })
        if _trade_has_confirmed_exit(trade):
            records.append({
                "id": _safe_slug(f"bhiksha-trade-exit-{_short_hash(trade['trade_id'])}"), "revision": 1,
                "caseId": case_id, "domain": "exit", "basis": "recorded", "knownAt": known,
                "occurredAt": trade["exit_filled_at"], "sourceRefs": [provenance_ref],
                "label": "confirmed exit fill recorded",
                "explanation": "Bhiksha retained an explicit filled exit status, quantity, price, and time.",
                "extensions": record["extensions"],
                "visuals": [{"type": "marker", "instrumentId": _safe_slug(f"bhiksha-{trade['symbol']}"), "units": "USD", "priceBasis": "instrument_price", "time": trade["exit_filled_at"]}],
            })
    return sorted(records, key=lambda item: (item["knownAt"], item["id"]))


def _event_shape(event_type: str, payload: dict[str, Any]) -> tuple[str, str, str]:
    if event_type in {"signal_evaluation", "signal_decision"}:
        label = "non-signal evaluation recorded" if event_type == "signal_evaluation" and payload.get("signal") is False else "signal decision recorded"
        return "signal", "recorded", label
    if event_type == "trade_plan":
        return "order", "recorded", "order attempt recorded"
    if event_type == "shadow_entry_assumed":
        return "fill", "modeled", "shadow entry assumption recorded"
    if event_type == "entry_fill_check" and _confirmed_positive_fill(payload):
        return "fill", "recorded", "confirmed entry fill recorded"
    if event_type in {"exit_fill_enriched", "shadow_exit_assumed"}:
        return "exit", "modeled" if event_type == "shadow_exit_assumed" else "recorded", "exit evidence recorded"
    if event_type in {"lifecycle_entry_blocked", "runtime_issue", "entry_reconcile_released", "entry_reprice_blocked"}:
        return "rejection", "recorded", "entry outcome recorded"
    return "management", "recorded", "runtime lifecycle evidence recorded"


def _coverage(cases: list[dict[str, Any]], deployments: list[dict[str, Any]], events: list[dict[str, Any]], records: list[dict[str, Any]], *, events_truncated: bool) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    domains_by_case: dict[str, set[str]] = {}
    for record in records:
        domains_by_case.setdefault(record["caseId"], set()).add(record["domain"])
    for case in cases:
        scope = case["extensions"]["bhiksha:scope"]
        certified = _source_certifies_complete(events, scope["deploymentId"])
        if certified and not events_truncated:
            status, reason = "complete", "Bhiksha recorded a scoped session-complete certification."
        elif events_truncated:
            status, reason = "partial", "The bounded event read reached its configured limit."
        else:
            status, reason = "partial", "No scoped source session-complete certification was available; clock time is not evidence of completeness."
        result.append({
            "caseId": case["id"], "symbol": next((d["symbol"] for d in deployments if d.get("deployment_id") == scope["deploymentId"]), "unknown"),
            "startTime": case["startTime"], "endTime": case["endTime"], "status": status,
            "domains": ["signal", "order", "fill", "management", "exit", "rejection"], "reason": reason,
            "mode": scope["mode"], "strategy": scope["strategy"],
        })
    return result


def _source_certifies_complete(events: list[dict[str, Any]], deployment_id: str) -> bool:
    for event in events:
        if event["event_type"] not in {"session_coverage", "session_complete"}:
            continue
        payload = event["payload"]
        if _identity(payload.get("deployment_id")) != deployment_id:
            continue
        if payload.get("complete") is True or payload.get("session_complete") is True:
            return True
    return False


def _allowlisted_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "deployment_id", "symbol", "timestamp", "entry_timestamp", "exit_filled_at", "trade_id", "signal_attempt_id",
        "underlying_entry_price", "underlying_price", "option_symbol", "status", "filledQuantity", "filled_quantity",
        "quantity", "signal", "direction", "complete", "session_complete", "order_id", "entry_order_id", "exit_order_id",
    }
    result = {key: payload[key] for key in fields if key in payload and _safe_value(payload[key])}
    nested = payload.get("payload")
    if isinstance(nested, dict):
        for key in ("status", "filledQuantity"):
            if key not in result and key in nested and _safe_value(nested[key]):
                result[key] = nested[key]
    if isinstance(payload.get("deployments"), list):
        clean_deployments: list[dict[str, Any]] = []
        for deployment in payload["deployments"]:
            if not isinstance(deployment, dict):
                continue
            execution = deployment.get("execution") if isinstance(deployment.get("execution"), dict) else {}
            strategy = deployment.get("strategy")
            clean_deployments.append(
                {
                    "deployment_id": deployment.get("deployment_id"),
                    "symbol": deployment.get("symbol"),
                    "enabled": deployment.get("enabled"),
                    "strategy": {"key": strategy.get("key")} if isinstance(strategy, dict) else strategy,
                    "execution": {
                        "shadow_only": execution.get("shadow_only"),
                        "runtime_mode": execution.get("runtime_mode"),
                    },
                }
            )
        result["deployments"] = clean_deployments
    return result


def _event_extension(payload: dict[str, Any]) -> dict[str, Any]:
    option_symbol = _identity(payload.get("option_symbol"))
    values = {
        "tradeId": _identity(payload.get("trade_id")), "attemptId": _identity(payload.get("signal_attempt_id")),
        "optionSymbol": option_symbol, "optionRight": _option_right(option_symbol), "status": _identity(payload.get("status")),
        "quantity": _positive_int(payload.get("quantity")), "filledQuantity": _positive_int(payload.get("filledQuantity") or payload.get("filled_quantity")),
        "signal": payload.get("signal") if isinstance(payload.get("signal"), bool) else None,
        "entryReceiptId": _opaque_identity(payload.get("order_id") or payload.get("entry_order_id")),
        "exitReceiptId": _opaque_identity(payload.get("exit_order_id")),
    }
    return {key: value for key, value in values.items() if value is not None}


def _confirmed_positive_fill(payload: dict[str, Any]) -> bool:
    status = str(payload.get("status") or "").upper()
    quantity = _positive_int(payload.get("filledQuantity") or payload.get("filled_quantity"))
    return status == "FILLED" and quantity is not None


def _trade_fill_basis(trade: dict[str, Any], mode: str) -> str | None:
    status = str(trade.get("status") or "").lower()
    if status in {"pending_entry", "pending_entry_reconcile"}:
        return None
    if status == "closed" and not _trade_has_confirmed_exit(trade):
        return None
    if not trade.get("entry_timestamp") or not trade.get("option_symbol"):
        return None
    if mode == "shadow" or str(trade.get("entry_order_id") or "").upper().startswith("SHADOW"):
        return "modeled"
    if mode == "live":
        return "recorded"
    return None


def _trade_has_confirmed_exit(trade: dict[str, Any]) -> bool:
    return (
        str(trade.get("exit_order_status") or "").upper() == "FILLED"
        and trade.get("exit_price") is not None
        and trade.get("exit_filled_quantity") is not None
        and trade.get("exit_filled_at") is not None
    )


def _deployment_mode(deployment: dict[str, Any]) -> str:
    execution = deployment.get("execution") if isinstance(deployment.get("execution"), dict) else {}
    if execution.get("shadow_only") is True:
        return "shadow"
    if execution.get("runtime_mode") == "live_approval_gated":
        return "live"
    return "unknown"


def _strategy(deployment: dict[str, Any]) -> str:
    return _identity(deployment.get("strategy")) or "strategy-unavailable"


def _session_bounds(day: Date) -> tuple[int, int]:
    start = datetime(day.year, day.month, day.day, 8, 30, tzinfo=CENTRAL)
    end = datetime(day.year, day.month, day.day, 15, 0, tzinfo=CENTRAL)
    return int(start.timestamp()), int(end.timestamp())


def _central_day_bounds(day: Date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=CENTRAL).astimezone(UTC)
    next_day = Date.fromordinal(day.toordinal() + 1)
    end = datetime(next_day.year, next_day.month, next_day.day, tzinfo=CENTRAL).astimezone(UTC)
    return start, end


def _coerce_day(value: str | Date | None) -> Date:
    if value is None:
        return datetime.now(CENTRAL).date()
    if isinstance(value, Date):
        return value
    return Date.fromisoformat(value)


def _epoch(value: Any) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value / 1000) if value > 10_000_000_000 else int(value)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(parsed.astimezone(UTC).timestamp()) if parsed.tzinfo else None


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_value(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))


def _identity(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text if _SAFE_TEXT.fullmatch(text) else None


def _symbol(value: Any) -> str | None:
    text = _identity(value)
    return text.upper() if text and re.fullmatch(r"[A-Za-z0-9.:-]{1,32}", text) else None


def _option_right(option_symbol: str | None) -> str | None:
    if option_symbol is None:
        return None
    match = re.fullmatch(r"[A-Z]+\d{6}([CP])\d{8}", option_symbol.upper())
    if match is None:
        return None
    return "call" if match.group(1) == "C" else "put"


def _opaque_identity(value: Any) -> str | None:
    raw = _identity(value)
    return f"sha256:{_short_hash(raw)}" if raw else None


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _positive_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _safe_slug(value: str) -> str:
    compact = _SAFE_ID.sub("-", value.lower()).strip("-")
    return compact[:96] or "bhiksha"


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
