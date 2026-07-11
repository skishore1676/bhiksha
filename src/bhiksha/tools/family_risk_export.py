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
        conn.execute("COMMIT")

    identity = _account_identity(account_cache, provider="public")
    definite_rows = [dict(row) for row in rows if str(row["status"]).lower() in _DEFINITE_EXPOSURE_STATES]
    ambiguous_rows = [row for row in rows if str(row["status"]).lower() not in _DEFINITE_EXPOSURE_STATES]
    exposures = [_map_row(row, broker_observed_at=reconciliation_raw) for row in definite_rows]
    gaps = [
        "Account topology relative to other family apps is not verified.",
        "Bhiksha trade_sessions does not persist BPR or position Greeks.",
        "Correlation cluster is not persisted with an open trade session.",
        "source_observed_at is the latest completed persisted broker reconciliation, not export time.",
    ]
    if not identity["verified"]:
        gaps.append("Stable account identity is unavailable from the app-owned account cache.")
    if reconciliation_raw is None:
        gaps.append("No completed broker reconciliation receipt is persisted; source_observed_at is null.")
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
        "source_observed_at": _utc_timestamp(reconciliation_raw) if reconciliation_raw else None,
        "account_alias": account_alias,
        "broker_provider": "public",
        "account_identity_verified": identity["verified"],
        "account_fingerprint": identity["fingerprint"],
        "account_group": identity["group"],
        "account_identity_provenance": identity["provenance"],
        "account_topology": "unknown",
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
    return {
        "trade_id": str(row["trade_id"]),
        "broker_group_id": _safe_fingerprint("public-order", entry_order_id),
        "broker_position_ids": [_safe_fingerprint("public-option", option_symbol)],
        "broker_identity_provenance": "app_database_entry_order_and_occ_contract_fingerprints",
        "lane": "live",
        "status": str(row["status"]).lower(),
        "symbol": str(row["symbol"]).upper(),
        "option_type": direction,
        "position_side": "long",
        "cluster": None,
        "quantity": quantity,
        "contract_multiplier": None,
        "multiplier_provenance": None,
        "entry_price": row.get("entry_price"),
        "stop_price": row.get("stop_price"),
        "updated_at": _utc_timestamp(row["updated_at"]),
        "position_as_of": _utc_timestamp(row["updated_at"]),
        "mark_as_of": None,
        "broker_as_of": _utc_timestamp(broker_observed_at) if broker_observed_at else None,
    }


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
