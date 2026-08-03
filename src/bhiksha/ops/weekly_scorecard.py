"""Weekly profile-vs-legacy scorecard for the live profile-exit experiment.

This is the month-long experiment's VERDICT MECHANISM (workplan #5). Where
``daily_report`` answers "what happened today", the weekly scorecard answers the
strategic question the experiment exists to settle:

    Do the operator-derived profile exits (T1 partial bank, stop->breakeven,
    T2 runner, plus no_progress / high_water_giveback / eod_flat) beat the
    legacy stop/target/strategy exits -- and which shadow lanes look promotable?

It reconstructs, for every trade closed in the week, the trade's REALIZED P&L
*including banked partial legs* (``trade_partial_fills``): ``trade_sessions``
overwrites ``quantity`` to the residual on each partial bank, so the banked
legs live only in ``trade_partial_fills`` and MUST be added back or a laddered
winner is undercounted by its whole T1 leg (2026-07-09 QQQ: +$468 partial that
never appears on the ``trade_sessions`` row). The cost basis for a banked leg is
the parent trade's ``entry_price``.

Report-only. This module never reads or touches order placement, sizing, or the
profile FSM; it is pure post-hoc analytics over the persisted trade record.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import json
import sqlite3
from pathlib import Path
from typing import Any, TYPE_CHECKING

# Reuse daily_report's canonical, well-tested primitives so the two reports
# classify a trade's lane and exit the exact same way (a lane that is "live" in
# the daily report must not read as "shadow" in the weekly one).
from bhiksha.ops.daily_report import (
    _compact_deployment_id,
    _data_quality_warnings,
    _exit_attribution,
    _fmt_money_or_na,
    _fmt_pct,
    _is_open_trade,
    _is_shadow_trade,
    _maybe_float,
    _maybe_int,
    _maybe_str,
    _parse_option_strike,
    _round_money,
)
from bhiksha.ops.trade_observation import (
    NON_TRADE_OUTCOMES,
    classify_trade_observation,
    index_terminal_entry_observations,
)

if TYPE_CHECKING:
    from bhiksha.config.models import DeploymentManifest

# Day 1 of the month-long live profile-exit test (workplan header). The
# cumulative live line always runs from here regardless of the week window.
EXPERIMENT_START = "2026-07-02"

# Promotion criteria (workplan #5). A shadow lane is a candidate only when it
# has cleared a minimum closed sample, is net-positive on realized dollars, and
# carries no disqualifying data-quality flag. Relaxed evidence is NOT itself
# disqualifying -- it is surfaced next to each candidate with the note that it
# needs fresh M7 validation / operator override before going live.
PROMOTION_MIN_CLOSED_TRADES = 5

# Operator-verified honesty caveat for the profile-vs-legacy comparison. Shadow
# "legacy" exits are paper marks, and paper stop marks print WORSE than the real
# broker fill: on 2026-07-09 the live QQQ disaster stop filled at -34.8% (design
# 35%, ~zero slippage) while shadow paper stops the same week printed -39/-41%.
# So a shadow legacy-stop bucket overstates the loss it is charged with, and the
# profile-vs-legacy gap in shadow is flattering to profile by that much.
PROFILE_VS_LEGACY_CAVEAT = (
    "Shadow legacy exits are paper marks and OVERSTATE stop slippage: the live "
    "QQQ disaster stop filled at -34.8% (35% design, ~zero slippage) while "
    "shadow paper stops the same week printed -39/-41%. Treat the shadow "
    "legacy-bucket loss as a pessimistic bound, and weight the LIVE rows more "
    "heavily when judging whether profile exits beat legacy."
)


@dataclass(slots=True, frozen=True)
class WeeklyScorecardWriteResult:
    report: dict[str, Any]
    json_path: Path
    markdown_path: Path


def write_weekly_scorecard(
    db_path: str | Path,
    *,
    output_dir: str | Path,
    week_start: date | str | None = None,
    week_end: date | str | None = None,
    experiment_start: date | str = EXPERIMENT_START,
    deployments: list["DeploymentManifest"] | None = None,
) -> WeeklyScorecardWriteResult:
    report = build_weekly_scorecard(
        db_path,
        week_start=week_start,
        week_end=week_end,
        experiment_start=experiment_start,
        deployments=deployments,
    )
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = f"weekly_scorecard_{report['week_start']}_{report['week_end']}"
    json_path = target_dir / f"{stem}.json"
    markdown_path = target_dir / f"{stem}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_weekly_scorecard_markdown(report), encoding="utf-8")
    return WeeklyScorecardWriteResult(report=report, json_path=json_path, markdown_path=markdown_path)


def build_weekly_scorecard(
    db_path: str | Path,
    *,
    week_start: date | str | None = None,
    week_end: date | str | None = None,
    experiment_start: date | str = EXPERIMENT_START,
    deployments: list["DeploymentManifest"] | None = None,
) -> dict[str, Any]:
    end = _coerce_day(week_end)
    start = _coerce_day(week_start) if week_start is not None else _monday_of(end)
    exp_start = _coerce_day(experiment_start)

    path = Path(db_path)
    if not path.exists():
        return _empty_report(start, end, exp_start, str(path))

    shadow_by_deployment, relaxed_by_deployment = _deployment_lookup(deployments)

    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = _load_window_trades(conn, start, end)
        partials_by_trade = _load_partials(conn, [row["trade_id"] for row in rows])
        terminal_events = _load_terminal_entry_events(conn, start, end)
        cumulative_rows = _load_live_cumulative_rows(conn, exp_start, end, shadow_by_deployment)

    terminal_by_trade = index_terminal_entry_observations(terminal_events)
    observation_outcomes: list[dict[str, Any]] = []
    trades = []
    for row in rows:
        trade = _augment_trade(
            dict(row),
            partials_by_trade.get(row["trade_id"], []),
            shadow_by_deployment,
        )
        observation = classify_trade_observation(trade, terminal_by_trade)
        if observation is not None:
            trade["observation_outcome"] = observation["observation_outcome"]
            trade["pnl_eligible"] = observation["pnl_eligible"]
            observation_outcomes.append(observation)
        if trade.get("observation_outcome") not in NON_TRADE_OUTCOMES:
            trades.append(trade)
    closed = [trade for trade in trades if not trade["is_open"]]

    lanes = _lane_rollups(trades, relaxed_by_deployment)
    headline = _headline(trades)
    profile_vs_legacy = _profile_vs_legacy(closed)
    promotion = _promotion_candidates(lanes, closed)
    live_cumulative = _live_cumulative(cumulative_rows, exp_start)
    data_quality_warnings = _data_quality_warnings(trades)

    return {
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "experiment_start": exp_start.isoformat(),
        "db_path": str(path),
        "deployments_supplied": deployments is not None,
        "headline": headline,
        "lanes": lanes,
        "profile_vs_legacy": profile_vs_legacy,
        "promotion_candidates": promotion,
        "live_cumulative": live_cumulative,
        "data_quality_warnings": data_quality_warnings,
        "observation_outcomes": observation_outcomes,
    }


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def _load_window_trades(conn: sqlite3.Connection, start: date, end: date) -> list[sqlite3.Row]:
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "trade_sessions" not in tables:
        return []
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(trade_sessions)").fetchall()}
    desired = [
        "trade_id",
        "deployment_id",
        "symbol",
        "option_symbol",
        "quantity",
        "entry_price",
        "underlying_entry_price",
        "entry_timestamp",
        "status",
        "entry_order_id",
        "stop_order_id",
        "stop_price",
        "target_order_id",
        "target_price",
        "exit_order_id",
        "exit_price",
        "exit_filled_quantity",
        "exit_filled_at",
        "exit_order_status",
        "exit_order_type",
        "exit_mode",
        "exit_rule",
        "can_ladder",
        "active_plan_id",
        "research_run_id",
        "evidence_packet_id",
        "evidence_artifact_sha256",
        "evidence_artifact_uri",
        "experiment_id",
        "cohort_id",
        "cohort_contract_sha256",
        "deployment_contract_sha256",
        "declared_option_selection_contract_id",
        "declared_option_selection_contract_sha256",
        "authorization_identity_status",
        "exit_policy_id",
        "exit_policy_sha256",
        "option_selection_snapshot_id",
        "option_selection_snapshot_persisted",
        "option_candidate_set_sha256",
        "actual_option_selection_sha256",
        "canary_id",
        "canary_authorization_sha256",
        "canary_start_at",
        "canary_expires_at",
        "plan_revision_id",
        "session_id",
        "fact_receipt_id",
        "frozen_entry_risk_usd",
        "frozen_round_trip_cost_usd",
    ]
    selected = [column for column in desired if column in columns]
    start_text = start.isoformat()
    end_text = end.isoformat()
    predicates: list[str] = []
    params: list[str] = []
    # A trade belongs to the week if it was entered OR exited inside the window.
    # All current live/shadow trades are intraday, but keeping both predicates
    # is correct for any future multi-day hold.
    if "entry_timestamp" in columns:
        predicates.append(
            "substr(replace(COALESCE(entry_timestamp, ''), ' ', 'T'), 1, 10) BETWEEN ? AND ?"
        )
        params.extend([start_text, end_text])
    if "exit_filled_at" in columns:
        predicates.append(
            "substr(replace(COALESCE(exit_filled_at, ''), ' ', 'T'), 1, 10) BETWEEN ? AND ?"
        )
        params.extend([start_text, end_text])
    if not predicates:
        return []
    order_columns = [c for c in ("entry_timestamp", "exit_filled_at", "trade_id") if c in columns]
    order_expr = f"COALESCE({', '.join(order_columns)})" if len(order_columns) > 1 else order_columns[0]
    return conn.execute(
        f"SELECT {', '.join(selected)} FROM trade_sessions WHERE {' OR '.join(predicates)} ORDER BY {order_expr}",
        params,
    ).fetchall()


def _load_partials(conn: sqlite3.Connection, trade_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "trade_partial_fills" not in tables or not trade_ids:
        return {}
    placeholders = ", ".join("?" for _ in trade_ids)
    rows = conn.execute(
        f"""
        SELECT trade_id, closed_quantity, fill_quantity, fill_price, exit_rule, origin, abandoned_reason
        FROM trade_partial_fills
        WHERE trade_id IN ({placeholders})
        ORDER BY id
        """,
        trade_ids,
    ).fetchall()
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(row["trade_id"], []).append(dict(row))
    return out


def _load_terminal_entry_events(
    conn: sqlite3.Connection,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "events" not in tables:
        return []
    rows = conn.execute(
        """
        SELECT id, created_at, event_type, payload
        FROM events
        WHERE event_type IN (
            'entry_reconcile_released',
            'entry_reprice_blocked',
            'entry_reprice_cancel_after_timeout'
        )
          AND substr(replace(COALESCE(created_at, ''), ' ', 'T'), 1, 10)
              BETWEEN ? AND ?
        ORDER BY id
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            payload = {}
        events.append(
            {
                "event_id": row["id"],
                "created_at": row["created_at"],
                "event_type": row["event_type"],
                "payload": payload if isinstance(payload, dict) else {},
            }
        )
    return events


def _load_live_cumulative_rows(
    conn: sqlite3.Connection,
    exp_start: date,
    end: date,
    shadow_by_deployment: dict[str, bool],
) -> list[dict[str, Any]]:
    """Every closed LIVE trade (with its partial legs) from experiment start
    through the week end, for the cumulative-by-day line."""
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "trade_sessions" not in tables:
        return []
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(trade_sessions)").fetchall()}
    if "entry_timestamp" not in columns:
        return []
    rows = conn.execute(
        """
        SELECT trade_id, deployment_id, entry_order_id, exit_order_type, entry_price, exit_price,
               exit_filled_quantity, quantity, entry_timestamp, exit_filled_at, status
        FROM trade_sessions
        WHERE substr(replace(COALESCE(entry_timestamp, ''), ' ', 'T'), 1, 10) BETWEEN ? AND ?
        ORDER BY COALESCE(exit_filled_at, entry_timestamp)
        """,
        (exp_start.isoformat(), end.isoformat()),
    ).fetchall()
    live_rows = [dict(row) for row in rows if not _is_shadow_trade(dict(row))]
    if not live_rows:
        return []
    partials = _load_partials(conn, [row["trade_id"] for row in live_rows])
    for row in live_rows:
        row["_partials"] = partials.get(row["trade_id"], [])
    return live_rows


# --------------------------------------------------------------------------- #
# Per-trade P&L (including banked partial legs)
# --------------------------------------------------------------------------- #


def _partial_leg_pnl(entry_price: float | None, partials: list[dict[str, Any]]) -> tuple[float, int]:
    """Sum realized P&L across confirmed banked partial legs and return the
    total banked quantity. Cost basis is the parent trade's entry_price; a
    banked leg's fill_quantity (fallback closed_quantity) is the leg size. Legs
    with no confirmed fill_price or an abandoned_reason are skipped from P&L
    (undercounting is safer than inventing a fill)."""
    if entry_price is None:
        return 0.0, 0
    total = 0.0
    banked_qty = 0
    for leg in partials:
        if leg.get("abandoned_reason"):
            continue
        fill_price = _maybe_float(leg.get("fill_price"))
        if fill_price is None:
            continue
        qty = _maybe_int(leg.get("fill_quantity")) or _maybe_int(leg.get("closed_quantity")) or 0
        if qty <= 0:
            continue
        total += (fill_price - entry_price) * qty * 100
        banked_qty += qty
    return total, banked_qty


def _trade_pnl_and_basis(row: dict[str, Any], partials: list[dict[str, Any]]) -> tuple[float | None, float | None, int]:
    """Return (realized_pnl_usd, cost_basis_usd, original_entry_qty).

    realized_pnl = final-leg P&L + banked-partial-leg P&L. cost_basis is entry
    premium over the ORIGINAL entry quantity (residual final qty + banked qty)
    so the blended return % is honest for a laddered trade.
    """
    entry = _maybe_float(row.get("entry_price"))
    exit_price = _maybe_float(row.get("exit_price"))
    final_qty = _maybe_int(row.get("exit_filled_quantity"))
    if final_qty is None:
        final_qty = _maybe_int(row.get("quantity")) or 0

    partial_pnl, banked_qty = _partial_leg_pnl(entry, partials)
    original_qty = final_qty + banked_qty

    has_final_leg = entry is not None and exit_price is not None and final_qty > 0
    has_partial_leg = entry is not None and banked_qty > 0
    if entry is None or not (has_final_leg or has_partial_leg):
        # Nothing we can trust as realized (still open, or missing prices).
        return None, None, original_qty

    final_pnl = (exit_price - entry) * final_qty * 100 if has_final_leg else 0.0
    realized = _round_money(final_pnl + partial_pnl)
    cost_basis = _round_money(entry * original_qty * 100) if original_qty else None
    return realized, cost_basis, original_qty


def _augment_trade(
    row: dict[str, Any],
    partials: list[dict[str, Any]],
    shadow_by_deployment: dict[str, bool],
) -> dict[str, Any]:
    realized, cost_basis, original_qty = _trade_pnl_and_basis(row, partials)
    deployment_id = _maybe_str(row.get("deployment_id"))
    # Lane is historical evidence, not current catalog state. A strategy may be
    # promoted after this trade closes; using today's manifest would then
    # rewrite a former shadow observation as a live fill. Trade-time order and
    # deployment markers are immutable and therefore authoritative.
    lane = "shadow" if _is_shadow_trade(row) else "live"
    is_open = _is_open_trade(row)
    exit_attribution = _exit_attribution(row)
    return_pct = None
    if realized is not None and cost_basis:
        return_pct = round(realized / cost_basis * 100, 2)
    return {
        **row,
        "lane": lane,
        "is_open": is_open,
        "realized_pnl_usd": realized,
        "cost_basis_usd": cost_basis,
        "original_entry_qty": original_qty,
        "banked_partial_count": len(partials),
        "return_pct": return_pct,
        "exit_attribution": exit_attribution,
        "is_profile_exit": bool(exit_attribution and exit_attribution.startswith("profile:")),
        "is_win": realized is not None and realized > 0,
        "option_strike": _parse_option_strike(_maybe_str(row.get("option_symbol"))),
    }


# --------------------------------------------------------------------------- #
# Rollups
# --------------------------------------------------------------------------- #


def _headline(trades: list[dict[str, Any]]) -> dict[str, Any]:
    def _side(subset: list[dict[str, Any]]) -> dict[str, Any]:
        closed = [t for t in subset if not t["is_open"]]
        missing_pnl = [t for t in closed if t["realized_pnl_usd"] is None]
        return {
            "trades": len(subset),
            "closed": len(closed),
            "open": len(subset) - len(closed),
            "wins": sum(1 for t in closed if t["is_win"]),
            "total_pnl_usd": (
                None
                if missing_pnl
                else _round_money(
                    sum(t["realized_pnl_usd"] or 0.0 for t in closed)
                )
            ),
            "missing_pnl_count": len(missing_pnl),
        }

    live = [t for t in trades if t["lane"] == "live"]
    shadow = [t for t in trades if t["lane"] == "shadow"]
    return {"live": _side(live), "shadow": _side(shadow), "total": _side(trades)}


def _lane_rollups(
    trades: list[dict[str, Any]],
    relaxed_by_deployment: dict[str, list[str]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        grouped.setdefault(str(trade.get("deployment_id") or "UNKNOWN"), []).append(trade)

    lanes: list[dict[str, Any]] = []
    for deployment_id, lane_trades in grouped.items():
        closed = [t for t in lane_trades if not t["is_open"]]
        missing_pnl = [t for t in closed if t["realized_pnl_usd"] is None]
        returns = [t["return_pct"] for t in closed if t["return_pct"] is not None]
        exit_counts = Counter(
            t["exit_attribution"] for t in closed if t["exit_attribution"]
        )
        relaxed = relaxed_by_deployment.get(deployment_id)
        lanes.append(
            {
                "deployment_id": deployment_id,
                "display_id": _compact_deployment_id(deployment_id),
                "mode": "live" if any(t["lane"] == "live" for t in lane_trades) else "shadow",
                "trades": len(lane_trades),
                "closed": len(closed),
                "open": len(lane_trades) - len(closed),
                "wins": sum(1 for t in closed if t["is_win"]),
                "total_pnl_usd": (
                    None
                    if missing_pnl
                    else _round_money(
                        sum(t["realized_pnl_usd"] or 0.0 for t in closed)
                    )
                ),
                "missing_pnl_count": len(missing_pnl),
                "avg_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
                "exit_rule_counts": dict(sorted(exit_counts.items())),
                "evidence_gates_relaxed": relaxed,
            }
        )
    # Live first, then by realized P&L descending, then id for stability.
    return sorted(
        lanes,
        key=lambda lane: (lane["mode"] != "live", -(lane["total_pnl_usd"] or 0.0), lane["deployment_id"]),
    )


def _bucket(trades: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [t["return_pct"] for t in trades if t["return_pct"] is not None]
    missing_pnl = [t for t in trades if t["realized_pnl_usd"] is None]
    return {
        "n": len(trades),
        "wins": sum(1 for t in trades if t["is_win"]),
        "total_pnl_usd": (
            None
            if missing_pnl
            else _round_money(
                sum(t["realized_pnl_usd"] or 0.0 for t in trades)
            )
        ),
        "avg_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
    }


def _profile_vs_legacy(closed: list[dict[str, Any]]) -> dict[str, Any]:
    def _split(subset: list[dict[str, Any]]) -> dict[str, Any]:
        profile = [t for t in subset if t["is_profile_exit"]]
        legacy = [t for t in subset if not t["is_profile_exit"]]
        return {"profile": _bucket(profile), "legacy": _bucket(legacy)}

    live = [t for t in closed if t["lane"] == "live"]
    shadow = [t for t in closed if t["lane"] == "shadow"]
    return {
        "live": _split(live),
        "shadow": _split(shadow),
        "overall": _split(closed),
        "caveat": PROFILE_VS_LEGACY_CAVEAT,
    }


def _promotion_candidates(
    lanes: list[dict[str, Any]],
    closed: list[dict[str, Any]],
) -> dict[str, Any]:
    warned_deployments = {
        str(warning.get("deployment_id"))
        for warning in _data_quality_warnings(closed)
        if warning.get("deployment_id") is not None
    }

    candidates: list[dict[str, Any]] = []
    near_misses: list[dict[str, Any]] = []
    for lane in lanes:
        if lane["mode"] != "shadow":
            continue
        disqualified = lane["deployment_id"] in warned_deployments
        relaxed = lane["evidence_gates_relaxed"]
        entry = {
            "deployment_id": lane["deployment_id"],
            "display_id": lane["display_id"],
            "closed": lane["closed"],
            "wins": lane["wins"],
            "total_pnl_usd": lane["total_pnl_usd"],
            "avg_return_pct": lane["avg_return_pct"],
            "evidence_gates_relaxed": relaxed,
            "note": _promotion_note(relaxed),
        }
        qualifies = (
            lane["closed"] >= PROMOTION_MIN_CLOSED_TRADES
            and (lane["total_pnl_usd"] or 0.0) > 0
            and not disqualified
        )
        if qualifies:
            candidates.append(entry)
        elif lane["closed"] >= PROMOTION_MIN_CLOSED_TRADES:
            # Cleared the sample bar but failed P&L / flags -- worth showing so
            # the operator sees the reasoning, not a silent omission.
            entry = {**entry, "disqualified_by": _disqualifier(lane, disqualified)}
            near_misses.append(entry)

    candidates.sort(key=lambda c: -(c["total_pnl_usd"] or 0.0))
    near_misses.sort(key=lambda c: -(c["total_pnl_usd"] or 0.0))
    return {
        "criteria": {
            "mode": "shadow",
            "min_closed_trades": PROMOTION_MIN_CLOSED_TRADES,
            "positive_total_pnl": True,
            "no_disqualifying_flags": True,
        },
        "candidates": candidates,
        "near_misses": near_misses,
    }


def _disqualifier(lane: dict[str, Any], data_quality_disqualified: bool) -> str:
    if data_quality_disqualified:
        return "data_quality_warning"
    if (lane["total_pnl_usd"] or 0.0) <= 0:
        return "non_positive_total_pnl"
    return "unknown"


def _promotion_note(relaxed: list[str] | None) -> str:
    if relaxed is None:
        return "evidence-gate status unknown (deployments not supplied); confirm before promotion"
    if relaxed:
        return (
            "relaxed-evidence lane -- needs fresh M7 validation or explicit operator override "
            "before going live"
        )
    return "no relaxed evidence gates recorded"


def _live_cumulative(live_rows: list[dict[str, Any]], exp_start: date) -> dict[str, Any]:
    by_day: dict[str, dict[str, Any]] = {}
    total = 0.0
    total_trades = 0
    for row in live_rows:
        partials = row.get("_partials", [])
        realized, _basis, _qty = _trade_pnl_and_basis(row, partials)
        if realized is None:
            continue
        day = _row_day(row)
        bucket = by_day.setdefault(day, {"day": day, "trades": 0, "pnl_usd": 0.0})
        bucket["trades"] += 1
        bucket["pnl_usd"] = _round_money(bucket["pnl_usd"] + realized)
        total = _round_money(total + realized)
        total_trades += 1
    return {
        "since": exp_start.isoformat(),
        "by_day": [by_day[day] for day in sorted(by_day)],
        "total_pnl_usd": _round_money(total),
        "total_trades": total_trades,
    }


def _row_day(row: dict[str, Any]) -> str:
    raw = row.get("exit_filled_at") or row.get("entry_timestamp") or ""
    return str(raw).replace(" ", "T")[:10]


# --------------------------------------------------------------------------- #
# Deployment lookup
# --------------------------------------------------------------------------- #


def _deployment_lookup(
    deployments: list["DeploymentManifest"] | None,
) -> tuple[dict[str, bool], dict[str, list[str]]]:
    shadow_by_deployment: dict[str, bool] = {}
    relaxed_by_deployment: dict[str, list[str]] = {}
    if not deployments:
        return shadow_by_deployment, relaxed_by_deployment
    for deployment in deployments:
        deployment_id = getattr(deployment, "deployment_id", None)
        if deployment_id is None:
            continue
        execution = getattr(deployment, "execution", None)
        shadow_by_deployment[deployment_id] = bool(getattr(execution, "shadow_only", False))
        source = getattr(deployment, "source", None)
        metadata = getattr(source, "metadata", None) or {}
        gates = metadata.get("evidence_gates_relaxed")
        relaxed_by_deployment[deployment_id] = list(gates) if gates else []
    return shadow_by_deployment, relaxed_by_deployment


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def render_weekly_scorecard_markdown(report: dict[str, Any]) -> str:
    headline = report.get("headline") or {}
    live = headline.get("live") or {}
    shadow = headline.get("shadow") or {}
    total = headline.get("total") or {}
    lines = [
        f"# Bhiksha Weekly Scorecard - {report.get('week_start')} to {report.get('week_end')}",
        "",
        "The month-test verdict: do profile exits beat legacy, and which shadow lanes look promotable.",
        "",
        "## Week Headline",
        "",
        f"- live: `{_fmt_money_or_na(live.get('total_pnl_usd'))}` "
        f"({live.get('closed', 0)} closed, {live.get('wins', 0)} wins)",
        f"- shadow: `{_fmt_money_or_na(shadow.get('total_pnl_usd'))}` "
        f"({shadow.get('closed', 0)} closed, {shadow.get('wins', 0)} wins)",
        f"- combined realized: `{_fmt_money_or_na(total.get('total_pnl_usd'))}` "
        f"({total.get('closed', 0)} closed trades)",
    ]
    if not report.get("deployments_supplied"):
        lines.append(
            "- note: `deployments not supplied -- lane mode inferred from trade rows, "
            "evidence-gate status unknown`"
        )

    # --- Per-lane table ---------------------------------------------------- #
    lanes = report.get("lanes") or []
    if lanes:
        lines.extend(
            [
                "",
                "## Per-Lane",
                "",
                "| Lane | Mode | Trades | Wins | P&L (incl. partials) | Avg Ret % | Exit rules | Relaxed |",
                "|---|---|---:|---:|---:|---:|---|---|",
            ]
        )
        for lane in lanes:
            exit_rules = ", ".join(f"{rule}×{count}" for rule, count in (lane.get("exit_rule_counts") or {}).items())
            lines.append(
                "| {display} | {mode} | {trades} | {wins} | {pnl} | {ret} | {rules} | {relaxed} |".format(
                    display=lane.get("display_id", ""),
                    mode=lane.get("mode", ""),
                    trades=lane.get("closed", 0),
                    wins=lane.get("wins", 0),
                    pnl=_fmt_money_or_na(lane.get("total_pnl_usd")),
                    ret=_fmt_pct(lane.get("avg_return_pct")),
                    rules=exit_rules or "-",
                    relaxed=_relaxed_flag(lane.get("evidence_gates_relaxed")),
                )
            )

    # --- Profile vs legacy ------------------------------------------------- #
    pvl = report.get("profile_vs_legacy") or {}
    if pvl:
        lines.extend(
            [
                "",
                "## Profile Exits vs Legacy Exits (realized, closed trades)",
                "",
                "| Scope | Exit kind | n | Wins | P&L | Avg Ret % |",
                "|---|---|---:|---:|---:|---:|",
            ]
        )
        for scope in ("live", "shadow", "overall"):
            block = pvl.get(scope) or {}
            for kind in ("profile", "legacy"):
                bucket = block.get(kind) or {}
                lines.append(
                    "| {scope} | {kind} | {n} | {wins} | {pnl} | {ret} |".format(
                        scope=scope,
                        kind=kind,
                        n=bucket.get("n", 0),
                        wins=bucket.get("wins", 0),
                        pnl=_fmt_money_or_na(bucket.get("total_pnl_usd")),
                        ret=_fmt_pct(bucket.get("avg_return_pct")),
                    )
                )
        lines.extend(["", f"> Caveat: {pvl.get('caveat', '')}"])

    # --- Promotion candidates --------------------------------------------- #
    promotion = report.get("promotion_candidates") or {}
    criteria = promotion.get("criteria") or {}
    lines.extend(
        [
            "",
            "## Promotion Candidates",
            "",
            f"Criteria: shadow lane, >= {criteria.get('min_closed_trades', PROMOTION_MIN_CLOSED_TRADES)} "
            "closed trades, positive total P&L, no disqualifying flags.",
        ]
    )
    candidates = promotion.get("candidates") or []
    if candidates:
        lines.extend(
            [
                "",
                "| Lane | Trades | Wins | P&L | Avg Ret % | Evidence |",
                "|---|---:|---:|---:|---:|---|",
            ]
        )
        for candidate in candidates:
            lines.append(
                "| {display} | {trades} | {wins} | {pnl} | {ret} | {relaxed} |".format(
                    display=candidate.get("display_id", ""),
                    trades=candidate.get("closed", 0),
                    wins=candidate.get("wins", 0),
                    pnl=_fmt_money_or_na(candidate.get("total_pnl_usd")),
                    ret=_fmt_pct(candidate.get("avg_return_pct")),
                    relaxed=_relaxed_flag(candidate.get("evidence_gates_relaxed")),
                )
            )
            lines.append(f"  - {candidate.get('display_id', '')}: {candidate.get('note', '')}")
    else:
        lines.append("")
        lines.append("- None this week: no shadow lane met the sample + positive-P&L bar.")
    near_misses = promotion.get("near_misses") or []
    if near_misses:
        lines.extend(["", "Near-misses (cleared sample bar, failed P&L/flags):"])
        for miss in near_misses:
            lines.append(
                "- `{display}`: {closed} closed, {pnl}, disqualified by `{by}`".format(
                    display=miss.get("display_id", ""),
                    closed=miss.get("closed", 0),
                    pnl=_fmt_money_or_na(miss.get("total_pnl_usd")),
                    by=miss.get("disqualified_by", "unknown"),
                )
            )

    # --- Live cumulative --------------------------------------------------- #
    cumulative = report.get("live_cumulative") or {}
    lines.extend(
        [
            "",
            f"## Live Experiment Cumulative (since {cumulative.get('since')})",
            "",
        ]
    )
    for day in cumulative.get("by_day") or []:
        lines.append(f"- `{day.get('day')}`: `{_fmt_money_or_na(day.get('pnl_usd'))}` ({day.get('trades', 0)} trades)")
    lines.append(
        f"- cumulative: `{_fmt_money_or_na(cumulative.get('total_pnl_usd'))}` "
        f"({cumulative.get('total_trades', 0)} live trades)"
    )

    # --- Data quality ------------------------------------------------------ #
    warnings = report.get("data_quality_warnings") or []
    if warnings:
        lines.extend(["", "## Data Quality Warnings"])
        for warning in warnings:
            lines.append(f"- `{warning.get('symbol')}` `{warning.get('option_symbol')}`: {warning.get('message')}")

    return "\n".join(lines) + "\n"


def render_weekly_scorecard_telegram_summary(
    report: dict[str, Any],
    *,
    markdown_path: str | Path | None = None,
) -> str:
    headline = report.get("headline") or {}
    live = headline.get("live") or {}
    shadow = headline.get("shadow") or {}
    pvl = report.get("profile_vs_legacy") or {}
    overall = pvl.get("overall") or {}
    profile = overall.get("profile") or {}
    legacy = overall.get("legacy") or {}
    promotion = report.get("promotion_candidates") or {}
    cumulative = report.get("live_cumulative") or {}
    candidates = promotion.get("candidates") or []

    lines = [
        f"Bhiksha Weekly Scorecard - {report.get('week_start')} to {report.get('week_end')}",
        "",
        "Verdict read",
        f"- Live: {_fmt_money_or_na(live.get('total_pnl_usd'))} "
        f"({live.get('closed', 0)} closed, {live.get('wins', 0)} wins)",
        f"- Shadow: {_fmt_money_or_na(shadow.get('total_pnl_usd'))} "
        f"({shadow.get('closed', 0)} closed, {shadow.get('wins', 0)} wins)",
        (
            "- Profile vs legacy (all): "
            f"profile ${_pnl(profile)} ({profile.get('n', 0)}), "
            f"legacy ${_pnl(legacy)} ({legacy.get('n', 0)})"
        ),
        f"- Promotion candidates: {len(candidates)}",
        (
            f"- Live cumulative since {cumulative.get('since')}: "
            f"${cumulative.get('total_pnl_usd', 0.0):.2f} "
            f"({cumulative.get('total_trades', 0)} trades)"
        ),
    ]
    if candidates:
        lines.append("")
        lines.append("Candidates")
        for candidate in candidates[:3]:
            lines.append(
                f"- {candidate.get('display_id')}: ${candidate.get('total_pnl_usd', 0.0):.2f} "
                f"({candidate.get('closed', 0)} closed)"
            )
    lines.extend(["", "Caveat: shadow legacy stops are paper marks; they overstate slippage vs live fills."])
    if markdown_path is not None:
        lines.extend(["", f"Full report: {markdown_path}"])
    return "\n".join(lines)


def _pnl(bucket: dict[str, Any]) -> str:
    value = bucket.get("total_pnl_usd")
    return f"{value:.2f}" if isinstance(value, (int, float)) else "0.00"


def _relaxed_flag(relaxed: list[str] | None) -> str:
    if relaxed is None:
        return "?"
    if relaxed:
        return f"yes ({len(relaxed)})"
    return "no"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _empty_report(start: date, end: date, exp_start: date, db_path: str) -> dict[str, Any]:
    empty_side = {"trades": 0, "closed": 0, "open": 0, "wins": 0, "total_pnl_usd": 0.0}
    empty_split = {"profile": _bucket([]), "legacy": _bucket([])}
    return {
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "experiment_start": exp_start.isoformat(),
        "db_path": db_path,
        "deployments_supplied": False,
        "headline": {"live": dict(empty_side), "shadow": dict(empty_side), "total": dict(empty_side)},
        "lanes": [],
        "profile_vs_legacy": {
            "live": empty_split,
            "shadow": empty_split,
            "overall": empty_split,
            "caveat": PROFILE_VS_LEGACY_CAVEAT,
        },
        "promotion_candidates": {
            "criteria": {
                "mode": "shadow",
                "min_closed_trades": PROMOTION_MIN_CLOSED_TRADES,
                "positive_total_pnl": True,
                "no_disqualifying_flags": True,
            },
            "candidates": [],
            "near_misses": [],
        },
        "live_cumulative": {"since": exp_start.isoformat(), "by_day": [], "total_pnl_usd": 0.0, "total_trades": 0},
        "data_quality_warnings": [],
        "observation_outcomes": [],
    }


def _coerce_day(value: date | str | None) -> date:
    if value is None:
        return datetime.now(UTC).date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())
