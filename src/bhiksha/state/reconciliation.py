"""Broker-state reconciliation helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import re
from typing import Iterable

from bhiksha.config.models import DeploymentManifest
from bhiksha.domain.models import TradeRecord
from bhiksha.execution.order_manager import normalize_option_symbol
from bhiksha.state.position_tracker import TrackedPosition

_OPTION_ROOT_RE = re.compile(r"^([A-Z]+)")
_RECOVERY_MATCH_WINDOW = timedelta(hours=6)


def _is_live_entry_order_id(order_id: str | None) -> bool:
    """True only for a REAL broker entry order id.

    Paper markers ("SHADOW_ENTRY", "DRY_RUN*") and missing ids are not live —
    mirrors ``bhiksha.execution.supervisor._is_paper_order_id`` semantics
    (kept local: the state layer must not import the execution layer).
    """
    if not order_id:
        return False
    return not (order_id == "SHADOW_ENTRY" or order_id.startswith("DRY_RUN"))


def reconcile_public_positions(
    positions: Iterable[dict],
    deployments: list[DeploymentManifest],
    *,
    orders: Iterable[dict] | None = None,
    known_trades: Iterable[TradeRecord] | None = None,
) -> list[TrackedPosition]:
    """Map Public portfolio positions into tracked deployment positions."""
    deployments_by_symbol: dict[str, list[DeploymentManifest]] = {}
    deployments_by_id = {deployment.deployment_id: deployment for deployment in deployments}
    for deployment in deployments:
        deployments_by_symbol.setdefault(deployment.symbol, []).append(deployment)
    stop_orders_by_symbol, limit_orders_by_symbol = _index_open_exit_orders(orders or [])
    known_trades = list(known_trades or [])
    trades_by_option_symbol: dict[str, list[TradeRecord]] = {}
    trades_by_order_id: dict[str, TradeRecord] = {}
    for trade in known_trades:
        if trade.option_symbol:
            trades_by_option_symbol.setdefault(trade.option_symbol, []).append(trade)
        # Order-id index (audit fix 2026-07-02): only OPEN trades can own a
        # RESTING broker order, so closed trades are excluded — a closed
        # trade's historical exit_order_id must never shadow a live trade's
        # resting stop. ``known_trades`` arrives newest-first (updated_at
        # DESC); ``setdefault`` keeps the NEWEST trade on an id collision
        # (the previous last-write-wins favored the oldest — backwards).
        if trade.status == "closed":
            continue
        for order_id in (trade.entry_order_id, trade.stop_order_id, trade.target_order_id, trade.exit_order_id):
            if order_id:
                trades_by_order_id.setdefault(order_id, trade)
    tracked: list[TrackedPosition] = []

    for position in positions:
        instrument = position.get("instrument", {}) or {}
        if instrument.get("type") != "OPTION":
            continue
        option_symbol = normalize_option_symbol(str(instrument.get("symbol", "")))
        symbol = _parse_option_root(option_symbol)
        stop_order = stop_orders_by_symbol.get(option_symbol) or {}
        limit_order = limit_orders_by_symbol.get(option_symbol) or {}
        broker_opened_at = _parse_opened_at(position)
        broker_entry_price = _parse_entry_price(position)
        matched_trade = _resolve_trade(
            option_symbol,
            stop_order_id=stop_order.get("order_id"),
            exit_or_target_order_id=limit_order.get("order_id"),
            trades_by_option_symbol=trades_by_option_symbol,
            trades_by_order_id=trades_by_order_id,
            broker_opened_at=broker_opened_at,
            broker_entry_price=broker_entry_price,
        )
        deployment = None
        trade_id = None
        entry_price = broker_entry_price
        entry_timestamp = broker_opened_at
        source = "broker_sync"
        if matched_trade is not None:
            deployment = deployments_by_id.get(matched_trade.deployment_id)
            trade_id = matched_trade.trade_id
            entry_price = entry_price or matched_trade.entry_price
            entry_timestamp = entry_timestamp or matched_trade.entry_timestamp
            # A broker position matched to a durable OPEN trade record whose entry
            # was a REAL broker order keeps its live identity. Reconciliation runs
            # every ~15s and REPLACES the tracker's positions wholesale, so labeling
            # these "broker_sync" stripped live positions of their profile-exit
            # dispatch authority within seconds of entry (the fail-closed allowlist
            # only opens for live_open/live_pending) — the 2026-07-01 armed-lanes-
            # never-dispatch root cause. Excluded on purpose: unmatched positions
            # ("broker_recovered"), paper entries, and positions matched to a
            # CLOSED trade record (record/broker divergence — not a position the
            # profile route should own).
            if _is_live_entry_order_id(matched_trade.entry_order_id) and matched_trade.status != "closed":
                source = "live_open"
        else:
            symbol_deployments = deployments_by_symbol.get(symbol, [])
            if len(symbol_deployments) == 1:
                deployment = symbol_deployments[0]
                trade_id = _synthetic_trade_id(
                    deployment_id=deployment.deployment_id,
                    option_symbol=option_symbol,
                    quantity=_parse_quantity(position),
                    entry_price=entry_price,
                    opened_at=entry_timestamp,
                )
                source = "broker_recovered"
            else:
                continue
        if deployment is None:
            continue
        quantity = _parse_quantity(position)
        if quantity <= 0:
            continue
        exit_order: dict[str, float | str] = {}
        target_order: dict[str, float | str] = {}
        if matched_trade is not None and matched_trade.status == "exit_pending":
            exit_order = limit_order
        else:
            target_order = limit_order
        tracked.append(
            TrackedPosition(
                symbol=symbol,
                deployment_id=deployment.deployment_id,
                trade_id=trade_id,
                option_symbol=option_symbol,
                quantity=quantity,
                entry_price=entry_price,
                underlying_entry_price=matched_trade.underlying_entry_price if matched_trade is not None else None,
                entry_timestamp=entry_timestamp,
                source=source,
                order_id=matched_trade.entry_order_id if matched_trade is not None else None,
                stop_order_id=stop_order.get("order_id") or (matched_trade.stop_order_id if matched_trade is not None else None),
                stop_price=stop_order.get("price") or (matched_trade.stop_price if matched_trade is not None else None),
                target_order_id=target_order.get("order_id") or (matched_trade.target_order_id if matched_trade is not None else None),
                target_price=target_order.get("price") or (matched_trade.target_price if matched_trade is not None else None),
                exit_order_id=exit_order.get("order_id") or (matched_trade.exit_order_id if matched_trade is not None else None),
                exit_limit_price=exit_order.get("price") or (matched_trade.exit_limit_price if matched_trade is not None else None),
                exit_submitted_at=matched_trade.exit_submitted_at if matched_trade is not None else None,
                exit_mode=matched_trade.exit_mode if matched_trade is not None and matched_trade.status == "exit_pending" else None,
            )
        )
    return tracked


def _resolve_trade(
    option_symbol: str,
    *,
    stop_order_id: str | None,
    exit_or_target_order_id: str | None,
    trades_by_option_symbol: dict[str, list[TradeRecord]],
    trades_by_order_id: dict[str, TradeRecord],
    broker_opened_at: datetime | None,
    broker_entry_price: float | None,
) -> TradeRecord | None:
    for order_id in (stop_order_id, exit_or_target_order_id):
        if order_id and order_id in trades_by_order_id:
            return trades_by_order_id[order_id]
    option_trades = trades_by_option_symbol.get(option_symbol, [])
    # Hard evidence filter (audit fix 2026-07-02): a candidate that POSITIVELY
    # contradicts the broker's own evidence (openedAt outside the recovery
    # window, or cost basis disagreeing — both sides present) can never match
    # this position, no matter how few candidates remain. Without this, a
    # stale open record (a close-write that lagged or failed) captured a
    # brand-new fill on the same contract via the single-candidate shortcut —
    # handing it live_open dispatch authority plus the stale trade's ladder
    # state, and self-reinforcing (the mismatched position kept the stale
    # record looking active, so it was never marked closed). Candidates with
    # missing evidence on either side are NOT contradicted (nothing to
    # corroborate against) and continue through the cascade unchanged.
    candidates = [
        trade
        for trade in option_trades
        if not _contradicts_broker_evidence(
            trade, broker_opened_at=broker_opened_at, broker_entry_price=broker_entry_price
        )
    ]
    # Single-candidate shortcut, restricted to a single surviving OPEN trade
    # (a closed record should reach a live broker position only through the
    # corroborated fuzzy cascade below, never by being the only record left).
    open_trades = [trade for trade in candidates if trade.status != "closed"]
    if len(open_trades) == 1:
        return open_trades[0]
    time_matches = [
        trade for trade in candidates
        if _matches_broker_opened_at(trade, broker_opened_at)
    ]
    if len(time_matches) == 1:
        return time_matches[0]
    price_matches = [
        trade for trade in time_matches or candidates
        if _matches_broker_entry_price(trade, broker_entry_price)
    ]
    if len(price_matches) == 1:
        return price_matches[0]
    return None


def _contradicts_broker_evidence(
    trade: TradeRecord,
    *,
    broker_opened_at: datetime | None,
    broker_entry_price: float | None,
) -> bool:
    opened_at_contradicts = (
        broker_opened_at is not None
        and trade.entry_timestamp is not None
        and not _matches_broker_opened_at(trade, broker_opened_at)
    )
    # REGRESSION-D guard (re-audit 2026-07-02): a record's entry_price can
    # legitimately diverge from the broker's cost basis by more than the tight
    # $0.05 CORROBORATION threshold (e.g. the fill payload lacked price keys
    # and the record fell back to the submitted limit, then the fill improved).
    # Price alone therefore CONTRADICTS only on gross divergence — >10%
    # relative AND >$0.25 — so a true record can't be rejected (which both
    # shut the dispatch gate and let sync_lifecycle mis-close the real trade).
    price_contradicts = False
    if broker_entry_price is not None and trade.entry_price is not None:
        divergence = abs(trade.entry_price - broker_entry_price)
        gross = divergence > max(0.25, 0.10 * max(trade.entry_price, broker_entry_price))
        price_contradicts = gross
    return opened_at_contradicts or price_contradicts


def _matches_broker_opened_at(trade: TradeRecord, broker_opened_at: datetime | None) -> bool:
    if broker_opened_at is None or trade.entry_timestamp is None:
        return False
    return abs(_ensure_utc(trade.entry_timestamp) - broker_opened_at) <= _RECOVERY_MATCH_WINDOW


def _matches_broker_entry_price(trade: TradeRecord, broker_entry_price: float | None) -> bool:
    if broker_entry_price is None or trade.entry_price is None:
        return False
    return abs(trade.entry_price - broker_entry_price) <= 0.05


def _synthetic_trade_id(
    *,
    deployment_id: str,
    option_symbol: str,
    quantity: int,
    entry_price: float | None,
    opened_at: datetime | None,
) -> str:
    opened_token = opened_at.isoformat() if opened_at is not None else "unknown"
    price_token = f"{entry_price:.2f}" if entry_price is not None else "unknown"
    return f"recovered:{deployment_id}:{option_symbol}:{quantity}:{price_token}:{opened_token}"


def _parse_option_root(option_symbol: str) -> str:
    match = _OPTION_ROOT_RE.match(option_symbol)
    return match.group(1) if match else ""


def _index_open_exit_orders(orders: Iterable[dict]) -> tuple[dict[str, dict[str, float | str]], dict[str, dict[str, float | str]]]:
    stop_indexed: dict[str, dict[str, float | str]] = {}
    limit_indexed: dict[str, dict[str, float | str]] = {}
    for order in orders:
        instrument = order.get("instrument", {}) or {}
        if instrument.get("type") != "OPTION":
            continue
        if str(order.get("side", "")).upper() != "SELL":
            continue
        status = str(order.get("status", "")).upper()
        if status not in {"NEW", "SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_FILLED"}:
            continue
        symbol = normalize_option_symbol(str(instrument.get("symbol", "")))
        order_id = order.get("orderId")
        if not symbol or not order_id:
            continue
        order_type = str(order.get("type", "")).upper()
        if order_type == "STOP":
            stop_indexed[symbol] = {
                "order_id": str(order_id),
                "price": _maybe_float(order.get("stopPrice")),
            }
        elif order_type == "LIMIT":
            limit_indexed[symbol] = {
                "order_id": str(order_id),
                "price": _maybe_float(order.get("limitPrice")),
            }
    return stop_indexed, limit_indexed


def _parse_entry_price(position: dict) -> float | None:
    return _maybe_float(((position.get("costBasis") or {}).get("unitCost")))


def _parse_quantity(position: dict) -> int:
    return int(float(position.get("quantity", "0") or 0))


def _parse_opened_at(position: dict) -> datetime | None:
    raw = (
        position.get("openedAt")
        or position.get("opened_at")
        or position.get("openDate")
        or position.get("open_date")
    )
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw) / 1000.0, tz=UTC) if float(raw) > 10_000_000_000 else datetime.fromtimestamp(float(raw), tz=UTC)
    if not isinstance(raw, str):
        return None
    normalized = raw.strip()
    if not normalized:
        return None
    normalized = normalized.replace("Z", "+00:00")
    try:
        return _ensure_utc(datetime.fromisoformat(normalized))
    except ValueError:
        return None


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _maybe_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
