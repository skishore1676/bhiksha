"""Export Bhiksha-owned live exposure facts for the read-only family-risk shadow.

The exporter reads only local persisted state.  It never imports a broker client,
refreshes auth, places an order, or participates in an entry/exit decision.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SCHEMA = "family-risk-shadow/v1"
_OCC = re.compile(r"^[A-Z.]+\d{6}([CP])\d{8}$")
_DEFINITE_EXPOSURE_STATES = {
    "open_protected", "open_unprotected", "target_active", "exit_pending",
    "protection_failed_exit_pending", "critical_unprotected",
}

# App-wide standard equity-option contract multiplier.  Every Bhiksha money-path
# calculation (cash_guard._position_cash_cost, shadow_ev_report) treats a
# contract as 100 shares, and the exporter only ever maps symbols that pass the
# standard OCC regex above (adjusted-deliverable roots carry a numeric suffix and
# are already rejected as non-unambiguous), so 100 is a fact here, not a guess.
_CONTRACT_MULTIPLIER = 100
_MULTIPLIER_PROVENANCE = "occ_standard_equity_option:100_shares_per_contract_app_convention"

# App-owned static underlying -> correlation cluster map.  Deterministic and
# documented, not inferred: an underlying absent from this map exports a null
# cluster (never a guess).  Kept intentionally small; extend only with clusters
# Suman has confirmed in the family-risk brief.
_CLUSTER_BY_UNDERLYING = {
    "SPY": "broad_market", "QQQ": "broad_market", "IWM": "broad_market",
    "DIA": "broad_market", "VOO": "broad_market", "VTI": "broad_market",
    "NVDA": "semiconductors", "AMD": "semiconductors", "SMH": "semiconductors",
    "MU": "semiconductors", "INTC": "semiconductors", "TSM": "semiconductors",
    "AVGO": "semiconductors", "SOXL": "semiconductors", "SOXX": "semiconductors",
    "VIX": "volatility", "VXX": "volatility", "UVXY": "volatility",
    "SVXY": "volatility", "VIXY": "volatility",
}
_CLUSTER_PROVENANCE = "app_static_underlying_cluster_map"


def build_export(
    *, db_path: Path, account_cache: Path, account_alias: str, now: datetime | None = None
) -> dict[str, Any]:
    generated_at = (now or datetime.now(UTC)).astimezone(UTC)
    with _read_connection(db_path) as conn:
        conn.execute("BEGIN")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(trade_sessions)")}
        required = {
            "trade_id", "deployment_id", "symbol", "option_symbol", "quantity",
            "entry_price", "stop_price", "status", "entry_order_id", "updated_at",
        }
        if not required.issubset(columns):
            raise ValueError(f"trade_sessions schema missing required fields: {sorted(required - columns)}")
        rows = conn.execute(
            """
            SELECT trade_id, deployment_id, symbol, option_symbol, quantity,
                   entry_price, stop_price, status, entry_order_id, updated_at
            FROM trade_sessions WHERE lower(status) != 'closed'
            ORDER BY trade_id
            """
        ).fetchall()
        reconciliation_raw = _latest_completed_reconciliation(conn)
        account_summary = _latest_account_summary(conn)
        conn.execute("COMMIT")

    identity = _account_identity(account_cache, provider="public")
    definite_rows = [dict(row) for row in rows if str(row["status"]).lower() in _DEFINITE_EXPOSURE_STATES]
    ambiguous_rows = [row for row in rows if str(row["status"]).lower() not in _DEFINITE_EXPOSURE_STATES]
    exposures = [_map_row(row, broker_observed_at=reconciliation_raw) for row in definite_rows]
    source_observed_at = _utc_timestamp(reconciliation_raw) if reconciliation_raw else None
    unmapped = sorted({e["symbol"] for e in exposures if e["cluster"] is None})
    gaps = [
        "Account topology relative to other family apps is not verified.",
        "Bhiksha does not persist position Greeks or portfolio buying-power-reduction; "
        "long_option_capital shown is the cash premium paid (entry_price x quantity x 100).",
        "Correlation cluster is derived from an app-owned static underlying map, not persisted "
        "per trade; underlyings absent from the map export a null cluster.",
        "source_observed_at is the latest completed persisted broker reconciliation, not export time.",
    ]
    if not identity["verified"]:
        gaps.append("Stable account identity is unavailable from the app-owned account cache.")
    if reconciliation_raw is None:
        gaps.append("No completed broker reconciliation receipt is persisted; source_observed_at is null.")
    if account_summary is None:
        gaps.append(
            "No persisted cash_budget_days row; assigned capital and account summary are unavailable."
        )
    if unmapped:
        gaps.append(
            f"{len(unmapped)} open underlying(s) have no cluster mapping: {', '.join(unmapped)}."
        )
    if ambiguous_rows:
        gaps.append(
            f"{len(ambiguous_rows)} nonclosed trade-session row(s) are not in confirmed exposure lifecycle states "
            "and were omitted pending app-owned reconciliation."
        )
    return {
        "schema": SCHEMA,
        "source": "bhiksha",
        "record_scope": "live_open_positions",
        "source_table": "trade_sessions",
        "generated_at": generated_at.isoformat(),
        "source_observed_at": source_observed_at,
        "broker_observation": {
            "source_observed_at": source_observed_at,
            "staleness_seconds": (
                round((generated_at - datetime.fromisoformat(source_observed_at)).total_seconds(), 3)
                if source_observed_at else None
            ),
            "provenance": "latest completed broker reconciliation receipt; freshness label applied by the kernel report",
        },
        "account_alias": account_alias,
        "broker_provider": "public",
        "account_identity_verified": identity["verified"],
        "account_fingerprint": identity["fingerprint"],
        "account_group": identity["group"],
        "account_identity_provenance": identity["provenance"],
        "account_topology": "unknown",
        "account_summary": account_summary,
        "adapter_gaps": gaps,
        "trade_sessions": exposures,
    }


def _map_row(row: dict[str, Any], *, broker_observed_at: str | None) -> dict[str, Any]:
    option_symbol = str(row.get("option_symbol") or "").strip().upper()
    match = _OCC.fullmatch(option_symbol)
    if not match:
        raise ValueError(f"open trade {row.get('trade_id')} lacks an unambiguous OCC option symbol")
    quantity = int(row.get("quantity") or 0)
    if quantity <= 0:
        raise ValueError(f"open trade {row.get('trade_id')} has non-positive residual quantity")
    entry_order_id = str(row.get("entry_order_id") or "").strip()
    if not entry_order_id:
        raise ValueError(f"open trade {row.get('trade_id')} lacks a broker entry-order reference")
    direction = "call" if match.group(1) == "C" else "put"
    symbol = str(row["symbol"]).upper()
    cluster = _CLUSTER_BY_UNDERLYING.get(symbol)
    entry_price = row.get("entry_price")
    stop_price = row.get("stop_price")
    capital, worst_case, planned_stop, risk_notes = _long_option_risk(
        entry_price=entry_price, stop_price=stop_price,
        quantity=quantity, multiplier=_CONTRACT_MULTIPLIER,
    )
    return {
        "trade_id": str(row["trade_id"]),
        "broker_group_id": _safe_fingerprint("public-order", entry_order_id),
        "broker_position_ids": [_safe_fingerprint("public-option", option_symbol)],
        "broker_identity_provenance": "app_database_entry_order_and_occ_contract_fingerprints",
        "lane": "live",
        "status": str(row["status"]).lower(),
        "symbol": symbol,
        "option_type": direction,
        "position_side": "long",
        "cluster": cluster,
        "cluster_provenance": _CLUSTER_PROVENANCE if cluster is not None else None,
        "quantity": quantity,
        "contract_multiplier": _CONTRACT_MULTIPLIER,
        "multiplier_provenance": _MULTIPLIER_PROVENANCE,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "risk_basis": "long_option_defined_risk",
        "long_option_capital": capital,
        "worst_case_loss": worst_case,
        "planned_stop_loss": planned_stop,
        "risk_notes": risk_notes,
        "updated_at": _utc_timestamp(row["updated_at"]),
        "position_as_of": _utc_timestamp(row["updated_at"]),
        "mark_as_of": None,
        "broker_as_of": _utc_timestamp(broker_observed_at) if broker_observed_at else None,
    }


def _long_option_risk(
    *, entry_price: Any, stop_price: Any, quantity: int, multiplier: int
) -> tuple[float | None, float | None, float | None, list[str]]:
    """Defined-risk math for a LONG option, or an explicit reason it cannot be done.

    A long option's maximum loss is the full premium paid, so worst_case_loss
    equals long_option_capital; planned_stop_loss is the loss realized if closed
    at the persisted stop (clamped to >=0, since a stop at/above cost locks no
    loss).  Nothing is guessed: a missing entry or stop yields ``None`` plus a
    note, never a fabricated number.
    """
    notes: list[str] = []
    if entry_price is None:
        notes.append(
            "entry_price not persisted: long_option_capital, worst_case_loss, "
            "and planned_stop_loss cannot be calculated"
        )
        return None, None, None, notes
    entry = float(entry_price)
    capital = round(entry * quantity * multiplier, 2)
    worst_case = capital  # long option max loss == full premium paid
    if stop_price is None:
        notes.append("stop_price not persisted: planned_stop_loss cannot be calculated")
        return capital, worst_case, None, notes
    planned_stop = round(max(0.0, entry - float(stop_price)) * quantity * multiplier, 2)
    return capital, worst_case, planned_stop, notes


def _account_identity(path: Path, *, provider: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw = str(payload.get("accountId") or "").strip()
    except (OSError, ValueError, TypeError):
        raw = ""
    key = os.environ.get("FAMILY_RISK_IDENTITY_HMAC_KEY", "").encode("utf-8")
    if not raw or not key:
        return {"verified": False, "fingerprint": None, "group": None,
                "provenance": "account cache or operator HMAC key unavailable; identity unknown"}
    digest = hmac.new(key, raw.encode("utf-8"), hashlib.sha256).hexdigest()
    return {"verified": True, "fingerprint": f"hmac-sha256:{digest}",
            "group": f"{provider}:hmac-sha256:{digest}",
            "provenance": "keyed HMAC-SHA256 of app-owned broker account identity; raw identity and key omitted"}


def _latest_completed_reconciliation(conn: sqlite3.Connection) -> str | None:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "events" not in tables:
        return None
    rows = conn.execute(
        "SELECT created_at, payload FROM events WHERE event_type='runtime_metric' ORDER BY id DESC LIMIT 500"
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if payload.get("metric") == "portfolio_sync_ms":
            return str(row["created_at"])
    return None


def _latest_account_summary(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The most recent persisted cash-budget day as an app-owned account summary.

    ``cash_budget_days`` is written by ``CashGuard`` from a broker portfolio
    readback (``cashOnlyBuyingPower``) — the amount of capital Bhiksha is
    operating with — never from a broker call at export time.  Returns ``None``
    when the table or a row is absent so the caller can record an explicit gap.
    """
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "cash_budget_days" not in tables:
        return None
    row = conn.execute(
        """
        SELECT trade_date, account_type, broker_cash_only_buying_power,
               usable_budget, buffer_pct, updated_at
        FROM cash_budget_days ORDER BY trade_date DESC LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return {
        "trade_date": row["trade_date"],
        "account_type": row["account_type"],
        "assigned_capital_basis": "broker_cash_only_buying_power",
        "assigned_capital": float(row["broker_cash_only_buying_power"]),
        "broker_cash_only_buying_power": float(row["broker_cash_only_buying_power"]),
        "usable_daily_budget": float(row["usable_budget"]),
        "budget_buffer_pct": float(row["buffer_pct"]),
        "observed_at": _utc_timestamp(row["updated_at"]) if row["updated_at"] else None,
        "provenance": (
            "app-owned cash_budget_days row derived from a persisted broker portfolio "
            "readback; no broker call at export time"
        ),
    }


def _safe_fingerprint(namespace: str, raw: str) -> str:
    return f"{namespace}:sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def _utc_timestamp(value: Any) -> str:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Bhiksha timestamp has no timezone provenance: {value!r}")
    return parsed.astimezone(UTC).isoformat()


def _read_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="bhiksha.db")
    parser.add_argument("--account-cache", default="config/public_account.json")
    parser.add_argument("--account-alias", default="bhiksha-public")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    payload = build_export(db_path=Path(args.db), account_cache=Path(args.account_cache),
                           account_alias=args.account_alias)
    atomic_write_json(Path(args.out), payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
