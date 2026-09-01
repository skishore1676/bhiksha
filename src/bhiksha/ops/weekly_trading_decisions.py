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

from bhiksha.ops.daily_report import build_daily_report
from bhiksha.ops.exit_edge_weekly import write_exit_edge_weekly_evidence
from bhiksha.ops.experiment_status import (
    build_app_experiment_status,
    collect_read_only_facts,
)
from bhiksha.ops.shadow_ev_report import build_shadow_ev_report
from bhiksha.ops.trading_governance_evidence import build_trading_governance_evidence
from bhiksha.ops.trade_observation import (
    BLOCKED,
    FILLED_CLOSED,
    MISSING,
    NON_TRADE_OUTCOMES,
    NO_SIGNAL,
    classify_trade_observation,
    group_events_by_deployment_day,
    index_terminal_entry_observations,
    terminal_entry_observation,
)
from bhiksha.ops.weekly_scorecard import (
    _augment_trade,
    _data_quality_warnings,
    _deployment_lookup,
    _load_partials,
    _load_window_trades,
    build_weekly_scorecard,
)
from bhiksha.risk_envelope_authority import (
    risk_envelope_authorization_fingerprint,
)
from bhiksha.persistence.exit_state import (
    inspect_risk_envelope_rollback_latches,
)

if TYPE_CHECKING:
    from bhiksha.config.models import DeploymentManifest


@dataclass(slots=True, frozen=True)
class WeeklyTradingDecisionsWriteResult:
    report: dict[str, Any]
    json_path: Path
    markdown_path: Path
    facts_path: Path
    governance_path: Path
    exit_edge_path: Path
    experiment_status_path: Path


def write_weekly_trading_decisions(
    db_path: str | Path,
    *,
    output_dir: str | Path,
    week_end: date | str | None = None,
    deployments: list["DeploymentManifest"] | None = None,
    active_plan: dict[str, Any] | None = None,
    exit_edge_db_path: str | Path = "artifacts/observations/exit_edge_live.sqlite3",
    exit_edge_status_path: str | Path = "artifacts/observations/exit_edge_live_status.json",
    exit_edge_collector_configured: bool = False,
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
    status_facts, status_source = collect_read_only_facts(
        db_path,
        through=end.isoformat(),
    )
    status_plan = active_plan or _status_plan_from_deployments(
        deployments,
        through=end,
    )
    experiment_status = build_app_experiment_status(
        status_plan,
        facts_by_deployment=status_facts,
        source_status=status_source,
        as_of=end.isoformat(),
    )
    experiment_status_path = target / f"bhiksha_experiment_status_{end.isoformat()}.json"
    _atomic_json(experiment_status_path, experiment_status)
    governance = build_trading_governance_evidence(
        scorecard,
        through=end,
    )
    authorized_canaries = [
        {
            "deployment_id": deployment.deployment_id,
            "candidate_id": deployment.exit.risk_envelope_live_candidate_id,
            "candidate_overlay_hash": (
                deployment.exit.risk_envelope_live_candidate_overlay_hash
            ),
            "runtime_source_policy_hash": deployment.exit.exit_policy_hash,
            "authorization_id": (
                deployment.exit.risk_envelope_live_authorization_id
            ),
            "start_at": (
                deployment.exit.risk_envelope_live_start_at.isoformat()
                if deployment.exit.risk_envelope_live_start_at
                else None
            ),
            "expires_at": (
                deployment.exit.risk_envelope_live_expires_at.isoformat()
                if deployment.exit.risk_envelope_live_expires_at
                else None
            ),
            "authorized_deployment_id": (
                deployment.exit.risk_envelope_live_authorized_deployment_id
            ),
            "authorized_symbol": (
                deployment.exit.risk_envelope_live_authorized_symbol
            ),
            "authorized_active_plan_id": (
                deployment.exit.risk_envelope_live_authorized_active_plan_id
            ),
            "rollback_action": (
                deployment.exit.risk_envelope_live_rollback_action
            ),
            "max_premium_cap_fraction": (
                deployment.exit.risk_envelope_live_max_premium_cap_fraction
            ),
            "base_max_trade_premium_usd": (
                deployment.risk.max_trade_premium_usd
            ),
            "effective_max_trade_premium_usd": (
                float(deployment.risk.max_trade_premium_usd or 0)
                * float(
                    deployment.exit.risk_envelope_live_max_premium_cap_fraction
                    or 0
                )
            ),
            "symbol": deployment.symbol,
            "runtime_mode": deployment.execution.runtime_mode,
            "dte_min": deployment.execution.dte_min,
            "dte_max": deployment.execution.dte_max,
            "dte_fallback_policy": deployment.execution.dte_fallback_policy,
            "max_contracts": deployment.risk.max_contracts,
        }
        for deployment in (deployments or [])
        if deployment.enabled
        and deployment.exit.risk_envelope_live_mode == "canary"
    ]
    if authorized_canaries:
        active_plan_ids = {
            str(item["authorized_active_plan_id"])
            for item in authorized_canaries
            if item.get("authorized_active_plan_id")
        }
        active_plan_id = (
            next(iter(active_plan_ids))
            if len(active_plan_ids) == 1
            else None
        )
        authorization_fingerprint = (
            risk_envelope_authorization_fingerprint(
                active_plan_id=active_plan_id,
                deployments=deployments or [],
            )
        )
        for canary in authorized_canaries:
            canary["startup_authorization_fingerprint"] = (
                authorization_fingerprint
            )
    live_envelope_enabled_count = len(authorized_canaries)
    rollback_latches = inspect_risk_envelope_rollback_latches(
        db_path,
        deployment_ids={
            str(item["deployment_id"])
            for item in authorized_canaries
        },
    )
    exit_edge = write_exit_edge_weekly_evidence(
        db_path=exit_edge_db_path,
        status_path=exit_edge_status_path,
        output_dir=target,
        week_start=start,
        week_end=end,
        collector_configured=exit_edge_collector_configured,
        live_envelope_enabled_count=live_envelope_enabled_count,
        authorized_canaries=authorized_canaries,
        rollback_latches=rollback_latches,
    )
    stable_id = f"bhiksha-weekly-trading-decisions:{end.isoformat()}"
    facts_path = target / f"trading_decision_facts_{end.isoformat()}.json"
    _atomic_json(facts_path, facts)
    governance_path = target / f"trading_governance_evidence_{end.isoformat()}.json"
    _atomic_json(governance_path, governance)
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
        "governance_evidence": str(governance_path),
        "governance_evidence_receipt": governance["receipt"],
        "experiment_status": str(experiment_status_path),
        "experiment_status_schema": experiment_status["schema"],
        "experiment_status_source_status": experiment_status["source_status"],
        # Canonical cross-desk contract consumed by TradeLab. Keep the legacy
        # singular fields below during the migration because older readers and
        # operator tooling still display them.
        "exit_policy_evidence": {"bhiksha": str(exit_edge.json_path)},
        "exit_policy_evidence_receipts": {
            "bhiksha": exit_edge.evidence["receipt"]
        },
        "exit_edge_evidence": str(exit_edge.json_path),
        "exit_edge_evidence_receipt": exit_edge.evidence["receipt"],
        "exit_edge": exit_edge.evidence,
        "workbook_update": {"status": "pending"},
    }
    report["receipt"] = _weekly_receipt(report)
    stem = f"weekly_trading_decisions_{start.isoformat()}_{end.isoformat()}"
    json_path = target / f"{stem}.json"
    markdown_path = target / f"{stem}.md"
    _atomic_json(json_path, report)
    _atomic_text(markdown_path, render_weekly_trading_decisions_markdown(report))
    return WeeklyTradingDecisionsWriteResult(
        report,
        json_path,
        markdown_path,
        facts_path,
        governance_path,
        exit_edge.json_path,
        experiment_status_path,
    )


def finalize_weekly_trading_decisions(
    result: WeeklyTradingDecisionsWriteResult,
    workbook_update: dict[str, Any],
) -> WeeklyTradingDecisionsWriteResult:
    result.report["workbook_update"] = workbook_update
    result.report["receipt"] = _weekly_receipt(result.report)
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
    terminal_events: list[dict[str, Any]] = []
    weekly_observation_events: list[dict[str, Any]] = []
    option_snapshot_selected_matches: dict[str, bool | None] = {}
    if path.exists():
        with closing(sqlite3.connect(path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = _load_window_trades(conn, date(2000, 1, 1), through)
            option_snapshot_selected_matches = (
                _load_option_snapshot_selected_matches(conn, rows)
            )
            partials = _load_partials(conn, [str(row["trade_id"]) for row in rows])
            terminal_events = _load_observation_events(
                conn,
                start=date(2000, 1, 1),
                end=through,
                terminal_only=True,
            )
            weekly_observation_events = _load_observation_events(
                conn,
                start=through - timedelta(days=through.weekday()),
                end=through,
                terminal_only=False,
            )
    exported_at = datetime.now(UTC).isoformat()
    facts = []
    observations: list[dict[str, Any]] = []
    terminal_by_trade = index_terminal_entry_observations(terminal_events)
    for raw in rows:
        row = dict(raw)
        trade = _augment_trade(row, partials.get(str(row.get("trade_id")), []), shadow_by_deployment)
        observation = classify_trade_observation(trade, terminal_by_trade)
        if observation is not None:
            trade["observation_outcome"] = observation["observation_outcome"]
            trade["pnl_eligible"] = observation["pnl_eligible"]
        if trade.get("realized_pnl_usd") is None:
            if observation is not None:
                observations.append(
                    _normalized_observation(
                        observation,
                        row=row,
                        db_path=path,
                        exported_at=exported_at,
                    )
                )
            continue
        if trade.get("observation_outcome") in NON_TRADE_OUTCOMES:
            observations.append(
                _normalized_observation(
                    observation or {},
                    row=row,
                    db_path=path,
                    exported_at=exported_at,
                )
            )
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
        quality_warnings = _data_quality_warnings([trade])
        if not exit_attribution:
            quality_warnings = [
                {"message": "missing_explicit_exit_attribution"},
                *quality_warnings,
            ]
        option_snapshot_selected_match = option_snapshot_selected_matches.get(
            str(row.get("trade_id") or "")
        )
        evidence_status, evidence_issues = _decision_evidence_status(
            row,
            trade,
            option_snapshot_selected_match=option_snapshot_selected_match,
        )
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
                "observation_outcome": FILLED_CLOSED,
                "pnl_eligible": True,
                "exit_attribution": exit_attribution,
                "exit_class": (
                    "profile"
                    if exit_attribution.startswith("profile:")
                    else "strategy"
                    if exit_attribution
                    else "missing"
                ),
                "data_quality_status": quality_warnings[0]["message"] if quality_warnings else "OK",
                "active_plan_id": row.get("active_plan_id"),
                "plan_revision_id": row.get("plan_revision_id"),
                "session_id": row.get("session_id"),
                "research_run_id": row.get("research_run_id"),
                "evidence_packet_id": row.get("evidence_packet_id"),
                "evidence_artifact_sha256": row.get(
                    "evidence_artifact_sha256"
                ),
                "evidence_artifact_uri": row.get("evidence_artifact_uri"),
                "experiment_id": row.get("experiment_id"),
                "cohort_id": row.get("cohort_id"),
                "cohort_contract_sha256": row.get("cohort_contract_sha256"),
                "deployment_contract_sha256": row.get("deployment_contract_sha256"),
                "declared_option_selection_contract_id": row.get(
                    "declared_option_selection_contract_id"
                ),
                "declared_option_selection_contract_sha256": row.get(
                    "declared_option_selection_contract_sha256"
                ),
                "authorization_identity_status": row.get(
                    "authorization_identity_status"
                ),
                "exit_policy_id": row.get("exit_policy_id"),
                "exit_policy_sha256": row.get("exit_policy_sha256"),
                "option_selection_snapshot_id": row.get(
                    "option_selection_snapshot_id"
                ),
                "option_selection_snapshot_persisted": row.get(
                    "option_selection_snapshot_persisted"
                ),
                "option_selection_snapshot_selected_match": (
                    option_snapshot_selected_match
                ),
                "option_candidate_set_sha256": row.get(
                    "option_candidate_set_sha256"
                ),
                "actual_option_selection_sha256": row.get(
                    "actual_option_selection_sha256"
                ),
                "observation_window": _observation_window(row, trade),
                "decision_evidence_status": evidence_status,
                "decision_evidence_issues": evidence_issues,
                "canary_id": row.get("canary_id"),
                "canary_authorization_sha256": row.get(
                    "canary_authorization_sha256"
                ),
                "fact_receipt_id": row.get("fact_receipt_id"),
                "frozen_entry_risk_usd": row.get("frozen_entry_risk_usd"),
                "frozen_round_trip_cost_usd": row.get(
                    "frozen_round_trip_cost_usd"
                ),
                "source_receipt": f"{path.name}#trade_sessions/{row.get('trade_id')}",
                "source_hash": f"sha256:{source_hash}",
                "exported_at": exported_at,
            }
        )
    observations.extend(
        _event_only_observations(
            weekly_observation_events,
            facts=facts,
            existing=observations,
            db_path=path,
            exported_at=exported_at,
        )
    )
    observations.sort(
        key=lambda row: (
            str(row.get("observation_date") or ""),
            str(row.get("deployment_id") or ""),
            str(row.get("trade_id") or ""),
            str(row.get("observation_outcome") or ""),
        )
    )
    daily_status = _daily_status_rows(
        report_dir,
        through,
        db_path=path,
    )
    body = {
        "schema": "bhiksha.trading_decision_facts.v1",
        "generated_at": exported_at,
        "facts": facts,
        "observations": observations,
        "daily_status": daily_status,
    }
    # The receipt identifies evidence, not run time. A retry with unchanged
    # facts must reuse the same digest so the workbook and Obsidian card can be
    # updated idempotently rather than creating weekly churn.
    digest_payload = {
        "schema": body["schema"],
        "facts": [{key: value for key, value in fact.items() if key != "exported_at"} for fact in facts],
        "observations": [
            {key: value for key, value in row.items() if key != "exported_at"}
            for row in observations
        ],
        "daily_status": daily_status,
    }
    digest = hashlib.sha256(json.dumps(digest_payload, sort_keys=True, default=str).encode()).hexdigest()
    body["receipt"] = {
        "status": "ok",
        "sha256": digest,
        "fact_count": len(facts),
        "observation_count": len(observations),
        "through": through.isoformat(),
    }
    return body


def _load_observation_events(
    conn: sqlite3.Connection,
    *,
    start: date,
    end: date,
    terminal_only: bool,
) -> list[dict[str, Any]]:
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "events" not in tables:
        return []
    event_types = [
        "entry_reconcile_released",
        "entry_reprice_blocked",
        "entry_reprice_cancel_after_timeout",
    ]
    if not terminal_only:
        event_types.extend(
            [
                "lifecycle_entry_blocked",
                "signal_decision",
                "signal_evaluation",
                "trade_plan",
            ]
        )
    placeholders = ", ".join("?" for _ in event_types)
    rows = conn.execute(
        f"""
        SELECT id, created_at, event_type, payload
        FROM events
        WHERE event_type IN ({placeholders})
          AND substr(replace(COALESCE(created_at, ''), ' ', 'T'), 1, 10)
              BETWEEN ? AND ?
        ORDER BY id
        """,
        [*event_types, start.isoformat(), end.isoformat()],
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


def _normalized_observation(
    observation: dict[str, Any],
    *,
    row: dict[str, Any] | None,
    db_path: Path,
    exported_at: str,
) -> dict[str, Any]:
    source_payload = row or observation
    source_hash = hashlib.sha256(
        json.dumps(source_payload, sort_keys=True, default=str).encode()
    ).hexdigest()
    event_id = observation.get("source_event_id")
    trade_id = observation.get("trade_id") or (row or {}).get("trade_id")
    source_receipt = (
        f"{db_path.name}#events/{event_id}"
        if event_id is not None
        else f"{db_path.name}#trade_sessions/{trade_id}"
    )
    observed_at = observation.get("observed_at") or (row or {}).get(
        "entry_timestamp"
    )
    return {
        **observation,
        "trade_id": str(trade_id or "") or None,
        "deployment_id": observation.get("deployment_id")
        or (row or {}).get("deployment_id"),
        "symbol": observation.get("symbol") or (row or {}).get("symbol"),
        "observation_date": str(observed_at or "").replace(" ", "T")[:10]
        or None,
        "source_receipt": source_receipt,
        "source_hash": f"sha256:{source_hash}",
        "exported_at": exported_at,
    }


def _event_only_observations(
    events: list[dict[str, Any]],
    *,
    facts: list[dict[str, Any]],
    existing: list[dict[str, Any]],
    db_path: Path,
    exported_at: str,
) -> list[dict[str, Any]]:
    """Emit only outcomes directly supported by the week's event receipts."""

    emitted_trade_outcomes = {
        (str(row.get("trade_id") or ""), str(row.get("observation_outcome") or ""))
        for row in existing
    }
    covered_deployment_days = {
        (
            str(row.get("deployment_id") or ""),
            str(row.get("entry_timestamp") or row.get("exit_timestamp") or "")[:10],
        )
        for row in facts
    }
    covered_deployment_days.update(
        (
            str(row.get("deployment_id") or ""),
            str(row.get("observation_date") or ""),
        )
        for row in existing
    )
    results: list[dict[str, Any]] = []

    for event in events:
        terminal = terminal_entry_observation(event)
        if terminal is None:
            continue
        key = (
            str(terminal.get("trade_id") or ""),
            str(terminal.get("observation_outcome") or ""),
        )
        if key in emitted_trade_outcomes:
            continue
        normalized = _normalized_observation(
            terminal,
            row=None,
            db_path=db_path,
            exported_at=exported_at,
        )
        results.append(normalized)
        emitted_trade_outcomes.add(key)
        covered_deployment_days.add(
            (
                str(normalized.get("deployment_id") or ""),
                str(normalized.get("observation_date") or ""),
            )
        )

    for (deployment_id, day), grouped in group_events_by_deployment_day(
        events
    ).items():
        if (deployment_id, day) in covered_deployment_days:
            continue
        signal_events = [
            event
            for event in grouped
            if event.get("event_type") in {"signal_decision", "signal_evaluation"}
        ]
        true_signal = any(
            (event.get("payload") or {}).get("signal") is True
            for event in signal_events
        )
        lifecycle_blocks = [
            event
            for event in grouped
            if event.get("event_type") == "lifecycle_entry_blocked"
        ]
        trade_plans = [
            event for event in grouped if event.get("event_type") == "trade_plan"
        ]

        outcome: str | None = None
        source_event: dict[str, Any] | None = None
        missing_reason: str | None = None
        if true_signal and lifecycle_blocks:
            outcome = BLOCKED
            source_event = lifecycle_blocks[-1]
        elif trade_plans and any(
            not (event.get("payload") or {}).get("order_id")
            for event in trade_plans
        ):
            outcome = BLOCKED
            source_event = trade_plans[-1]
        elif signal_events and not true_signal:
            outcome = NO_SIGNAL
            source_event = signal_events[-1]
        elif trade_plans:
            outcome = MISSING
            source_event = trade_plans[-1]
            missing_reason = "trade_plan_has_no_trade_or_terminal_entry_receipt"
        if outcome is None or source_event is None:
            continue

        payload = source_event.get("payload") or {}
        observation = {
            "observation_outcome": outcome,
            "trade_id": payload.get("trade_id"),
            "deployment_id": deployment_id,
            "symbol": payload.get("symbol"),
            "pnl_eligible": False,
            "source_event_type": source_event.get("event_type"),
            "source_event_id": source_event.get("event_id"),
            "observed_at": source_event.get("created_at"),
        }
        if missing_reason is not None:
            observation["missing_reason"] = missing_reason
        results.append(
            _normalized_observation(
                observation,
                row=None,
                db_path=db_path,
                exported_at=exported_at,
            )
        )
    return results


def render_weekly_trading_decisions_markdown(report: dict[str, Any]) -> str:
    score = report.get("scorecard") or {}
    headline = score.get("headline") or {}
    live = headline.get("live") or {}
    shadow = headline.get("shadow") or {}
    promotion = score.get("promotion_candidates") or {}
    candidates = promotion.get("candidates") or []
    near_misses = promotion.get("near_misses") or []
    negative_lanes = sorted(
        (
            lane for lane in (score.get("lanes") or [])
            if (lane.get("closed") or 0) >= 2 and (lane.get("total_pnl_usd") or 0.0) < 0
        ),
        key=lambda lane: lane.get("total_pnl_usd") or 0.0,
    )[:3]
    workbook = report.get("workbook_update") or {}
    governance = report.get("governance_evidence_receipt") or {}
    exit_edge = report.get("exit_edge") or {}
    edge_verdict = (exit_edge.get("verdict") or {}).get("status", "unavailable")
    edge_paired_raw = (exit_edge.get("cumulative") or {}).get("paired_count")
    edge_source_error = (exit_edge.get("collection") or {}).get("source_error")
    edge_paired = (
        "Unavailable"
        if edge_source_error or edge_paired_raw is None
        else str(int(edge_paired_raw))
    )
    lines = [
        f"# Weekly Trading Decisions — Performance, Promotions & Fixes — {report.get('week_end')}",
        "",
        f"- artifact: `{report.get('artifact_id')}`",
        f"- workbook update: `{workbook.get('status', 'pending')}`",
        f"- facts: `{(report.get('facts_export_receipt') or {}).get('fact_count', 0)}` through `{report.get('week_end')}`",
        f"- active Rail B demotions: `{governance.get('active_demotion_count', 0)}`",
        f"- Dynamic Risk Envelope evidence: `{edge_verdict}`; cumulative paired cohorts: `{edge_paired}`",
        "",
        "## What happened",
        "",
        f"- live: `{live.get('trades', 0)}` trades, `{_decision_pnl(live.get('total_pnl_usd'))}`",
        f"- shadow: `{shadow.get('trades', 0)}` trades, `{_decision_pnl(shadow.get('total_pnl_usd'))}`",
        "",
        "## Decisions to make",
        "",
    ]
    if candidates:
        for candidate in candidates:
            lines.append(f"- **PROMOTION REVIEW:** `{candidate.get('display_id')}` — {candidate.get('closed', 0)} closed, `{_decision_pnl(candidate.get('total_pnl_usd'))}`. Decide promote / observe / reject.")
    else:
        lines.append("- **PROMOTION:** no lane currently clears the visible evidence threshold; no promotion decision is required.")
    if near_misses:
        for lane in near_misses[:5]:
            lines.append(f"- **FIX OR OBSERVE:** `{lane.get('display_id')}` — `{lane.get('disqualified_by')}`.")
    near_miss_ids = {lane.get("deployment_id") for lane in near_misses}
    for lane in negative_lanes:
        if lane.get("deployment_id") in near_miss_ids:
            continue
        lines.append(
            f"- **PERFORMANCE FIX REVIEW:** `{lane.get('display_id')}` ({lane.get('mode')}) — "
            f"{lane.get('closed', 0)} closed, `{_decision_pnl(lane.get('total_pnl_usd'))}`, "
            f"avg return `{lane.get('avg_return_pct', 0.0):.1f}%`. Decide diagnose / keep observing / retire."
        )
    issues = len(score.get("data_quality_warnings") or [])
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


def _decision_pnl(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "unknown"
    return f"${value:.2f}"


def _daily_status_rows(
    report_dir: Path,
    through: date,
    *,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(report_dir.glob("trade_session_report_*.json")):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            day = str(report.get("trading_date") or "")
            if not day or day > through.isoformat():
                continue
            stored_provider_health = report.get("provider_health") or {}
            stored_runtime_counts = (
                stored_provider_health.get("runtime_issue_counts") or {}
            )
            needs_shadow_reclassification = (
                "suppressed_shadow_runtime_issue_counts"
                not in stored_provider_health
                and bool(
                    stored_runtime_counts.get(
                        "exit_state_degraded_protection"
                    )
                )
            )
            if (
                needs_shadow_reclassification
                and db_path is not None
                and db_path.exists()
            ):
                # Recompute the read-only classification from immutable event
                # and trade facts. This lets a Friday export apply newer
                # report semantics to an already-written Monday report without
                # rewriting that historical artifact.
                report = build_daily_report(
                    db_path,
                    trading_date=day,
                )
            provider = (report.get("provider_health") or {}).get("reconciliation") or {}
            runtime_issue_counts = (
                (report.get("provider_health") or {}).get(
                    "runtime_issue_counts"
                )
                or {}
            )
            suppressed_shadow_counts = (
                (report.get("provider_health") or {}).get(
                    "suppressed_shadow_runtime_issue_counts"
                )
                or {}
            )
            rows.append({
                "date": day,
                "risk_event_count": sum((report.get("event_type_counts") or {}).get(key, 0) for key in ("risk_halt", "risk_flatten", "risk_open_drawdown_warning")),
                "operational_issue_count": sum(runtime_issue_counts.values()),
                "shadow_diagnostic_count": sum(
                    suppressed_shadow_counts.values()
                ),
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


def _status_plan_from_deployments(
    deployments: list["DeploymentManifest"] | None,
    *,
    through: date,
) -> dict[str, Any]:
    """Keep unit callers useful without inventing a second experiment config."""

    serialized = [
        deployment.model_dump(mode="json")
        if hasattr(deployment, "model_dump")
        else dict(deployment)
        for deployment in (deployments or [])
    ]
    return {
        "active_plan_id": None,
        "trading_date": through.isoformat(),
        "generated_at": through.isoformat(),
        "deployments": serialized,
    }


def _decision_evidence_status(
    row: dict[str, Any],
    trade: dict[str, Any],
    *,
    option_snapshot_selected_match: bool | None = None,
) -> tuple[str, list[str]]:
    """Classify a closed trade without upgrading incomplete plumbing to evidence."""

    if row.get("authorization_identity_status") == "evidence_binding_quarantined":
        return "plumbing_invalid", ["observation_evidence_binding_quarantined"]
    if not row.get("evidence_packet_id"):
        return "legacy_unbound", ["missing_evidence_packet_id"]
    required = (
        "experiment_id",
        "cohort_id",
        "cohort_contract_sha256",
        "deployment_contract_sha256",
        "declared_option_selection_contract_sha256",
        "exit_policy_sha256",
        "plan_revision_id",
        "session_id",
        "fact_receipt_id",
        "option_selection_snapshot_id",
        "option_candidate_set_sha256",
        "actual_option_selection_sha256",
    )
    issues = [f"missing_{field}" for field in required if not row.get(field)]
    if row.get("option_selection_snapshot_persisted") not in (True, 1):
        issues.append("option_selection_snapshot_not_persisted")
    elif option_snapshot_selected_match is False:
        issues.append("option_selection_selected_contract_not_persisted")
    elif option_snapshot_selected_match is None:
        issues.append("option_selection_snapshot_consistency_unavailable")
    if not trade.get("exit_attribution"):
        issues.append("missing_explicit_exit_attribution")
    if {
        "option_selection_snapshot_not_persisted",
        "option_selection_selected_contract_not_persisted",
    }.intersection(issues):
        return "plumbing_invalid", sorted(set(issues))
    if issues:
        return "incomplete", sorted(set(issues))
    if row.get("authorization_identity_status") == "compiled_observation_only":
        return "observation_only", []
    return "eligible", []


def _load_option_snapshot_selected_matches(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
) -> dict[str, bool | None]:
    """Prove that a persisted selection snapshot contains its claimed winner."""

    tables = {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if not {
        "option_chain_snapshot_attempts",
        "option_chain_snapshots",
    }.issubset(tables):
        return {
            str(row["trade_id"]): None
            for row in rows
            if row["option_selection_snapshot_id"]
        }

    by_snapshot: dict[str, tuple[str | None, bool]] = {}
    snapshot_ids = sorted(
        {
            str(row["option_selection_snapshot_id"])
            for row in rows
            if row["option_selection_snapshot_id"]
        }
    )
    if snapshot_ids:
        placeholders = ", ".join("?" for _ in snapshot_ids)
        query = f"""
            SELECT
                attempt.snapshot_id,
                attempt.selected_option_symbol,
                MAX(
                    CASE
                        WHEN snapshot.is_selected = 1
                         AND snapshot.option_symbol = attempt.selected_option_symbol
                        THEN 1 ELSE 0
                    END
                ) AS selected_row_persisted
            FROM option_chain_snapshot_attempts AS attempt
            LEFT JOIN option_chain_snapshots AS snapshot
              ON snapshot.snapshot_id = attempt.snapshot_id
            WHERE attempt.snapshot_id IN ({placeholders})
            GROUP BY attempt.snapshot_id, attempt.selected_option_symbol
        """
        for result in conn.execute(query, snapshot_ids).fetchall():
            by_snapshot[str(result["snapshot_id"])] = (
                result["selected_option_symbol"],
                bool(result["selected_row_persisted"]),
            )

    matches: dict[str, bool | None] = {}
    for row in rows:
        snapshot_id = row["option_selection_snapshot_id"]
        if not snapshot_id:
            continue
        recorded = by_snapshot.get(str(snapshot_id))
        matches[str(row["trade_id"])] = bool(
            recorded
            and recorded[0]
            and str(recorded[0]) == str(row["option_symbol"] or "")
            and recorded[1]
        )
    return matches


def _observation_window(row: dict[str, Any], trade: dict[str, Any]) -> str:
    status = row.get("authorization_identity_status")
    if status == "compiled_observation_only":
        return "live_approval_gated"
    if status == "shadow_observation" and trade.get("lane") == "shadow":
        return "current_shadow"
    if row.get("experiment_id"):
        return "current_other"
    return "older_shadow_or_legacy"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_text(path, json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def weekly_stable_digest(report: dict[str, Any]) -> str:
    stable = {
        key: value
        for key, value in report.items()
        if key not in {"generated_at", "receipt"}
    }
    if isinstance(stable.get("exit_edge"), dict):
        edge = {
            key: value
            for key, value in stable["exit_edge"].items()
            if key not in {"generated_at", "receipt"}
        }
        collection = edge.get("collection")
        if isinstance(collection, dict) and isinstance(
            collection.get("freshness"), dict
        ):
            edge["collection"] = dict(collection)
            edge["collection"]["freshness"] = {
                key: value
                for key, value in collection["freshness"].items()
                if key != "age_seconds"
            }
        stable["exit_edge"] = edge
    return hashlib.sha256(
        json.dumps(
            stable,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def _weekly_receipt(report: dict[str, Any]) -> dict[str, Any]:
    workbook_status = (report.get("workbook_update") or {}).get("status", "pending")
    status = "ok" if workbook_status == "ok" else workbook_status
    return {
        "status": status,
        "sha256": weekly_stable_digest(report),
        "through": report.get("week_end"),
    }


def _atomic_text(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
