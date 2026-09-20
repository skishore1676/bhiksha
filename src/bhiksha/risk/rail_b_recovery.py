"""Opt-in, one reduced-risk probe per fresh shadow sample; never grants LIVE authority."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import fmean
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field


class RailBRecoveryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    # Supplying this object is the per-lane opt-in. All money/evidence choices
    # are explicit; the absence of the object disables recovery.
    min_shadow_trades: int = Field(ge=20)
    min_sessions: int = Field(ge=5)
    max_age_days: int = Field(ge=5, le=60)
    min_mean_net_r: float = Field(gt=0)
    round_trip_cost_per_contract_usd: float = Field(gt=0)
    premium_cap_fraction: float = Field(gt=0, le=.25)


def assess_sample(cases, *, deployment_id, policy_hash, since, now, policy):
    """Use complete, fresh, modeled fills for the unchanged primary policy."""
    from bhiksha.execution.exit_policy import canonical_policy_hash
    from bhiksha.ops.exit_edge_lab import analyze_cases

    eligible = [c for c in cases if c.deployment_id == deployment_id
        and max(since, now - timedelta(days=policy.max_age_days)) < c.entry_timestamp <= now
        and c.cohort_dimensions.get("entry_fill_kind") == "modeled_ask_touch"
        and "named_profiles" in c.experiment
        and canonical_policy_hash(c.experiment["named_profiles"][0]) == policy_hash]
    eligible.sort(key=lambda c: (c.entry_timestamp, c.trade_id), reverse=True)
    sample = eligible[:policy.min_shadow_trades]
    receipt = {"eligible": False, "sample_count": len(sample), "reason": "insufficient_fresh_shadow_trades"}
    if len(sample) < policy.min_shadow_trades:
        return receipt
    if len({c.trade_id for c in sample}) != len(sample):
        return {**receipt, "reason": "duplicate_trade_identity"}
    rows = analyze_cases(sample)["cases"]
    by_trade = {c.trade_id: c for c in sample}
    daily = defaultdict(list)
    for row in rows:
        if row["status"] != "paired":
            return {**receipt, "reason": "incomplete_or_censored_comparisons"}
        case = by_trade[row["trade_id"]]
        outcome = row["named_exit_outcomes"][row["management_exit"]]
        net = outcome["realized_pnl_usd"] - policy.round_trip_cost_per_contract_usd * case.quantity
        daily[case.entry_timestamp.astimezone(ZoneInfo("America/New_York")).date().isoformat()].append(
            net / row["common_entry_risk_usd"])
    session_means = [fmean(values) for values in daily.values()]
    receipt.update(session_count=len(daily), mean_session_net_r=fmean(session_means),
                   trade_ids=[c.trade_id for c in sample], evidence_after=since.isoformat())
    if len(daily) < policy.min_sessions:
        return {**receipt, "reason": "insufficient_independent_sessions"}
    if min(session_means) < 0 or fmean(session_means) < policy.min_mean_net_r:
        return {**receipt, "reason": "net_shadow_performance_below_recovery_floor"}
    return {**receipt, "eligible": True, "reason": "reduced_risk_probe", "max_contracts": 1,
            "premium_cap_fraction": policy.premium_cap_fraction}


def _read_cases(db_path, deployment_id, since):
    from bhiksha.ops.exit_edge_lab import ProspectiveQuoteTapeRepository
    repository = ProspectiveQuoteTapeRepository(Path(db_path), read_only=True)
    with repository._connect() as connection:
        # Lane-filtered and read-only. Timestamp filtering uses parsed aware
        # datetimes in assess_sample, not SQLite text ordering across offsets.
        rows = connection.execute("SELECT cohort_id FROM exit_edge_cohorts WHERE deployment_id = ?",
                                  (deployment_id,)).fetchall()
    return [repository.load_case(row[0]) for row in rows]


async def recovery_candidate(*, deployment, risk_manager, db_path, event_repository):
    """Called only after Rail B (never Rail A or infrastructure) vetoes entry."""
    policy = getattr(deployment.execution, "rail_b_recovery", None)
    if policy is None:
        return None
    receipt = {"eligible": False, "reason": "lane_not_authorized_for_named_live_exit"}
    if (not deployment.execution.shadow_only
        and deployment.execution.runtime_mode == "live_approval_gated"
        and deployment.exit.management_exit and deployment.exit.exit_policy_hash):
        try:
            from bhiksha.risk.risk_manager import _is_live_trade, _ensure_utc
            closed = await risk_manager.trade_state_repository.get_closed_trades_for_deployment(deployment.deployment_id)
            live = [t for t in closed if _is_live_trade(t) and t.entry_order_id and t.exit_filled_at]
            open_trades = await risk_manager.trade_state_repository.get_open_trades()
            if any(t.deployment_id == deployment.deployment_id and _is_live_trade(t) for t in open_trades):
                receipt = {"eligible": False, "reason": "live_probe_or_position_already_open"}
            elif not live:
                receipt = {"eligible": False, "reason": "no_closed_live_evidence"}
            else:
                # Any subsequent live probe consumes the old sample, regardless
                # of profit. A restart cannot reuse it or erase live losses.
                since = max(_ensure_utc(t.exit_filled_at) for t in live)
                if risk_manager.settings.demote_reset_at:
                    since = max(since, _ensure_utc(risk_manager.settings.demote_reset_at))
                cases = await asyncio.to_thread(_read_cases, db_path, deployment.deployment_id, since)
                receipt = await asyncio.to_thread(assess_sample, cases, deployment_id=deployment.deployment_id,
                    policy_hash=deployment.exit.exit_policy_hash, since=since, now=datetime.now(UTC), policy=policy)
        except Exception as exc:
            receipt = {"eligible": False, "reason": "recovery_evidence_unavailable", "error_type": type(exc).__name__}
    await event_repository.append("rail_b_recovery_evaluation", {"deployment_id": deployment.deployment_id, **receipt})
    if not receipt["eligible"]:
        return None
    cap = deployment.risk.max_trade_premium_usd
    if cap is None or cap <= 0:
        return None
    return deployment.model_copy(update={"risk": deployment.risk.model_copy(update={
        "max_trade_premium_usd": cap * policy.premium_cap_fraction, "max_contracts": 1})}, deep=True)
