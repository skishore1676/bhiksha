"""Date-scoped runtime reports for live and shadow trading sessions."""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, TYPE_CHECKING

from bhiksha.ops.reconciliation_supervision import (
    RECONCILIATION_HOLD_STATUS,
    summarize_reconciliation_state,
)
from bhiksha.ops.provider_reconciliation_health import (
    empty_provider_reconciliation,
    summarize_provider_reconciliation,
)
from bhiksha.ops.trade_observation import (
    ENTRY_CANCELLED_UNFILLED,
    MISSING,
    NON_TRADE_OUTCOMES,
    NO_FILL,
    classify_trade_observation,
    index_terminal_entry_observations,
)

if TYPE_CHECKING:
    from bhiksha.config.models import DeploymentManifest

_OPTION_SYMBOL_RE = re.compile(r"^[A-Z]+\d{6}[CP](\d{8})$")

# Matches bhiksha.risk.risk_manager.OPEN_DRAWDOWN_WARNING_REASON. Kept as a
# plain string literal (not an import) to match this file's existing
# convention of not importing bhiksha.risk.risk_manager -- see the
# "risk_manager_startup"/"risk_manager_decision" literals below.
OPEN_DRAWDOWN_WARNING_EVENT_TYPE = "risk_open_drawdown_warning"
SHADOW_ONLY_DIAGNOSTIC_CATEGORIES = frozenset(
    {"exit_state_degraded_protection"}
)


@dataclass(slots=True, frozen=True)
class DailyReportWriteResult:
    report: dict[str, Any]
    json_path: Path
    markdown_path: Path


def write_daily_report(
    db_path: str | Path,
    *,
    output_dir: str | Path,
    trading_date: date | str | None = None,
    deployments: list["DeploymentManifest"] | None = None,
    app_status: dict[str, Any] | None = None,
    schwab_status: dict[str, Any] | None = None,
) -> DailyReportWriteResult:
    report = build_daily_report(db_path, trading_date=trading_date, deployments=deployments)
    # Keep the exact best-effort runtime probe in the canonical JSON. The RYG
    # renderer is only a projection; without this field a later investigation
    # cannot distinguish stopped, running, and probe-failed states.
    report["app_status"] = dict(app_status) if isinstance(app_status, dict) else None
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    day = report["trading_date"]
    json_path = target_dir / f"trade_session_report_{day}.json"
    markdown_path = target_dir / f"trade_session_report_{day}.md"
    ryg_markdown_path = target_dir / f"trade_session_report_{day}_ryg.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_path.write_text(render_daily_report_markdown(report), encoding="utf-8")
    # RYG table (user preference)
    ryg_markdown_path.write_text(
        render_daily_report_ryg_markdown(report, app_status=app_status, schwab_status=schwab_status),
        encoding="utf-8",
    )
    return DailyReportWriteResult(report=report, json_path=json_path, markdown_path=markdown_path)


def build_daily_report(
    db_path: str | Path,
    *,
    trading_date: date | str | None = None,
    deployments: list["DeploymentManifest"] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the date-scoped report.

    ``deployments`` is the OPTIONAL active-plan deployment list (workplan #17):
    when supplied, any deployment carrying ``source.metadata.evidence_gates_relaxed``
    that also traded today is surfaced in ``relaxed_evidence_lanes`` so a
    weak-evidence shadow lane is never promoted by accident. Report-only —
    nothing here reads order placement/sizing/suppression state.
    """
    day = _coerce_day(trading_date)
    path = Path(db_path)
    if not path.exists():
        return _empty_report(day)

    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        events = _load_day_events(conn, day)
        trades = _load_day_trades(conn, day)
        partials_by_trade = _load_confirmed_partial_fills(conn, [trade.get("trade_id") for trade in trades])
        risk_rails = _risk_rails_summary(conn, day, events)

    event_counts = Counter(event["event_type"] for event in events)
    provider_events = _provider_events(events)
    lifecycle_events = _lifecycle_events(events)
    terminal_entry_observations = index_terminal_entry_observations(events)
    augmented_trades: list[dict[str, Any]] = []
    observation_outcomes: list[dict[str, Any]] = []
    for raw_trade in trades:
        trade = _augment_trade(
            raw_trade,
            partials_by_trade.get(str(raw_trade.get("trade_id")), []),
        )
        observation = classify_trade_observation(
            trade,
            terminal_entry_observations,
        )
        if observation is not None:
            trade["observation_outcome"] = observation["observation_outcome"]
            trade["pnl_eligible"] = observation["pnl_eligible"]
            observation_outcomes.append(observation)
        augmented_trades.append(trade)
    trades = augmented_trades
    entry_reconciliation = summarize_reconciliation_state(trades, events, now=now)
    # Entry attempts in reconciliation are not broker positions, and a
    # proved terminal zero-fill attempt is not a trade. Keep both in the
    # reconciliation/observation accounts instead of contaminating position
    # and P&L reporting. A generic release event alone is insufficient: the
    # classifier above must also prove terminal zero/null fill truth.
    trades = [
        trade
        for trade in trades
        if not _is_entry_reconciliation_hold(trade)
        and trade.get("observation_outcome") not in NON_TRADE_OUTCOMES
    ]
    live_trades = [trade for trade in trades if trade["lane"] == "live"]
    shadow_trades = [trade for trade in trades if trade["lane"] == "shadow"]
    live_missing_exit_truth = _missing_exit_truth_count(live_trades)
    shadow_missing_exit_truth = _missing_exit_truth_count(shadow_trades)
    open_positions = [trade for trade in trades if _is_open_trade(trade)]
    live_open_positions = [trade for trade in open_positions if trade["lane"] == "live"]
    shadow_open_positions = [trade for trade in open_positions if trade["lane"] == "shadow"]
    open_position_summary = _open_position_summary(open_positions)
    data_quality_warnings = _data_quality_warnings(trades)
    profile_exit_summary = _profile_exit_summary(trades)
    relaxed_evidence_lanes = _relaxed_evidence_lanes(trades, deployments)
    code_version = None
    for event in events:
        if event["event_type"] == "startup_config":
            code_version = (event["payload"] or {}).get("code_version") or code_version
    (
        runtime_issue_counts,
        suppressed_shadow_runtime_issue_counts,
    ) = _runtime_issue_summaries(
        events,
        shadow_trade_ids={
            str(trade["trade_id"])
            for trade in shadow_trades
            if trade.get("trade_id")
        },
    )
    entry_selector_empty_by_deployment = _entry_selector_empty_by_deployment(
        events, deployments, trades
    )
    entry_profile_comparison = _entry_profile_comparison_summary(events)

    return {
        "trading_date": day.isoformat(),
        "db_path": str(path),
        "code_version": code_version,
        "total_events": len(events),
        "event_type_counts": dict(sorted(event_counts.items())),
        "provider_health": {
            "reconciliation": provider_events,
            "runtime_issue_counts": runtime_issue_counts,
            "suppressed_shadow_runtime_issue_counts": (
                suppressed_shadow_runtime_issue_counts
            ),
            "entry_selector_empty_by_deployment": entry_selector_empty_by_deployment,
        },
        "trade_summary": {
            "live_count": len(live_trades),
            "shadow_count": len(shadow_trades),
            "live_open_count": len(live_open_positions),
            "shadow_open_count": len(shadow_open_positions),
            "total_open_count": len(open_positions),
            "live_realized_pnl_usd": _complete_realized_pnl(live_trades),
            "shadow_realized_pnl_usd": _complete_realized_pnl(shadow_trades),
            "total_realized_pnl_usd": _complete_realized_pnl(trades),
            "live_missing_exit_truth_count": live_missing_exit_truth,
            "shadow_missing_exit_truth_count": shadow_missing_exit_truth,
            "total_missing_exit_truth_count": live_missing_exit_truth + shadow_missing_exit_truth,
            "entry_cancelled_unfilled_count": sum(
                1
                for row in observation_outcomes
                if row.get("observation_outcome") == ENTRY_CANCELLED_UNFILLED
            ),
            "no_fill_count": sum(
                1
                for row in observation_outcomes
                if row.get("observation_outcome") == NO_FILL
            ),
            "missing_observation_count": sum(
                1
                for row in observation_outcomes
                if row.get("observation_outcome") == MISSING
            ),
        },
        "open_position_summary": open_position_summary,
        "open_positions": open_positions,
        "entry_reconciliation": entry_reconciliation,
        "trades": trades,
        "observation_outcomes": observation_outcomes,
        "lifecycle": lifecycle_events,
        "data_quality_warnings": data_quality_warnings,
        "profile_exit_summary": profile_exit_summary,
        "entry_profile_comparison": entry_profile_comparison,
        "relaxed_evidence_lanes": relaxed_evidence_lanes,
        "risk_rails": risk_rails,
        "status": _report_status(
            provider_events=provider_events,
            data_quality_warnings=data_quality_warnings,
            runtime_issue_counts=runtime_issue_counts,
            open_positions=open_positions,
            entry_reconciliation=entry_reconciliation,
        ),
    }


def render_daily_report_markdown(report: dict[str, Any]) -> str:
    summary = report.get("trade_summary") or {}
    status = report.get("status") or {}
    provider = ((report.get("provider_health") or {}).get("reconciliation") or {})
    entry_reconciliation = report.get("entry_reconciliation") or {}
    lines = [
        f"# Bhiksha Trade Session - {report.get('trading_date')}",
        "",
        f"- status: `{status.get('level', 'UNKNOWN')}`",
        f"- live trades: `{summary.get('live_count', 0)}`",
        f"- shadow trades: `{summary.get('shadow_count', 0)}`",
        f"- open live positions: `{summary.get('live_open_count', 0)}`",
        f"- open shadow positions: `{summary.get('shadow_open_count', 0)}`",
        f"- live realized P&L: `{_summary_pnl_text(summary, 'live')}`",
        f"- shadow realized P&L: `{_summary_pnl_text(summary, 'shadow')}`",
        f"- total realized P&L: `{_summary_pnl_text(summary, 'total')}`",
        (
            "- non-trade entry outcomes: "
            f"`cancelled unfilled {summary.get('entry_cancelled_unfilled_count', 0)}, "
            f"other no-fill {summary.get('no_fill_count', 0)}`"
        ),
        f"- provider reconciliation state: `{provider.get('state', 'healthy')}`",
        (
            "- active provider reconciliation: "
            f"`warn {provider.get('active_warning_count', 0)}, "
            f"degraded {provider.get('active_degraded_count', 0)}, "
            f"blocking {provider.get('active_blocking_count', 0)}`"
        ),
        (
            "- observed provider reconciliation: "
            f"`warn {provider.get('warning_count', 0)}, "
            f"degraded {provider.get('degraded_count', 0)}, "
            f"blocking {provider.get('blocking_count', 0)}, "
            f"recovered {provider.get('recovered_count', 0)}`"
        ),
        (
            "- entry reconciliation: "
            f"`recovered {entry_reconciliation.get('recovered_count', 0)}, "
            f"self-healing {entry_reconciliation.get('self_healing_count', 0)}, "
            f"needs you {entry_reconciliation.get('needs_human_count', 0)}`"
        ),
        f"- data-quality warnings: `{len(report.get('data_quality_warnings') or [])}`",
    ]
    profile_exits = report.get("profile_exit_summary") or {}
    if profile_exits.get("count"):
        rules = ", ".join(sorted(profile_exits.get("rule_counts") or {}))
        lines.append(f"- profile exits: `{profile_exits['count']}` ({rules})")
    open_summary = report.get("open_position_summary") or {}
    if any(
        open_summary.get(key, 0)
        for key in ("protected_count", "target_active_count", "unprotected_count", "exit_pending_count")
    ):
        lines.append(
            "- open protection: "
            f"`protected {open_summary.get('protected_count', 0)}, "
            f"target active {open_summary.get('target_active_count', 0)}, "
            f"unprotected {open_summary.get('unprotected_count', 0)}, "
            f"exit pending {open_summary.get('exit_pending_count', 0)}`"
        )
    if status.get("reason"):
        lines.append(f"- reason: `{status['reason']}`")
    code_version = report.get("code_version") or {}
    if code_version.get("git_commit"):
        dirty_suffix = " (dirty)" if code_version.get("git_dirty") else ""
        lines.append(f"- code: `{str(code_version['git_commit'])[:12]}{dirty_suffix}`")

    open_positions = report.get("open_positions") or []
    if open_positions:
        lines.extend(["", "## Open Positions", "", "| Lane | Symbol | Option | Qty | Status | Entry | Stop | Target | Protection |", "|---|---|---|---:|---|---:|---:|---:|---|"])
        for position in open_positions:
            lines.append(
                "| {lane} | {symbol} | {option} | {qty} | {status} | {entry} | {stop} | {target} | {protection} |".format(
                    lane=position.get("lane", ""),
                    symbol=position.get("symbol", ""),
                    option=position.get("option_symbol") or "",
                    qty=position.get("qty_label", position.get("quantity") or 0),
                    status=position.get("status") or "",
                    entry=_fmt_money(position.get("entry_price")),
                    stop=_fmt_money(position.get("stop_price")),
                    target=_fmt_money(position.get("target_price")),
                    protection=position.get("protection_state") or "",
                )
            )

    active_holds = entry_reconciliation.get("active_holds") or []
    recoveries = entry_reconciliation.get("recoveries") or []
    if active_holds or recoveries:
        lines.extend(["", "## Entry Reconciliation"])
        for hold in active_holds:
            lines.append(
                f"- `{hold.get('symbol')}` `{hold.get('deployment_id')}`: "
                f"`{hold.get('state')}` for `{hold.get('age_seconds')}`s; "
                f"order `{hold.get('entry_order_id')}`; affected deployment is blocked"
            )
        for recovery in recoveries:
            duration = recovery.get("duration_seconds")
            duration_text = f" in {duration}s" if duration is not None else ""
            lines.append(
                f"- `{recovery.get('symbol')}` `{recovery.get('deployment_id')}`: "
                f"`{recovery.get('action')}`{duration_text}; no human action required"
            )

    trades = report.get("trades") or []
    if trades:
        lines.extend(
            [
                "",
                "## Trades",
                "",
                "| Lane | Symbol | Option | Qty | Entry | Exit Px | Exit | P&L | Status |",
                "|---|---|---|---:|---:|---:|---|---:|---|",
            ]
        )
        for trade in trades:
            lines.append(
                "| {lane} | {symbol} | {option} | {qty} | {entry} | {exit_px} | {exit} | {pnl} | {status} |".format(
                    lane=trade.get("lane", ""),
                    symbol=trade.get("symbol", ""),
                    option=trade.get("option_symbol") or "",
                    qty=trade.get("qty_label", trade.get("quantity") or 0),
                    entry=_fmt_money(trade.get("entry_price")),
                    exit_px=_fmt_money(trade.get("exit_price")),
                    exit=trade.get("exit_attribution") or "",
                    pnl=_fmt_money(trade.get("realized_pnl_usd")),
                    status=trade.get("status") or "",
                )
            )

    lifecycle = report.get("lifecycle") or {}
    if lifecycle:
        lines.extend(["", "## Lifecycle"])
        for key, value in sorted(lifecycle.items()):
            lines.append(f"- `{key}`: `{value}`")

    warnings = report.get("data_quality_warnings") or []
    if warnings:
        lines.extend(["", "## Data Quality Warnings"])
        for warning in warnings:
            lines.append(f"- `{warning.get('symbol')}` `{warning.get('option_symbol')}`: {warning.get('message')}")

    relaxed_lanes = report.get("relaxed_evidence_lanes") or []
    if relaxed_lanes:
        lines.extend(["", "## Shadow Lanes on Relaxed Evidence"])
        for lane in relaxed_lanes:
            gates = ", ".join(lane.get("evidence_gates_relaxed") or [])
            lines.append(f"- `{lane.get('deployment_id')}` [{gates}]")

    profile_comparisons = report.get("entry_profile_comparison") or []
    if profile_comparisons:
        lines.extend(
            [
                "",
                "## Entry Profile Quote Comparison",
                "",
                "| Lane | Active | Candidate | Samples | Blocked | Avg savings vs ask | Avg fraction |",
                "|---|---|---|---:|---:|---:|---:|",
            ]
        )
        for lane in profile_comparisons:
            for profile_name, profile in sorted((lane.get("profiles") or {}).items()):
                lines.append(
                    "| {lane} | {active} | {profile_name} | {samples} | {blocked} | {savings} | {fraction} |".format(
                        lane=lane.get("deployment_id") or "",
                        active=lane.get("active_profile") or "legacy",
                        profile_name=profile_name,
                        samples=profile.get("sample_count", 0),
                        blocked=profile.get("blocked_count", 0),
                        savings=_fmt_money(profile.get("average_savings_vs_ask")),
                        fraction=_fmt_decimal(profile.get("average_effective_spread_fraction")),
                    )
                )

    runtime_issue_counts = ((report.get("provider_health") or {}).get("runtime_issue_counts") or {})
    if runtime_issue_counts:
        lines.extend(["", "## Runtime Issues"])
        for category, count in sorted(runtime_issue_counts.items()):
            lines.append(f"- `{category}`: `{count}`")
    suppressed_shadow_runtime_issue_counts = (
        (report.get("provider_health") or {}).get(
            "suppressed_shadow_runtime_issue_counts"
        )
        or {}
    )
    if suppressed_shadow_runtime_issue_counts:
        lines.extend(
            [
                "",
                "## Shadow-only Diagnostics (excluded from operational health)",
            ]
        )
        for category, count in sorted(
            suppressed_shadow_runtime_issue_counts.items()
        ):
            lines.append(f"- `{category}`: `{count}`")

    # Item C (2026-07-08 hygiene batch): a filtered-out live signal (selector
    # returned zero contracts) must never masquerade as a quiet day -- see the
    # 2026-07-07 SMH case.
    selector_empty_lanes = ((report.get("provider_health") or {}).get("entry_selector_empty_by_deployment") or [])
    if selector_empty_lanes:
        lines.extend(["", "## Entry Selector Empty (per lane)"])
        for lane in selector_empty_lanes:
            marker = "LIVE" if lane.get("live") else "shadow"
            diagnostics = lane.get("latest_diagnostics") or {}
            requested = (
                f"{diagnostics.get('requested_dte_min')}-{diagnostics.get('requested_dte_max')}"
                if diagnostics.get("requested_dte_min") is not None
                else "unknown"
            )
            nearest = diagnostics.get("nearest_after_dte")
            fallback = diagnostics.get("dte_fallback_policy") or "strict"
            lines.append(
                f"- `{lane.get('deployment_id')}` [{marker}]: `{lane.get('count')}`; "
                f"outcome `{lane.get('outcome')}`; requested DTE `{requested}`; "
                f"fallback `{fallback}`; nearest-after `{nearest}`; "
                f"rejections `{lane.get('latest_rejections') or {}}`"
            )

    # --- Risk rails section (operator-audit P3) ---------------------------
    risk_rails = report.get("risk_rails")
    if risk_rails:
        lines.extend(["", "## Risk Rails"])
        lines.append(
            "- rails: "
            f"`rail-A(halt) {'on' if risk_rails.get('rail_a_enabled') else 'off'}, "
            f"rail-B(session veto) {'on' if risk_rails.get('rail_b_enabled') else 'off'}`"
        )
        usable_budget = risk_rails.get("usable_budget_usd")
        budget_text = f"${usable_budget:,.2f}" if usable_budget is not None else "unknown"
        lines.append(f"- usable budget: `{budget_text}`")
        lines.append(
            "- tier-1 halt (new entries): "
            f"`{_fmt_pct(risk_rails.get('max_daily_drawdown_pct'))} "
            f"({_fmt_money_or_na(risk_rails.get('max_daily_drawdown_usd'))})`"
        )
        lines.append(
            "- tier-2 flatten (book): "
            f"`{_fmt_pct(risk_rails.get('flatten_daily_drawdown_pct'))} "
            f"({_fmt_money_or_na(risk_rails.get('flatten_daily_drawdown_usd'))})`"
        )
        lines.append(
            "- session entry veto: "
            f"`window {risk_rails.get('demote_window')}, "
            f"min_n {risk_rails.get('demote_min_n')}, "
            f"threshold {_fmt_money_or_na(risk_rails.get('demote_threshold_usd'))}`"
        )
        prospective_enabled = risk_rails.get("prospective_loss_enabled")
        prospective_text = (
            "unknown"
            if prospective_enabled is None
            else ("on" if prospective_enabled else "off")
        )
        cluster_cap = risk_rails.get("max_open_positions_per_cluster")
        lines.append(
            "- sized-entry defenses: "
            f"`prospective loss {prospective_text}, "
            f"cluster cap {cluster_cap if cluster_cap is not None else 'unknown'}`"
        )
        sized_entry_blocks = risk_rails.get("sized_entry_blocks") or []
        if sized_entry_blocks:
            lines.append(f"- sized entries blocked: `{len(sized_entry_blocks)}`")
            for blocked in sized_entry_blocks:
                lines.append(
                    f"  - `{blocked.get('deployment_id')}`: `{blocked.get('reason')}`"
                )
        # Operator audit P4 (2026-07-06): open-book mark-to-market WARNING
        # (never a halt/flatten -- see RiskManager.OpenDrawdownStatus).
        lines.append(
            "- open-book MTM warn (awareness only): "
            f"`{_fmt_pct(risk_rails.get('open_drawdown_warn_pct'))} "
            f"({_fmt_money_or_na(risk_rails.get('open_drawdown_warn_usd'))})`"
        )
        open_drawdown_warnings = risk_rails.get("open_drawdown_warnings") or []
        if open_drawdown_warnings:
            lines.append(f"- open-book MTM warnings fired: `{len(open_drawdown_warnings)}`")
            for warning in open_drawdown_warnings:
                lines.append(
                    "  - day MTM "
                    f"{_fmt_money_or_na(warning.get('day_mtm_usd'))} "
                    f"(realized {_fmt_money_or_na(warning.get('realized_usd'))} + "
                    f"unrealized open {_fmt_money_or_na(warning.get('unrealized_open_usd'))} "
                    f"across {warning.get('open_position_count')} position(s)) "
                    f"<= threshold {_fmt_money_or_na(warning.get('warn_threshold_usd'))}"
                )
        rail_warnings = risk_rails.get("validation_warnings") or []
        if rail_warnings:
            lines.append(f"- validation warnings: `{len(rail_warnings)}`")
            for warning in rail_warnings:
                lines.append(f"  - {warning}")
    # --- end risk rails section ---------------------------------------------

    return "\n".join(lines) + "\n"


def render_daily_report_telegram_summary(
    report: dict[str, Any],
    *,
    markdown_path: str | Path | None = None,
) -> str:
    summary = report.get("trade_summary") or {}
    status = report.get("status") or {}
    provider = ((report.get("provider_health") or {}).get("reconciliation") or {})
    warnings = report.get("data_quality_warnings") or []
    trades = report.get("trades") or []
    open_summary = report.get("open_position_summary") or {}
    open_positions = report.get("open_positions") or []
    entry_reconciliation = report.get("entry_reconciliation") or {}
    lines = [
        f"Bhiksha Session Report - {report.get('trading_date')}",
        "",
        "Quick read",
        f"- Status: {status.get('level', 'UNKNOWN')} ({status.get('reason', 'ok')})",
        f"- Open: live {summary.get('live_open_count', 0)}, shadow {summary.get('shadow_open_count', 0)}",
        (
            "- Protection: "
            f"protected {open_summary.get('protected_count', 0)}, "
            f"target active {open_summary.get('target_active_count', 0)}, "
            f"unprotected {open_summary.get('unprotected_count', 0)}, "
            f"exit pending {open_summary.get('exit_pending_count', 0)}"
        ),
        (
            "- P&L: "
            f"live {_summary_pnl_text(summary, 'live')} "
            f"({summary.get('live_count', 0)} trades), "
            f"shadow {_summary_pnl_text(summary, 'shadow')} "
            f"({summary.get('shadow_count', 0)} trades)"
        ),
        (
            "- Reconciliation: "
            f"{provider.get('state', 'healthy')}; "
            f"active warn {provider.get('active_warning_count', 0)}, "
            f"degraded {provider.get('active_degraded_count', 0)}, "
            f"blocking {provider.get('active_blocking_count', 0)}; "
            f"observed warn {provider.get('warning_count', 0)}, "
            f"recovered {provider.get('recovered_count', 0)}"
        ),
        (
            "- Entry holds: "
            f"self-healing {entry_reconciliation.get('self_healing_count', 0)}, "
            f"recovered {entry_reconciliation.get('recovered_count', 0)}, "
            f"needs you {entry_reconciliation.get('needs_human_count', 0)}"
        ),
    ]
    watch_items: list[str] = []
    profile_exits = report.get("profile_exit_summary") or {}
    if profile_exits.get("count"):
        rules = ", ".join(sorted(profile_exits.get("rule_counts") or {}))
        watch_items.append(f"Profile exits: {profile_exits['count']} ({rules})")
    relaxed_lanes = report.get("relaxed_evidence_lanes") or []
    for lane in relaxed_lanes:
        gate_count = len(lane.get("evidence_gates_relaxed") or [])
        gate_text = f"{gate_count} gate" if gate_count == 1 else f"{gate_count} gates"
        watch_items.append(f"Relaxed shadow evidence: {_compact_deployment_id(lane.get('deployment_id'))} ({gate_text})")
    if open_positions:
        lines.extend(["", "Open positions"])
        for position in open_positions[:3]:
            lines.append(
                "- {lane} {symbol} qty {qty}: entry {entry}, stop {stop}, target {target}, {protection}".format(
                    lane=str(position.get("lane", "")).upper(),
                    symbol=position.get("symbol", ""),
                    qty=position.get("quantity") or 0,
                    entry=_fmt_money(position.get("entry_price")) or "?",
                    stop=_fmt_money(position.get("stop_price")) or "?",
                    target=_fmt_money(position.get("target_price")) or "?",
                    protection=position.get("protection_state") or "unknown",
                )
            )
        if len(open_positions) > 3:
            lines.append(f"- +{len(open_positions) - 3} more open")
    else:
        lines.extend(["", "Open positions", "- None"])
    if warnings:
        first = warnings[0]
        more = len(warnings) - 1
        suffix = f" +{more} more" if more else ""
        watch_items.append(
            "Data quality: "
            f"{len(warnings)} warning(s); first={first.get('symbol')} "
            f"{_compact_warning_message(first.get('message'))}{suffix}"
        )
    needs_human_holds = [
        hold for hold in (entry_reconciliation.get("active_holds") or []) if hold.get("human_action_required")
    ]
    if needs_human_holds:
        lane_text = ", ".join(
            f"{hold.get('symbol')} ({_compact_deployment_id(hold.get('deployment_id'))})"
            for hold in needs_human_holds[:3]
        )
        watch_items.append(f"NEEDS YOU - unresolved entry reconciliation: {lane_text}")
    runtime_issues = (((report.get("provider_health") or {}).get("runtime_issue_counts")) or {})
    if runtime_issues:
        issue_text = ", ".join(f"{key} x{value}" for key, value in list(runtime_issues.items())[:3])
        more = len(runtime_issues) - 3
        if more > 0:
            issue_text = f"{issue_text}, +{more} more"
        watch_items.append(f"Runtime issue: {issue_text}")
    selector_empty_lanes = ((report.get("provider_health") or {}).get("entry_selector_empty_by_deployment") or [])
    live_selector_empty_lanes = [
        lane
        for lane in selector_empty_lanes
        if lane.get("live") and lane.get("outcome") != "selected_later"
    ]
    if live_selector_empty_lanes:
        # Item C: surface LIVE lanes distinctly so a filtered-out live signal
        # (2026-07-07 SMH) is never buried behind shadow-lane noise.
        lane_text = ", ".join(
            f"{_compact_deployment_id(lane.get('deployment_id'))} x{lane.get('count')}"
            for lane in live_selector_empty_lanes[:3]
        )
        more = len(live_selector_empty_lanes) - 3
        if more > 0:
            lane_text = f"{lane_text}, +{more} more"
        watch_items.append(f"LIVE entry_selector_empty: {lane_text}")
    risk_rails = report.get("risk_rails")
    if risk_rails:
        watch_items.append(
            "Risk rails: "
            f"halt {_fmt_pct(risk_rails.get('max_daily_drawdown_pct'))} "
            f"({_fmt_money_or_na(risk_rails.get('max_daily_drawdown_usd'))}), "
            f"flatten {_fmt_pct(risk_rails.get('flatten_daily_drawdown_pct'))} "
            f"({_fmt_money_or_na(risk_rails.get('flatten_daily_drawdown_usd'))})"
        )
    closed_trades = [item for item in trades if not _is_open_trade(item)]
    if closed_trades:
        lines.extend(["", "Recent closes"])
        for trade in closed_trades[:3]:
            lines.append(
                "- {lane} {symbol} qty {qty}: {entry}->{exit}, P&L {pnl}".format(
                    lane=str(trade.get("lane", "")).upper(),
                    symbol=trade.get("symbol", ""),
                    qty=trade.get("quantity") or 0,
                    entry=_fmt_money(trade.get("entry_price")) or "?",
                    exit=_fmt_money(trade.get("exit_price")) or "?",
                    pnl=_pnl_text(trade.get("realized_pnl_usd")),
                )
            )
        if len(closed_trades) > 3:
            lines.append(f"- +{len(closed_trades) - 3} more closed in report")
    if watch_items:
        lines.extend(["", "Watch"])
        lines.extend(f"- {item}" for item in watch_items[:5])
        if len(watch_items) > 5:
            lines.append(f"- +{len(watch_items) - 5} more watch item(s)")
    if markdown_path is not None:
        lines.extend(["", f"Full report: {markdown_path}"])
    return "\n".join(lines)


def _compact_deployment_id(value: Any) -> str:
    deployment_id = str(value or "unknown")
    prefixes = (
        "strategy_market_impulse_all_basket_discovery_",
        "strategy_market_impulse_",
        "strategy_",
    )
    for prefix in prefixes:
        if deployment_id.startswith(prefix):
            deployment_id = deployment_id.removeprefix(prefix)
            break
    if len(deployment_id) <= 48:
        return deployment_id
    return f"{deployment_id[:45]}..."


def _compact_warning_message(value: Any) -> str:
    message = str(value or "warning")
    if len(message) <= 72:
        return message
    return f"{message[:69]}..."


def _runtime_issue_summaries(
    events: list[dict[str, Any]],
    *,
    shadow_trade_ids: set[str],
) -> tuple[dict[str, int], dict[str, int]]:
    """Separate known paper-only diagnostics from operational health.

    Historical events remain counted in the explicit suppressed bucket.  Only
    the exact live-protection category is eligible, and only when the trade is
    independently proved paper by its trade row, shadow-entry event, or source.
    """

    proved_shadow_ids = set(shadow_trade_ids)
    for event in events:
        payload = event.get("payload") or {}
        trade_id = _maybe_str(payload.get("trade_id"))
        if trade_id and (
            event.get("event_type") == "shadow_entry_assumed"
            or _maybe_str(payload.get("source")) in {"shadow", "dry_run"}
        ):
            proved_shadow_ids.add(trade_id)

    actionable: Counter[str] = Counter()
    suppressed: Counter[str] = Counter()
    for event in events:
        if event.get("event_type") != "runtime_issue":
            continue
        payload = event.get("payload") or {}
        category = _maybe_str(payload.get("category")) or "exception"
        trade_id = _maybe_str(payload.get("trade_id"))
        if (
            category in SHADOW_ONLY_DIAGNOSTIC_CATEGORIES
            and trade_id in proved_shadow_ids
        ):
            suppressed[category] += 1
        else:
            actionable[category] += 1
    return dict(sorted(actionable.items())), dict(sorted(suppressed.items()))


def _empty_report(day: date) -> dict[str, Any]:
    return {
        "trading_date": day.isoformat(),
        "db_path": "",
        "total_events": 0,
        "event_type_counts": {},
        "provider_health": {
            "reconciliation": _empty_reconciliation(),
            "runtime_issue_counts": {},
            "suppressed_shadow_runtime_issue_counts": {},
            "entry_selector_empty_by_deployment": [],
        },
        "trade_summary": {
            "live_count": 0,
            "shadow_count": 0,
            "live_realized_pnl_usd": 0.0,
            "shadow_realized_pnl_usd": 0.0,
            "total_realized_pnl_usd": 0.0,
            "live_missing_exit_truth_count": 0,
            "shadow_missing_exit_truth_count": 0,
            "total_missing_exit_truth_count": 0,
            "live_open_count": 0,
            "shadow_open_count": 0,
            "total_open_count": 0,
            "entry_cancelled_unfilled_count": 0,
            "no_fill_count": 0,
            "missing_observation_count": 0,
        },
        "open_position_summary": _open_position_summary([]),
        "open_positions": [],
        "entry_reconciliation": {
            "state": "no_data",
            "attention_required": False,
            "active_count": 0,
            "self_healing_count": 0,
            "needs_human_count": 0,
            "recovered_count": 0,
            "active_holds": [],
            "recoveries": [],
            "released_no_fill_trade_ids": [],
        },
        "trades": [],
        "observation_outcomes": [],
        "lifecycle": {},
        "data_quality_warnings": [],
        "profile_exit_summary": {"count": 0, "rule_counts": {}},
        "entry_profile_comparison": [],
        "relaxed_evidence_lanes": [],
        "risk_rails": None,
        "status": {"level": "NO_DATA", "reason": "db_missing", "attention_required": True},
    }


def _load_day_events(conn: sqlite3.Connection, day: date) -> list[dict[str, Any]]:
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "events" not in tables:
        return []
    start = f"{day.isoformat()}T00:00:00"
    end = f"{day.isoformat()}T99:99:99"
    rows = conn.execute(
        "SELECT id, created_at, event_type, payload FROM events WHERE created_at >= ? AND created_at <= ? ORDER BY id",
        (start, end),
    ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        payload = _safe_json(row["payload"])
        events.append(
            {
                "event_id": row["id"],
                "created_at": row["created_at"],
                "event_type": row["event_type"],
                "payload": payload,
            }
        )
    return events


def _entry_profile_comparison_summary(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lanes: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("event_type") != "trade_plan":
            continue
        payload = event.get("payload") or {}
        pricing = (payload.get("risk_details") or {}).get("entry_pricing") or {}
        comparison = pricing.get("initial_profile_comparison") or {}
        deployment_id = str(payload.get("deployment_id") or "")
        if not deployment_id or not isinstance(comparison, dict):
            continue
        lane = lanes.setdefault(
            deployment_id,
            {
                "deployment_id": deployment_id,
                "symbol": payload.get("symbol"),
                "active_profile_counts": Counter(),
                "profiles": {},
            },
        )
        lane["active_profile_counts"][str(pricing.get("entry_execution_profile") or "legacy")] += 1
        for profile_name, evidence in comparison.items():
            if not isinstance(evidence, dict):
                continue
            profile = lane["profiles"].setdefault(
                str(profile_name),
                {"sample_count": 0, "blocked_count": 0, "savings": [], "fractions": []},
            )
            profile["sample_count"] += 1
            if evidence.get("block_reasons"):
                profile["blocked_count"] += 1
            savings = _maybe_float(evidence.get("savings_vs_ask"))
            if savings is not None:
                profile["savings"].append(savings)
            fraction = _maybe_float(evidence.get("effective_spread_fraction"))
            if fraction is not None:
                profile["fractions"].append(fraction)

    result: list[dict[str, Any]] = []
    for deployment_id, lane in sorted(lanes.items()):
        profiles = {}
        for profile_name, profile in sorted(lane["profiles"].items()):
            profiles[profile_name] = {
                "sample_count": profile["sample_count"],
                "blocked_count": profile["blocked_count"],
                "average_savings_vs_ask": _average_or_none(profile["savings"]),
                "average_effective_spread_fraction": _average_or_none(profile["fractions"], digits=4),
            }
        active_counts = lane["active_profile_counts"]
        active_profile = active_counts.most_common(1)[0][0] if active_counts else "legacy"
        result.append(
            {
                "deployment_id": deployment_id,
                "symbol": lane["symbol"],
                "active_profile": active_profile,
                "active_profile_counts": dict(sorted(active_counts.items())),
                "profiles": profiles,
            }
        )
    return result


# --- Risk rails section (operator-audit P3) -------------------------------
# Reads the day's resolved risk-manager thresholds from the
# ``risk_manager_startup`` event (payload == ``RiskSettings.to_dict()``,
# emitted once at session start by ``RiskManager.startup_log``) and renders
# pct thresholds as $ amounts by joining the day's ``cash_budget_days`` row
# (``usable_budget``). Self-contained: safe to read as one block when
# resolving merge conflicts against other daily_report.py edits.
def _risk_rails_summary(conn: sqlite3.Connection, day: date, events: list[dict[str, Any]]) -> dict[str, Any] | None:
    settings_payload: dict[str, Any] | None = None
    for event in events:
        if event["event_type"] == "risk_manager_startup":
            settings_payload = event["payload"] or {}
    if settings_payload is None:
        return None

    usable_budget = _load_day_usable_budget(conn, day)
    max_dd_pct = _maybe_float(settings_payload.get("max_daily_drawdown_pct"))
    flatten_dd_pct = _maybe_float(settings_payload.get("flatten_daily_drawdown_pct"))
    # Operator audit P4 (2026-07-06): open_drawdown_warn_pct defaults to None
    # in the raw RiskSettings payload when unset -- RiskManager falls back to
    # max_daily_drawdown_pct at point-of-use (effective_open_drawdown_warn_pct),
    # so mirror that same fallback here for display rather than showing a
    # blank/"unknown" threshold when nothing was explicitly configured.
    open_dd_warn_pct = _maybe_float(settings_payload.get("open_drawdown_warn_pct"))
    if open_dd_warn_pct is None:
        open_dd_warn_pct = max_dd_pct

    def _pct_to_usd(pct: float | None) -> float | None:
        if pct is None or usable_budget is None:
            return None
        return _round_money((pct / 100.0) * usable_budget)

    # Operator audit P4: the day's open-book mark-to-market WARNING events
    # (risk_open_drawdown_warning), if any fired this session -- see
    # RiskManager._compute_open_drawdown_status / _book_actions_uncached.
    # This is warning-only (never a halt/flatten): surfaced for visibility,
    # not as an actionable rail toggle.
    open_drawdown_warnings = [
        event["payload"] or {} for event in events if event["event_type"] == OPEN_DRAWDOWN_WARNING_EVENT_TYPE
    ]
    sized_entry_blocks = [
        event["payload"] or {}
        for event in events
        if event["event_type"] == "risk_manager_sized_entry_decision"
        and (event["payload"] or {}).get("decision") == "blocked"
    ]

    return {
        "rail_a_enabled": bool(settings_payload.get("rail_a_enabled", True)),
        "rail_b_enabled": bool(settings_payload.get("rail_b_enabled", True)),
        "usable_budget_usd": _round_money(usable_budget) if usable_budget is not None else None,
        "max_daily_drawdown_pct": max_dd_pct,
        "max_daily_drawdown_usd": _pct_to_usd(max_dd_pct),
        "flatten_daily_drawdown_pct": flatten_dd_pct,
        "flatten_daily_drawdown_usd": _pct_to_usd(flatten_dd_pct),
        "demote_window": _maybe_int(settings_payload.get("demote_window")),
        "demote_min_n": _maybe_int(settings_payload.get("demote_min_n")),
        "demote_threshold_usd": _maybe_float(settings_payload.get("demote_threshold_usd")),
        "prospective_loss_enabled": (
            bool(settings_payload["prospective_loss_enabled"])
            if "prospective_loss_enabled" in settings_payload
            else None
        ),
        "max_open_positions_per_cluster": _maybe_int(
            settings_payload.get("max_open_positions_per_cluster")
        ),
        "sized_entry_blocks": sized_entry_blocks,
        "open_drawdown_warn_pct": open_dd_warn_pct,
        "open_drawdown_warn_usd": _pct_to_usd(open_dd_warn_pct),
        "open_drawdown_warnings": open_drawdown_warnings,
        "validation_warnings": list(settings_payload.get("validation_warnings") or []),
    }


def _load_day_usable_budget(conn: sqlite3.Connection, day: date) -> float | None:
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "cash_budget_days" not in tables:
        return None
    row = conn.execute(
        "SELECT usable_budget FROM cash_budget_days WHERE trade_date = ?",
        (day.isoformat(),),
    ).fetchone()
    return _maybe_float(row["usable_budget"]) if row is not None else None
# --- end risk rails section -------------------------------------------------


def _load_day_trades(conn: sqlite3.Connection, day: date) -> list[dict[str, Any]]:
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
        "updated_at",
    ]
    selected = [column for column in desired if column in columns]
    day_text = day.isoformat()
    predicates: list[str] = []
    params: list[str] = []
    if "entry_timestamp" in columns:
        predicates.append("substr(replace(COALESCE(entry_timestamp, ''), ' ', 'T'), 1, 10) = ?")
        params.append(day_text)
    if "exit_filled_at" in columns:
        predicates.append("substr(replace(COALESCE(exit_filled_at, ''), ' ', 'T'), 1, 10) = ?")
        params.append(day_text)
    if "status" in columns:
        predicates.append("status != 'closed'")
    if not predicates:
        return []
    order_columns = [column for column in ("entry_timestamp", "exit_filled_at", "trade_id") if column in columns]
    order_expr = f"COALESCE({', '.join(order_columns)})" if len(order_columns) > 1 else order_columns[0]
    rows = conn.execute(
        f"""
        SELECT {", ".join(selected)}
        FROM trade_sessions
        WHERE {" OR ".join(predicates)}
        ORDER BY {order_expr}
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _load_confirmed_partial_fills(
    conn: sqlite3.Connection,
    trade_ids: list[str | None],
) -> dict[str, list[dict[str, Any]]]:
    ids = [str(trade_id) for trade_id in trade_ids if trade_id]
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if not ids or "trade_partial_fills" not in tables:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT trade_id, closed_quantity, fill_quantity, fill_price, abandoned_reason
        FROM trade_partial_fills
        WHERE trade_id IN ({placeholders})
        ORDER BY id
        """,
        ids,
    ).fetchall()
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        result.setdefault(str(row["trade_id"]), []).append(dict(row))
    return result


def _augment_trade(trade: dict[str, Any], partials: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    entry = _maybe_float(trade.get("entry_price"))
    exit_price = _maybe_float(trade.get("exit_price"))
    quantity = _maybe_int(trade.get("exit_filled_quantity")) or _maybe_int(trade.get("quantity")) or 0
    partial_pnl = 0.0
    banked_quantity = 0
    for partial in partials or []:
        if partial.get("abandoned_reason"):
            continue
        fill_price = _maybe_float(partial.get("fill_price"))
        fill_quantity = _maybe_int(partial.get("fill_quantity")) or _maybe_int(partial.get("closed_quantity")) or 0
        if entry is None or fill_price is None or fill_quantity <= 0:
            continue
        partial_pnl += (fill_price - entry) * fill_quantity * 100
        banked_quantity += fill_quantity
    realized = None
    if entry is not None and ((exit_price is not None and quantity) or banked_quantity):
        final_pnl = (exit_price - entry) * quantity * 100 if exit_price is not None and quantity else 0.0
        realized = _round_money(final_pnl + partial_pnl)
    lane = "shadow" if _is_shadow_trade(trade) else "live"
    return {
        **trade,
        "lane": lane,
        "realized_pnl_usd": realized,
        "banked_partial_pnl_usd": _round_money(partial_pnl),
        "banked_quantity": banked_quantity,
        "option_strike": _parse_option_strike(_maybe_str(trade.get("option_symbol"))),
        "protection_state": _protection_state(trade),
        "exit_attribution": _exit_attribution(trade),
        "qty_label": _format_qty_label(_maybe_int(trade.get("quantity")), trade.get("can_ladder")),
    }


def _missing_exit_truth_count(trades: list[dict[str, Any]]) -> int:
    return sum(
        1
        for trade in trades
        if trade.get("observation_outcome") not in NON_TRADE_OUTCOMES
        and not _is_open_trade(trade)
        and _maybe_float(trade.get("entry_price")) is not None
        and (_maybe_int(trade.get("quantity")) or 0) > 0
        and _maybe_float(trade.get("realized_pnl_usd")) is None
    )


def _complete_realized_pnl(trades: list[dict[str, Any]]) -> float | None:
    if _missing_exit_truth_count(trades):
        return None
    return _round_money(sum(_maybe_float(trade.get("realized_pnl_usd")) or 0.0 for trade in trades))


def _summary_pnl_text(summary: dict[str, Any], lane: str) -> str:
    missing = _maybe_int(summary.get(f"{lane}_missing_exit_truth_count")) or 0
    value = _maybe_float(summary.get(f"{lane}_realized_pnl_usd"))
    if missing:
        noun = "fill" if missing == 1 else "fills"
        return f"unknown ({missing} missing exit {noun})"
    return f"${value or 0.0:.2f}"


def _pnl_text(value: Any) -> str:
    formatted = _fmt_money(value)
    return f"${formatted}" if formatted else "unknown"


def _format_qty_label(quantity: int | None, can_ladder: Any) -> str:
    """ITEM D (2026-07-08 hygiene batch): mark a trade whose ORIGINAL entry
    could not express the profile ladder (< 2 contracts -- the T1 60/40 split
    needs >= 2, see ``can_ladder`` on ``TradeRecord``) so the month's
    analytics can separate full-DNA trades from T1-only trades at a glance.
    ``can_ladder`` is ``None`` for rows written before this migration --
    rendered as a bare quantity rather than misrepresented as no-ladder.
    """
    qty = quantity if quantity is not None else 0
    if can_ladder is None:
        return str(qty)
    if can_ladder in (0, False):
        return f"{qty} (no-ladder)"
    return str(qty)


def _exit_attribution(trade: dict[str, Any]) -> str | None:
    """Classify how a trade exited for the report's "Exit" column (workplan #10).

    A profile-dispatched exit is labeled ``profile:<rule>`` (e.g.
    ``profile:no_progress``) from the ``exit_rule`` attribution stamped by the
    profile route — distinct from a native/legacy thesis exit even though both
    share the exact same ``_handle_exit_locked`` dispatcher and ``exit_mode``.
    Absent that, fall back to what the existing fields already tell us:
    ``hard_flat`` (the EOD sweep), ``stop``/``target`` (the exit order id matches
    the resting protective order), or ``strategy`` (a native thesis exit — either
    ``exit_mode == "strategy"`` or any other filled exit). Returns ``None`` for a
    still-open trade (nothing to attribute yet).
    """
    if _is_open_trade(trade):
        return None
    exit_rule = _maybe_str(trade.get("exit_rule"))
    if exit_rule:
        return f"profile:{exit_rule}"
    exit_mode = str(trade.get("exit_mode") or "").lower()
    if exit_mode == "hard_flat":
        return "hard_flat"
    exit_order_id = trade.get("exit_order_id")
    if exit_order_id is not None:
        if exit_order_id == trade.get("stop_order_id"):
            return "stop"
        if exit_order_id == trade.get("target_order_id"):
            return "target"
    if exit_mode == "strategy":
        return "strategy"
    if exit_order_id is not None or trade.get("exit_price") is not None:
        # A closed trade with no other attribution (e.g. a dry_run/paper close,
        # which never sets exit_mode) is a native strategy exit by elimination —
        # the profile route always stamps exit_rule, checked above.
        return "strategy"
    return None


def _provider_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    return summarize_provider_reconciliation(events)


def _empty_reconciliation() -> dict[str, Any]:
    return empty_provider_reconciliation()


def _lifecycle_events(events: list[dict[str, Any]]) -> dict[str, int]:
    interesting = {
        "target_approach_detected",
        "virtual_target_activation",
        "virtual_target_pullback_restore",
        "profit_target_armed",
        "protective_stop_submission",
        "protection_restore_attempt",
        "hard_flat_submission",
        "shadow_exit_assumed",
        "exit_plan",
        "exit_decision",
    }
    return dict(sorted(Counter(event["event_type"] for event in events if event["event_type"] in interesting).items()))


def _profile_exit_summary(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Count + rule breakdown of profile-dispatched exits today (workplan #10).

    Only trades whose ``exit_attribution`` is ``profile:<rule>`` count — a
    reporting-only rollup so the operator's profile-vs-legacy exit readback has
    a headline number without scanning the full Trades table.
    """
    rules: list[str] = []
    for trade in trades:
        attribution = trade.get("exit_attribution")
        if isinstance(attribution, str) and attribution.startswith("profile:"):
            rules.append(attribution.split(":", 1)[1])
    return {
        "count": len(rules),
        "rule_counts": dict(sorted(Counter(rules).items())),
    }


def _relaxed_evidence_lanes(
    trades: list[dict[str, Any]],
    deployments: list["DeploymentManifest"] | None,
) -> list[dict[str, Any]]:
    """Surface today's trading lanes whose active-plan deployment carries
    ``source.metadata.evidence_gates_relaxed`` (workplan #17 / operator-audit P5).

    A shadow lane compiled with relaxed evidence gates is a genuine risk of
    accidental promotion if it happens to look good on a thin sample; this list
    is reporting-only (it does not touch which lanes trade or how) so the
    operator sees "this row's evidence was relaxed" right next to today's
    activity instead of having to cross-reference active_plan.json by hand.
    """
    if not deployments:
        return []
    traded_today = {trade.get("deployment_id") for trade in trades if trade.get("deployment_id")}
    if not traded_today:
        return []
    lanes: list[dict[str, Any]] = []
    for deployment in deployments:
        deployment_id = getattr(deployment, "deployment_id", None)
        if deployment_id is None or deployment_id not in traded_today:
            continue
        source = getattr(deployment, "source", None)
        metadata = getattr(source, "metadata", None) or {}
        gates = metadata.get("evidence_gates_relaxed")
        if not gates:
            continue
        lanes.append({"deployment_id": deployment_id, "evidence_gates_relaxed": list(gates)})
    return sorted(lanes, key=lambda lane: lane["deployment_id"])


def _entry_selector_empty_by_deployment(
    events: list[dict[str, Any]],
    deployments: list["DeploymentManifest"] | None,
    trades: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Per-deployment ``entry_selector_empty`` count, flagging LIVE lanes
    distinctly (2026-07-08 hygiene batch, item C).

    2026-07-07 case: SMH's signal was live and dispatched, but the selector
    filtered out every contract, so no trade -- and no runtime_issue signal
    -- ever reached the report at a per-lane granularity. A filtered-out live
    signal must never masquerade as a quiet day. Reporting-only: this reads
    the same ``runtime_issue``/``entry_selector_empty`` events daily_report
    already aggregates into ``runtime_issue_counts``, just re-grouped by
    deployment and cross-referenced against ``deployment.execution.shadow_only``
    (the same live/shadow signal ``_relaxed_evidence_lanes`` already uses)
    so a live lane's count is never buried among shadow-lane noise.
    """
    counts: Counter[str] = Counter()
    latest_payloads: dict[str, dict[str, Any]] = {}
    for event in events:
        if event["event_type"] != "runtime_issue":
            continue
        payload = event["payload"] or {}
        if payload.get("category") != "entry_selector_empty":
            continue
        deployment_id = payload.get("deployment_id") or payload.get("symbol") or "UNKNOWN"
        deployment_id = str(deployment_id)
        counts[deployment_id] += 1
        latest_payloads[deployment_id] = payload
    if not counts:
        return []
    live_deployment_ids = {
        getattr(deployment, "deployment_id", None)
        for deployment in (deployments or [])
        if not getattr(getattr(deployment, "execution", None), "shadow_only", False)
    }
    selected_deployment_ids = {
        str(trade.get("deployment_id"))
        for trade in (trades or [])
        if trade.get("deployment_id") and trade.get("option_symbol")
    }
    rows = []
    for deployment_id, count in counts.items():
        latest = latest_payloads[deployment_id]
        breakdown = latest.get("selector_breakdown") or {}
        rows.append(
            {
                "deployment_id": deployment_id,
                "count": count,
                "live": deployment_id in live_deployment_ids,
                "outcome": (
                    "selected_later"
                    if deployment_id in selected_deployment_ids
                    else "no_contract_selected"
                ),
                "latest_diagnostics": latest.get("selector_diagnostics") or {},
                "latest_rejections": {
                    key: value
                    for key, value in breakdown.items()
                    if key != "total_candidates" and value
                },
            }
        )
    return sorted(rows, key=lambda row: (not row["live"], -row["count"], row["deployment_id"]))


def _data_quality_warnings(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for trade in trades:
        if trade.get("observation_outcome") in NON_TRADE_OUTCOMES:
            continue
        if (
            trade.get("lane") == "live"
            and not _is_open_trade(trade)
            and _maybe_float(trade.get("entry_price")) is not None
            and (_maybe_int(trade.get("quantity")) or 0) > 0
            and _maybe_float(trade.get("exit_price")) is None
        ):
            warnings.append(
                {
                    "category": "live_exit_truth_missing",
                    "trade_id": trade.get("trade_id"),
                    "deployment_id": trade.get("deployment_id"),
                    "symbol": trade.get("symbol"),
                    "option_symbol": trade.get("option_symbol"),
                    "message": "closed live trade is missing confirmed exit fill truth; realized P&L is unknown",
                }
            )
        underlying = _maybe_float(trade.get("underlying_entry_price"))
        strike = _maybe_float(trade.get("option_strike"))
        if underlying is None or strike is None or strike <= 0:
            continue
        ratio = underlying / strike
        if ratio < 0.5 or ratio > 2.0:
            warnings.append(
                {
                    "trade_id": trade.get("trade_id"),
                    "deployment_id": trade.get("deployment_id"),
                    "symbol": trade.get("symbol"),
                    "option_symbol": trade.get("option_symbol"),
                    "underlying_entry_price": underlying,
                    "option_strike": strike,
                    "ratio": round(ratio, 4),
                    "message": "underlying entry price is far from option strike; check quote scaling before using this as promotion evidence",
                }
            )
            continue
    return warnings


def _is_open_trade(trade: dict[str, Any]) -> bool:
    status = str(trade.get("status") or "").lower()
    return status not in {"closed", RECONCILIATION_HOLD_STATUS}


def _is_entry_reconciliation_hold(trade: dict[str, Any]) -> bool:
    return str(trade.get("status") or "").lower() == RECONCILIATION_HOLD_STATUS


def _protection_state(trade: dict[str, Any]) -> str:
    status = str(trade.get("status") or "").lower()
    if status == "closed":
        return "closed"
    if status == RECONCILIATION_HOLD_STATUS:
        return "reconciliation_hold"
    if trade.get("exit_order_id") or status.endswith("exit_pending") or "exit_pending" in status:
        return "exit_pending"
    if status == "target_active":
        return "target_active"
    if trade.get("stop_order_id"):
        return "protected"
    return "unprotected"


def _open_position_summary(open_positions: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(_protection_state(position) for position in open_positions)
    return {
        "protected_count": counts.get("protected", 0),
        "target_active_count": counts.get("target_active", 0),
        "unprotected_count": counts.get("unprotected", 0),
        "exit_pending_count": counts.get("exit_pending", 0),
    }


def _report_status(
    *,
    provider_events: dict[str, Any],
    data_quality_warnings: list[dict[str, Any]],
    runtime_issue_counts: dict[str, int] | None = None,
    open_positions: list[dict[str, Any]] | None = None,
    entry_reconciliation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if any(
        str(position.get("lane") or "").lower() == "live"
        and position.get("protection_state") == "unprotected"
        for position in (open_positions or [])
    ):
        return {"level": "RED", "reason": "live_open_unprotected", "attention_required": True}
    if (runtime_issue_counts or {}).get("dead_lane", 0) > 0:
        return {"level": "RED", "reason": "dead_live_lane", "attention_required": True}
    if (entry_reconciliation or {}).get("attention_required"):
        return {"level": "RED", "reason": "stale_entry_reconciliation_hold", "attention_required": True}
    if provider_events.get("attention_required"):
        return {"level": "RED", "reason": "reconciliation_recovery_exhausted", "attention_required": True}
    if provider_events.get("active_blocking_count", 0) > 0:
        return {"level": "RED", "reason": "blocking_reconciliation_failure", "attention_required": True}
    if provider_events.get("active_degraded_count", 0) > 0:
        return {"level": "YELLOW", "reason": "degraded_reconciliation", "attention_required": False}
    if data_quality_warnings:
        return {"level": "YELLOW", "reason": "data_quality_warning", "attention_required": False}
    if provider_events.get("active_warning_count", 0) > 0 or provider_events.get(
        "active_runtime_issue_count", 0
    ) > 0:
        return {"level": "YELLOW", "reason": "provider_warning", "attention_required": False}
    return {"level": "GREEN", "reason": "ok", "attention_required": False}


def _is_shadow_trade(trade: dict[str, Any]) -> bool:
    deployment_id = str(trade.get("deployment_id") or "").lower()
    entry_order_id = str(trade.get("entry_order_id") or "")
    exit_order_id = str(trade.get("exit_order_id") or "")
    exit_order_type = str(trade.get("exit_order_type") or "").upper()
    return (
        "shadow" in deployment_id
        or entry_order_id.startswith("SHADOW")
        or exit_order_id.startswith("DRY_RUN")
        or exit_order_type == "PAPER"
    )


def _parse_option_strike(option_symbol: str | None) -> float | None:
    if not option_symbol:
        return None
    match = _OPTION_SYMBOL_RE.match(option_symbol)
    if not match:
        return None
    return int(match.group(1)) / 1000.0


def _safe_json(payload_text: str | None) -> dict[str, Any]:
    try:
        value = json.loads(payload_text or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _coerce_day(value: date | str | None) -> date:
    if value is None:
        return datetime.now(UTC).date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _maybe_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _maybe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _maybe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _round_money(value: float) -> float:
    return round(float(value), 2)


def _average_or_none(values: list[float], *, digits: int = 2) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), digits)


def _fmt_money(value: Any) -> str:
    number = _maybe_float(value)
    return "" if number is None else f"{number:.2f}"


def _fmt_decimal(value: Any) -> str:
    number = _maybe_float(value)
    return "" if number is None else f"{number:.4f}"


# --- Risk rails formatting helpers (operator-audit P3) ---------------------
def _fmt_pct(value: Any) -> str:
    number = _maybe_float(value)
    return "n/a" if number is None else f"{number:.2f}%"


def _fmt_money_or_na(value: Any) -> str:
    number = _maybe_float(value)
    if number is None:
        return "n/a"
    sign = "-" if number < 0 else ""
    return f"{sign}${abs(number):,.2f}"
# --- end risk rails formatting helpers --------------------------------------


# --- RYG tabular report (user preference: metrics + RYG + why) ------------

_RYG_EMOJI = {"RED": "🔴", "YELLOW": "🟡", "GREEN": "🟢", "NO_DATA": "⚪"}


def _ryg(level: str | None) -> str:
    return _RYG_EMOJI.get(str(level or "").upper(), "⚪")


def _signal_funnel_by_lane(report: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Funnel per lane from events + trades.

    evaluated = signal_evaluation count (total, not lane-split)
    selector_empty is lane-split via provider_health entry_selector_empty_by_deployment.
    When deployments is None, lane is inferred from deployment_id containing 'shadow'.
    """
    counts = report.get("event_type_counts") or {}
    selector_empty = report.get("provider_health", {}).get("entry_selector_empty_by_deployment") or []
    # If deployments list was None, live flag may be unreliable (all False) — fallback to name heuristic
    def _is_live_row(r: dict[str, Any]) -> bool:
        if "live" in r and r["live"] is not None:
            # If report has at least one live True, trust it; otherwise infer
            return bool(r.get("live"))
        return "shadow" not in str(r.get("deployment_id") or "").lower()

    # Detect if live inference is broken (no live rows at all but we have live trades)
    has_live_flag = any(bool(r.get("live")) for r in selector_empty)
    if not has_live_flag and selector_empty:
        # Fallback: infer from deployment_id string
        live_selector = sum(r.get("count", 0) for r in selector_empty if "shadow" not in str(r.get("deployment_id") or "").lower())
        shadow_selector = sum(r.get("count", 0) for r in selector_empty if "shadow" in str(r.get("deployment_id") or "").lower())
    else:
        live_selector = sum(r.get("count", 0) for r in selector_empty if _is_live_row(r))
        shadow_selector = sum(r.get("count", 0) for r in selector_empty if not _is_live_row(r))
    summary = report.get("trade_summary") or {}
    live_trades = int(summary.get("live_count") or 0)
    shadow_trades = int(summary.get("shadow_count") or 0)
    total_eval = int(counts.get("signal_evaluation") or 0)
    # When we can't split evaluated by lane, show total in live and — in shadow (see renderer)
    return {
        "live": {
            "evaluated": total_eval,
            "selector_empty": live_selector,
            "attempted": live_trades + live_selector,
            "filled": live_trades,
        },
        "shadow": {
            "evaluated": 0,
            "selector_empty": shadow_selector,
            "attempted": shadow_trades + shadow_selector,
            "filled": shadow_trades,
        },
    }


def _app_running_row(report: dict[str, Any], *, app_status: dict[str, Any] | None = None) -> tuple[str, str, str, str]:
    if app_status is None:
        return ("App running", "unknown", _ryg("NO_DATA"), "no runtime probe")
    if app_status.get("running") is None or app_status.get("detail") == "runtime_probe_failed":
        return ("App running", "unknown", _ryg("RED"), "runtime probe failed")
    running = bool(app_status.get("running"))
    live = bool(app_status.get("live"))
    pid = app_status.get("pid")
    raw_started = str(app_status.get("started_at") or "")
    started = raw_started[:16]  # fallback UTC slice
    # Prefer CT (America/Chicago) — Bhiksha schedule is CT; show ET equivalent in parens if needed
    try:
        from zoneinfo import ZoneInfo

        dt = datetime.fromisoformat(raw_started.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        ct = dt.astimezone(ZoneInfo("America/Chicago"))
        started = ct.strftime("%Y-%m-%d %H:%M CT")
    except Exception:
        pass
    if running and live:
        return ("App running", f"PID {pid} live since {started}" if pid else f"live since {started}", _ryg("GREEN"), "session active")
    if running:
        return ("App running", f"PID {pid} (not live) since {started}" if pid else f"running (dry) since {started}", _ryg("YELLOW"), "dry mode")
    return ("App running", "stopped", _ryg("RED"), "not running")


def _schwab_row(report: dict[str, Any], *, schwab_status: dict[str, Any] | None = None) -> tuple[str, str, str, str]:
    # Try report-embedded schwab summary if passed, else fallback
    state = None
    if schwab_status:
        # schwab_token_guard latest.json shape: final.state
        final = schwab_status.get("final") or schwab_status
        state = str(final.get("state") or schwab_status.get("state") or "").lower()
        days = final.get("refresh_token_days_left")
        if state in {"healthy"}:
            val = f"healthy ({days:.1f}d left)" if isinstance(days, (int, float)) else "healthy"
            return ("Schwab auth", val, _ryg("GREEN"), "token healthy")
        if state in {"access_token_stale"}:
            return ("Schwab auth", "access_token_stale", _ryg("YELLOW"), "will refresh")
        if state in {"refresh_token_near_expiry"}:
            return ("Schwab auth", f"near expiry ({days:.1f}d)" if isinstance(days, (int, float)) else "near expiry", _ryg("YELLOW"), "renew soon")
        if state in {"refresh_token_expired", "missing"}:
            return ("Schwab auth", state, _ryg("RED"), "renew required")
    return ("Schwab auth", "unknown", _ryg("NO_DATA"), "no probe")


def _provider_row(report: dict[str, Any]) -> tuple[str, str, str, str]:
    prov = (report.get("provider_health") or {}).get("reconciliation") or {}
    blocking = int(prov.get("active_blocking_count") or 0)
    degraded = int(prov.get("active_degraded_count") or 0)
    warning = int(prov.get("active_warning_count") or 0)
    state = str(prov.get("state") or "healthy")
    if blocking > 0:
        return ("Broker rec.", f"{blocking} blocking", _ryg("RED"), f"{blocking} blocking")
    if degraded > 0:
        return ("Broker rec.", f"{degraded} degraded", _ryg("YELLOW"), f"{degraded} degraded")
    if warning > 0:
        return ("Broker rec.", f"{warning} warning", _ryg("YELLOW"), f"{warning} warning")
    return ("Broker rec.", state, _ryg("GREEN"), "healthy")


def _data_quality_row(report: dict[str, Any]) -> tuple[str, str, str, str]:
    warnings = report.get("data_quality_warnings") or []
    issues = (report.get("provider_health") or {}).get("runtime_issue_counts") or {}
    dead = int(issues.get("dead_lane") or 0)
    if dead > 0:
        return ("Warnings", f"{dead} dead lane", _ryg("RED"), "live lane unfillable")
    if warnings:
        return ("Warnings", f"{len(warnings)} data-quality", _ryg("YELLOW"), warnings[0].get("message", "warning")[:40])
    if issues:
        total = sum(issues.values())
        return ("Warnings", f"{total} runtime", _ryg("YELLOW"), ", ".join(list(issues.keys())[:2]))
    return ("Warnings", "none", _ryg("GREEN"), "clean")


def _open_row(positions: list[dict[str, Any]], lane: str) -> tuple[str, str, str, str]:
    filtered = [p for p in positions if str(p.get("lane") or "").lower() == lane]
    total = len(filtered)
    if total == 0:
        return ("Open positions", "0", _ryg("GREEN"), "flat")
    unprotected = sum(1 for p in filtered if p.get("protection_state") == "unprotected")
    if unprotected > 0:
        return ("Open positions", f"{total} ({unprotected} unprotected)", _ryg("RED"), f"{unprotected} unprotected")
    return ("Open positions", f"{total} all protected", _ryg("GREEN"), "all protected")


def _pnl_row(report: dict[str, Any], lane: str) -> tuple[str, str, str, str]:
    summary = report.get("trade_summary") or {}
    key = f"{lane}_realized_pnl_usd"
    val = summary.get(key)
    text = _fmt_money_or_na(val) if val is not None else "$0.00"
    count = int(summary.get(f"{lane}_count") or 0)
    # P&L is info-only; keep GREEN unless we have no truth
    missing = int(summary.get(f"{lane}_missing_exit_truth_count") or 0)
    if missing > 0:
        return (f"P&L ({lane})", f"{text} ({count} trades)", _ryg("YELLOW"), f"{missing} missing exit truth")
    return (f"P&L ({lane})", f"{text} ({count} trades)", _ryg("GREEN"), "realized")


def _reconciliation_row(report: dict[str, Any]) -> tuple[str, str, str, str]:
    entry = report.get("entry_reconciliation") or {}
    prov = (report.get("provider_health") or {}).get("reconciliation") or {}
    needs = int(entry.get("needs_human_count") or 0)
    if needs > 0 or prov.get("attention_required"):
        return ("Reconciliation", f"{needs} needs you" if needs else "needs you", _ryg("RED"), "attention required")
    healing = int(entry.get("self_healing_count") or 0)
    if healing > 0:
        return ("Reconciliation", f"{healing} self-healing", _ryg("YELLOW"), "auto-recovering")
    return ("Reconciliation", "ok", _ryg("GREEN"), "healthy")


def _signal_rows(report: dict[str, Any], lane: str, funnel: dict[str, dict[str, int]]) -> list[tuple[str, str, str, str]]:
    f = funnel.get(lane) or {}
    evaluated = int(f.get("evaluated") or 0)
    selector_empty = int(f.get("selector_empty") or 0)
    attempted = int(f.get("attempted") or 0)
    filled = int(f.get("filled") or 0)
    triggered = max(0, attempted)  # proxy for triggered that reached selector
    # Only surface RYG on the "unfillable" case
    if selector_empty > 0 and attempted == selector_empty:
        ryg = _ryg("YELLOW")
        why = f"{selector_empty} passed but no contract"
    elif selector_empty > 0:
        ryg = _ryg("YELLOW")
        why = f"{selector_empty} selector_empty"
    else:
        ryg = _ryg("GREEN")
        why = "flow ok" if attempted > 0 or evaluated == 0 else "quiet"
    return [
        ("Signals eval", str(evaluated) if lane == "live" else "—", ryg if lane == "live" else _ryg("GREEN"), why if lane == "live" else "shadow"),
        ("Signals triggered", str(triggered), ryg, why),
        ("Entry attempts", str(attempted), ryg, why),
        ("Fills", str(filled), _ryg("GREEN"), "filled"),
    ]


def _build_ryg_tables(report: dict[str, Any], *, app_status: dict[str, Any] | None = None, schwab_status: dict[str, Any] | None = None) -> dict[str, list[tuple[str, str, str, str]]]:
    positions = report.get("open_positions") or []
    funnel = _signal_funnel_by_lane(report)
    app_rows: list[tuple[str, str, str, str]] = [
        _app_running_row(report, app_status=app_status),
        _schwab_row(report, schwab_status=schwab_status),
        _provider_row(report),
        _data_quality_row(report),
        _reconciliation_row(report),
    ]
    live_rows: list[tuple[str, str, str, str]] = [
        _pnl_row(report, "live"),
        _open_row(positions, "live"),
        *_signal_rows(report, "live", funnel),
    ]
    shadow_rows: list[tuple[str, str, str, str]] = [
        _pnl_row(report, "shadow"),
        _open_row(positions, "shadow"),
        *_signal_rows(report, "shadow", funnel),
    ]
    return {"app": app_rows, "live": live_rows, "shadow": shadow_rows}


def render_daily_report_ryg_markdown(report: dict[str, Any], *, app_status: dict[str, Any] | None = None, schwab_status: dict[str, Any] | None = None) -> str:
    tables = _build_ryg_tables(report, app_status=app_status, schwab_status=schwab_status)
    day = report.get("trading_date") or ""
    lines = [f"# Bhiksha RYG — {day}", ""]
    for title, key in [("APP", "app"), ("LIVE", "live"), ("SHADOW", "shadow")]:
        lines.extend([f"## {title}", "", "| Metric | Value | Status | Why |", "|---|---|---|---|"])
        for metric, value, ryg, why in tables[key]:
            lines.append(f"| {metric} | {value} | {ryg} | {why} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def render_daily_report_ryg_telegram_html(report: dict[str, Any], *, app_status: dict[str, Any] | None = None, schwab_status: dict[str, Any] | None = None) -> str:
    """Telegram HTML — mobile-friendly, no <pre> tables.

    See kamandal_v2/ops/daily_report.py for rationale: fixed-width <pre>
    tables at 60 chars wrap on mobile (~40 cols, openclaw#36323) and force
    truncation. Per https://core.telegram.org/bots/api#html-style the
    allowed tags are <b><i><u><s><code><pre><a> etc. (no nesting); we use
    inline  🟢 <b>Metric</b>: <code>value</code> — <i>why</i>  so lines
    wrap naturally and render correctly with lathi-bus template=status
    (parse_mode HTML via _render_html_message). Outer title is supplied
    via send_lathi_alert(title=...), so body does not repeat the day.
    """

    from html import escape

    tables = _build_ryg_tables(report, app_status=app_status, schwab_status=schwab_status)
    lines: list[str] = []
    for section, key in [("APP", "app"), ("LIVE", "live"), ("SHADOW", "shadow")]:
        lines.append(f"<b>{section}</b>")
        for metric, value, ryg, why in tables[key]:
            metric_html = escape(metric)
            value_html = escape(value) if value else "—"
            why_html = escape(why) if why else ""
            row = f"{ryg} <b>{metric_html}</b>: <code>{value_html}</code>"
            if why_html:
                row += f" — <i>{why_html}</i>"
            lines.append(row)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def render_daily_report_ryg_telegram_text(report: dict[str, Any], *, app_status: dict[str, Any] | None = None, schwab_status: dict[str, Any] | None = None) -> str:
    """Plain-text fallback (no HTML) for Telegram when HTML is not desired."""
    tables = _build_ryg_tables(report, app_status=app_status, schwab_status=schwab_status)
    day = report.get("trading_date") or ""
    lines = [f"Bhiksha RYG — {day}", ""]
    for title, key in [("APP", "app"), ("LIVE", "live"), ("SHADOW", "shadow")]:
        lines.append(f"{title}")
        lines.append("-" * 40)
        for metric, value, ryg, why in tables[key]:
            lines.append(f"{ryg} {metric}: {value} — {why}")
        lines.append("")
    return "\n".join(lines) + "\n"
