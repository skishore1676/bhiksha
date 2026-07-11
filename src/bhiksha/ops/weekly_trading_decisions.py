"""One Friday decision packet and normalized workbook feed.

The existing weekly scorecard and shadow-EV builders remain internal analytic
components. This module gives them one operator-facing outcome: decide what to
fix, keep observing, or consider promoting. It never mutates trading state.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, TYPE_CHECKING

from bhiksha.ops.shadow_ev_report import build_shadow_ev_report
from bhiksha.ops.weekly_scorecard import (
    _augment_trade,
    _deployment_lookup,
    _load_partials,
    _load_window_trades,
    build_weekly_scorecard,
)

if TYPE_CHECKING:
    from bhiksha.config.models import DeploymentManifest


@dataclass(slots=True, frozen=True)
class WeeklyTradingDecisionsWriteResult:
    report: dict[str, Any]
    json_path: Path
    markdown_path: Path
    facts_path: Path


def write_weekly_trading_decisions(
    db_path: str | Path,
    *,
    output_dir: str | Path,
    week_end: date | str | None = None,
    deployments: list["DeploymentManifest"] | None = None,
) -> WeeklyTradingDecisionsWriteResult:
    end = _coerce_day(week_end)
    start = end - timedelta(days=end.weekday())
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    scorecard = build_weekly_scorecard(
        db_path,
        week_start=start,
        week_end=end,
        deployments=deployments,
    )
    shadow_ev = build_shadow_ev_report(db_path, now=datetime.combine(end, datetime.min.time(), tzinfo=UTC))
    facts = build_trading_decision_export(
        db_path,
        through=end,
        deployments=deployments,
        report_dir=target,
    )
    stable_id = f"bhiksha-weekly-trading-decisions:{end.isoformat()}"
    facts_path = target / f"trading_decision_facts_{end.isoformat()}.json"
    _atomic_json(facts_path, facts)
    report = {
        "schema": "bhiksha.weekly_trading_decisions.v1",
        "artifact_id": stable_id,
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "generated_at": datetime.now(UTC).isoformat(),
        "scorecard": scorecard,
        "shadow_ev": shadow_ev,
        "facts_export": str(facts_path),
        "facts_export_receipt": facts["receipt"],
        "workbook_update": {"status": "pending"},
    }
    stem = f"weekly_trading_decisions_{start.isoformat()}_{end.isoformat()}"
    json_path = target / f"{stem}.json"
    markdown_path = target / f"{stem}.md"
    _atomic_json(json_path, report)
    _atomic_text(markdown_path, render_weekly_trading_decisions_markdown(report))
    return WeeklyTradingDecisionsWriteResult(report, json_path, markdown_path, facts_path)


def finalize_weekly_trading_decisions(
    result: WeeklyTradingDecisionsWriteResult,
    workbook_update: dict[str, Any],
) -> WeeklyTradingDecisionsWriteResult:
    result.report["workbook_update"] = workbook_update
    _atomic_json(result.json_path, result.report)
    _atomic_text(result.markdown_path, render_weekly_trading_decisions_markdown(result.report))
    return result


def build_trading_decision_export(
    db_path: str | Path,
    *,
    through: date,
    deployments: list["DeploymentManifest"] | None,
    report_dir: Path,
) -> dict[str, Any]:
    path = Path(db_path)
    shadow_by_deployment, _relaxed = _deployment_lookup(deployments)
    rows: list[sqlite3.Row] = []
    partials: dict[str, list[dict[str, Any]]] = {}
    if path.exists():
        with closing(sqlite3.connect(path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = _load_window_trades(conn, date(2000, 1, 1), through)
            partials = _load_partials(conn, [str(row["trade_id"]) for row in rows])
    exported_at = datetime.now(UTC).isoformat()
    facts = []
    for raw in rows:
        row = dict(raw)
        trade = _augment_trade(row, partials.get(str(row.get("trade_id")), []), shadow_by_deployment)
        if trade.get("realized_pnl_usd") is None:
            continue
        banked_qty = int(trade.get("original_entry_qty") or 0) - int(row.get("exit_filled_quantity") or row.get("quantity") or 0)
        entry = float(row.get("entry_price") or 0.0)
        exit_price = row.get("exit_price")
        final_qty = int(row.get("exit_filled_quantity") or row.get("quantity") or 0)
        final_pnl = (float(exit_price) - entry) * final_qty * 100 if exit_price is not None else 0.0
        partial_pnl = float(trade["realized_pnl_usd"]) - final_pnl
        source_payload = {key: row.get(key) for key in sorted(row)}
        source_hash = hashlib.sha256(json.dumps(source_payload, sort_keys=True, default=str).encode()).hexdigest()
        deployment_id = str(row.get("deployment_id") or "")
        exit_attribution = str(trade.get("exit_attribution") or "")
        facts.append(
            {
                "trade_id": str(row.get("trade_id")),
                "deployment_id": deployment_id,
                "strategy_key": re.sub(r"_row_\d+$", "", deployment_id),
                "symbol": row.get("symbol"),
                "lane": trade.get("lane"),
                "entry_timestamp": row.get("entry_timestamp"),
                "exit_timestamp": row.get("exit_filled_at"),
                "entry_price": row.get("entry_price"),
                "exit_price": row.get("exit_price"),
                "original_quantity": trade.get("original_entry_qty"),
                "final_quantity": final_qty,
                "banked_quantity": banked_qty,
                "partial_pnl_usd": round(partial_pnl, 2),
                "final_pnl_usd": round(final_pnl, 2),
                "realized_pnl_usd": trade.get("realized_pnl_usd"),
                "cost_basis_usd": trade.get("cost_basis_usd"),
                "return_pct": (float(trade.get("return_pct") or 0.0) / 100.0),
                "exit_attribution": exit_attribution,
                "exit_class": "profile" if exit_attribution.startswith("profile:") else "legacy",
                "data_quality_status": "OK",
                "source_receipt": f"{path.name}#trade_sessions/{row.get('trade_id')}",
                "source_hash": f"sha256:{source_hash}",
                "exported_at": exported_at,
            }
        )
    daily_status = _daily_status_rows(report_dir, through)
    body = {"schema": "bhiksha.trading_decision_facts.v1", "generated_at": exported_at, "facts": facts, "daily_status": daily_status}
    # The receipt identifies evidence, not run time. A retry with unchanged
    # facts must reuse the same digest so the workbook and Obsidian card can be
    # updated idempotently rather than creating weekly churn.
    digest_payload = {
        "schema": body["schema"],
        "facts": [{key: value for key, value in fact.items() if key != "exported_at"} for fact in facts],
        "daily_status": daily_status,
    }
    digest = hashlib.sha256(json.dumps(digest_payload, sort_keys=True, default=str).encode()).hexdigest()
    body["receipt"] = {"status": "ok", "sha256": digest, "fact_count": len(facts), "through": through.isoformat()}
    return body


def render_weekly_trading_decisions_markdown(report: dict[str, Any]) -> str:
    score = report.get("scorecard") or {}
    headline = score.get("headline") or {}
    live = headline.get("live") or {}
    shadow = headline.get("shadow") or {}
    candidates = ((score.get("promotion") or {}).get("candidates") or [])
    near_misses = ((score.get("promotion") or {}).get("near_misses") or [])
    workbook = report.get("workbook_update") or {}
    lines = [
        f"# Weekly Trading Decisions — Performance, Promotions & Fixes — {report.get('week_end')}",
        "",
        f"- artifact: `{report.get('artifact_id')}`",
        f"- workbook update: `{workbook.get('status', 'pending')}`",
        f"- facts: `{(report.get('facts_export_receipt') or {}).get('fact_count', 0)}` through `{report.get('week_end')}`",
        "",
        "## What happened",
        "",
        f"- live: `{live.get('trades', 0)}` trades, `${live.get('total_pnl_usd', 0.0):.2f}`",
        f"- shadow: `{shadow.get('trades', 0)}` trades, `${shadow.get('total_pnl_usd', 0.0):.2f}`",
        "",
        "## Decisions to make",
        "",
    ]
    if candidates:
        for candidate in candidates:
            lines.append(f"- **PROMOTION REVIEW:** `{candidate.get('display_id')}` — {candidate.get('closed', 0)} closed, `${candidate.get('total_pnl_usd', 0.0):.2f}`. Decide promote / observe / reject.")
    else:
        lines.append("- **PROMOTION:** no lane currently clears the visible evidence threshold; no promotion decision is required.")
    if near_misses:
        for lane in near_misses[:5]:
            lines.append(f"- **FIX OR OBSERVE:** `{lane.get('display_id')}` — `{lane.get('disqualified_by')}`.")
    issues = sum((score.get("data_quality") or {}).values()) if isinstance(score.get("data_quality"), dict) else 0
    lines.extend([
        f"- **SYSTEM HEALTH:** `{issues}` scorecard data-quality warnings. Decide whether any issue needs repair before next session.",
        "",
        "## Evidence and math",
        "",
        f"- canonical workbook: `Trading Decision Ledger.xlsx`",
        f"- workbook receipt: `{workbook.get('receipt', workbook.get('error', 'not available'))}`",
        f"- fact export receipt: `{(report.get('facts_export_receipt') or {}).get('sha256', '')}`",
        "",
        "> This report recommends questions, not trades. Promotion, pause, retirement, and risk changes require Suman's explicit decision.",
        "",
    ])
    return "\n".join(lines)


def _daily_status_rows(report_dir: Path, through: date) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(report_dir.glob("trade_session_report_*.json")):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            day = str(report.get("trading_date") or "")
            if not day or day > through.isoformat():
                continue
            provider = (report.get("provider_health") or {}).get("reconciliation") or {}
            rows.append({
                "date": day,
                "risk_event_count": sum((report.get("event_type_counts") or {}).get(key, 0) for key in ("risk_halt", "risk_flatten", "risk_open_drawdown_warning")),
                "operational_issue_count": sum(((report.get("provider_health") or {}).get("runtime_issue_counts") or {}).values()),
                "reconciliation_status": "DEGRADED" if provider.get("degraded_count") else ("WARNING" if provider.get("warning_count") else "OK"),
                "report_status": ((report.get("status") or {}).get("level") or "UNKNOWN"),
            })
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    return rows


def _coerce_day(value: date | str | None) -> date:
    if isinstance(value, date):
        return value
    if value:
        return date.fromisoformat(str(value))
    now = datetime.now(UTC).date()
    return now - timedelta(days=(now.weekday() - 4) % 7)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_text(path, json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def _atomic_text(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
