"""Post-close observation reports for enabled deployments."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Any

import polars as pl

from bhiksha.app.bootstrap import build_runtime
from bhiksha.app.replay import ReplaySignalEvaluator
from bhiksha.market_data.feature_service import FeatureService
from bhiksha.market_data.session import ensure_utc
from bhiksha.market_data.trading_calendar import trading_window_start
from bhiksha.ops.summary import build_session_summary


async def write_observation_reports(
    *,
    config_root: str | Path = "config",
    db_path: str | Path | None = None,
    trading_days: int = 3,
    provider: str | None = None,
    output_dir: str | Path | None = None,
    include_replay: bool = True,
    session_payload_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    runtime = build_runtime(config_root, session_payload_path=session_payload_path)
    repo_root = Path(config_root).parent
    resolved_db_path = Path(db_path) if db_path is not None else Path(runtime.app_config.sqlite_path)
    if not resolved_db_path.is_absolute():
        resolved_db_path = repo_root / resolved_db_path
    target_dir = Path(output_dir) if output_dir is not None else (repo_root / runtime.app_config.observation_reports_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    events = _load_events(resolved_db_path)
    summary = build_session_summary(str(resolved_db_path), recent_limit=50)
    evaluator = ReplaySignalEvaluator(FeatureService(), runtime.strategy_registry)
    reportable_deployments = list(runtime.enabled_deployments)

    packets: list[dict[str, Any]] = []
    for deployment in reportable_deployments:
        packet = await _deployment_packet(
            runtime=runtime,
            evaluator=evaluator,
            deployment=deployment,
            events=events,
            session_summary=summary,
            trading_days=trading_days,
            provider=provider,
            include_replay=include_replay,
        )
        packets.append(packet)
        packet_path = target_dir / f"{deployment.deployment_id}.json"
        packet_path.write_text(json.dumps(packet, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        markdown_path = target_dir / f"{deployment.deployment_id}.md"
        markdown_path.write_text(_packet_markdown(packet), encoding="utf-8")

    index_path = target_dir / "index.json"
    index_path.write_text(json.dumps({"reports": packets}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return packets


async def _deployment_packet(
    *,
    runtime,
    evaluator: ReplaySignalEvaluator,
    deployment,
    events: list[dict[str, Any]],
    session_summary,
    trading_days: int,
    provider: str | None,
    include_replay: bool,
) -> dict[str, Any]:
    startup_snapshot, startup_created_at = _latest_startup_snapshot(events, deployment.deployment_id)
    relevant_events = [
        event
        for event in events
        if event["payload"].get("deployment_id") == deployment.deployment_id
        and _event_at_or_after(event, startup_created_at)
    ]
    symbol_events = [
        event
        for event in events
        if event["payload"].get("deployment_id") is None
        and event["payload"].get("symbol") == deployment.symbol
        and _event_at_or_after(event, startup_created_at)
    ]
    signal_events = [event for event in relevant_events if event["event_type"] == "signal_decision"]
    trade_plan_events = [event for event in relevant_events if event["event_type"] == "trade_plan"]
    exit_plan_events = [event for event in relevant_events if event["event_type"] == "exit_plan"]
    exit_decision_events = [event for event in relevant_events if event["event_type"] == "exit_decision"]
    lifecycle_blocked_events = [event for event in relevant_events if event["event_type"] == "lifecycle_entry_blocked"]
    runtime_issue_events = [
        event
        for event in relevant_events + symbol_events
        if event["event_type"] == "runtime_issue"
    ]
    blocked_entry_reasons = Counter()
    for event in trade_plan_events:
        for reason in event["payload"].get("risk_reasons") or []:
            blocked_entry_reasons[str(reason)] += 1
    for event in lifecycle_blocked_events:
        blocked_entry_reasons[f"lifecycle_state:{event['payload'].get('state')}"] += 1

    runtime_issue_counts = Counter()
    for event in runtime_issue_events:
        category = str(event["payload"].get("category", "exception"))
        runtime_issue_counts[category] += 1
    signal_reason_counts = Counter()
    for event in signal_events:
        for reason in event["payload"].get("reason") or []:
            signal_reason_counts[str(reason)] += 1
    exit_reason_counts = Counter()
    for event in exit_decision_events:
        for reason in event["payload"].get("reason") or []:
            exit_reason_counts[str(reason)] += 1

    replay = await _replay_summary(
        runtime=runtime,
        evaluator=evaluator,
        deployment=deployment,
        trading_days=trading_days,
        provider=provider,
        include_replay=include_replay,
    )
    signal_true_count = sum(1 for event in signal_events if bool(event["payload"].get("signal")))
    exit_true_count = sum(1 for event in exit_decision_events if bool(event["payload"].get("exit")))
    startup_deployment = _deployment_from_startup_snapshot(startup_snapshot, deployment.deployment_id)
    startup_selection = startup_snapshot.get("deployment_selection") or {}
    startup_session = startup_snapshot.get("session") or {}
    source_metadata = deployment.source.metadata or {}
    packet = {
        "deployment_id": deployment.deployment_id,
        "candidate_id": deployment.source.metadata.get("candidate_id"),
        "playbook_id": source_metadata.get("playbook_id"),
        "trade_id": source_metadata.get("trade_id"),
        "symbol": deployment.symbol,
        "strategy_key": deployment.strategy.key,
        "source_origin": deployment.source.origin,
        "source_artifact": deployment.source.artifact,
        "authorization_mode": source_metadata.get("authorization_mode"),
        "session_mode": startup_selection.get("mode"),
        "session_id": startup_selection.get("session_id"),
        "live_requested": startup_session.get("live"),
        "shadow_only": deployment.execution.shadow_only,
        "bias_template": deployment.source.metadata.get("bias_template"),
        "horizon": deployment.source.metadata.get("horizon"),
        "startup_config_fingerprint": startup_snapshot.get("config_fingerprint"),
        "signal_decisions_total": len(signal_events),
        "signal_true_count": signal_true_count,
        "trade_plan_count": len(trade_plan_events),
        "exit_plan_count": len(exit_plan_events),
        "exit_decision_count": len(exit_decision_events),
        "exit_true_count": exit_true_count,
        "latest_lifecycle_state": session_summary.lifecycle_last_state.get(deployment.deployment_id),
        "blocked_entry_reasons": dict(blocked_entry_reasons),
        "signal_reason_counts": dict(signal_reason_counts),
        "exit_reason_counts": dict(exit_reason_counts),
        "runtime_issue_counts": dict(runtime_issue_counts),
        "startup_deployment": startup_deployment,
        "replay": replay,
        "safe_for_live_review": bool(startup_snapshot.get("config_fingerprint"))
        and signal_true_count > 0
        and sum(runtime_issue_counts.values()) == 0,
    }
    return packet


async def _replay_summary(
    *,
    runtime,
    evaluator: ReplaySignalEvaluator,
    deployment,
    trading_days: int,
    provider: str | None,
    include_replay: bool,
) -> dict[str, Any]:
    if not include_replay:
        return {"status": "skipped"}
    try:
        bars = await runtime.warm_start_symbol(deployment.symbol, provider=provider)
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    if not bars:
        return {"status": "no_bars"}
    frame = pl.DataFrame(
        {
            "symbol": [bar.symbol for bar in bars],
            "timestamp": [bar.timestamp for bar in bars],
            "open": [bar.open for bar in bars],
            "high": [bar.high for bar in bars],
            "low": [bar.low for bar in bars],
            "close": [bar.close for bar in bars],
            "volume": [bar.volume for bar in bars],
        }
    )
    enriched = evaluator.prepare_enriched_frames(frame, [deployment])[deployment.deployment_id]
    window_start = trading_window_start(datetime.now(UTC), trading_days)
    start_index = _start_index_for_window(enriched["timestamp"].to_list(), window_start)
    trades = evaluator.scan_trade_history_on_enriched(deployment, enriched, start_at=start_index)
    exit_categories = Counter(trade.exit_category for trade in trades)
    return {
        "status": "ok",
        "trade_count": len(trades),
        "exit_categories": dict(exit_categories),
        "open_trade_count": sum(1 for trade in trades if trade.exit_decision is None),
    }


def _load_events(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT created_at, event_type, payload FROM events ORDER BY id"
        ).fetchall()
    events: list[dict[str, Any]] = []
    for created_at, event_type, payload_text in rows:
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        events.append(
            {
                "created_at": created_at,
                "event_type": event_type,
                "payload": payload,
            }
        )
    return events


def _event_at_or_after(event: dict[str, Any], cutoff_created_at: str | None) -> bool:
    if cutoff_created_at is None:
        return True
    return str(event["created_at"]) >= cutoff_created_at


def _start_index_for_window(timestamps: list[datetime], window_start: datetime) -> int:
    for index, timestamp in enumerate(timestamps):
        if ensure_utc(timestamp) >= window_start:
            return index
    return len(timestamps)


def _latest_startup_snapshot(events: list[dict[str, Any]], deployment_id: str) -> tuple[dict[str, Any], str | None]:
    for event in reversed(events):
        if event["event_type"] != "startup_config":
            continue
        deployments = event["payload"].get("deployments") or []
        if any(str(item.get("deployment_id")) == deployment_id for item in deployments if isinstance(item, dict)):
            return event["payload"], str(event["created_at"])
    return {}, None


def _deployment_from_startup_snapshot(startup_snapshot: dict[str, Any], deployment_id: str) -> dict[str, Any] | None:
    for item in startup_snapshot.get("deployments") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("deployment_id")) == deployment_id:
            return item
    return None


def _packet_markdown(packet: dict[str, Any]) -> str:
    lines = [
        f"# {packet['deployment_id']}",
        "",
        f"- source_origin: `{packet['source_origin']}`",
        f"- authorization_mode: `{packet['authorization_mode']}`",
        f"- session_mode: `{packet['session_mode']}`",
        f"- session_id: `{packet['session_id']}`",
        f"- live_requested: `{packet['live_requested']}`",
        f"- shadow_only: `{packet['shadow_only']}`",
        f"- safe_for_live_review: `{packet['safe_for_live_review']}`",
        f"- startup_config_fingerprint: `{packet['startup_config_fingerprint']}`",
        f"- signal_true_count: `{packet['signal_true_count']}`",
        f"- trade_plan_count: `{packet['trade_plan_count']}`",
        f"- exit_plan_count: `{packet['exit_plan_count']}`",
        f"- exit_true_count: `{packet['exit_true_count']}`",
        f"- latest_lifecycle_state: `{packet['latest_lifecycle_state']}`",
    ]
    if packet["signal_reason_counts"]:
        lines.extend(["", "## Signal Reasons"])
        for reason, count in sorted(packet["signal_reason_counts"].items()):
            lines.append(f"- `{reason}`: `{count}`")
    if packet["exit_reason_counts"]:
        lines.extend(["", "## Exit Reasons"])
        for reason, count in sorted(packet["exit_reason_counts"].items()):
            lines.append(f"- `{reason}`: `{count}`")
    if packet["blocked_entry_reasons"]:
        lines.extend(["", "## Blocked Entry Reasons"])
        for reason, count in sorted(packet["blocked_entry_reasons"].items()):
            lines.append(f"- `{reason}`: `{count}`")
    if packet["runtime_issue_counts"]:
        lines.extend(["", "## Runtime Issues"])
        for category, count in sorted(packet["runtime_issue_counts"].items()):
            lines.append(f"- `{category}`: `{count}`")
    lines.extend(["", "## Replay", f"- status: `{packet['replay']['status']}`"])
    if "trade_count" in packet["replay"]:
        lines.append(f"- trade_count: `{packet['replay']['trade_count']}`")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main_async(argv))


async def _main_async(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Write per-deployment observation reports for automated shadow deployments.")
    parser.add_argument("--config-root", default="config", help="Bhiksha config root")
    parser.add_argument("--db-path", default=None, help="Override SQLite event DB path")
    parser.add_argument("--trading-days", type=int, default=3, help="Recent NYSE trading days to include in replay")
    parser.add_argument("--provider", default=None, help="Override warm-start provider for replay")
    parser.add_argument("--output-dir", default=None, help="Output directory for observation packets")
    parser.add_argument("--skip-replay", action="store_true", help="Skip the replay step and use event history only")
    parser.add_argument(
        "--session-payload",
        default=None,
        help="Optional active_session.json path. When supplied, observation reports target that session-payload lane.",
    )
    args = parser.parse_args(argv)

    packets = await write_observation_reports(
        config_root=args.config_root,
        db_path=args.db_path,
        trading_days=args.trading_days,
        provider=args.provider,
        output_dir=args.output_dir,
        include_replay=not args.skip_replay,
        session_payload_path=args.session_payload,
    )
    print(json.dumps({"reports": packets}, indent=2, sort_keys=True))
    return 0 if all(packet["safe_for_live_review"] for packet in packets) and packets else 2
