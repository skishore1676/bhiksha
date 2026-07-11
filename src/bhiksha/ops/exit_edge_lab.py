"""Observational, read-only paired exit experiment utilities.

Historical Bhiksha data is suitable only for a coverage audit: its quote stream
usually ends at the authoritative exit and older profile events are not keyed by
trade.  Actual paired estimates therefore require a prospective, append-only
quote tape that continues until both virtual policies terminate.

Nothing here imports a broker/order manager or mutates runtime/profile state.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from statistics import fmean
from typing import Any
from zoneinfo import ZoneInfo

from bhiksha.execution.profile_exit import (
    ProfileExitFields,
    ProfileExitState,
    ProfileFsmAction,
    ProfileMarketView,
    evaluate_profile_exit,
)

ET = ZoneInfo("America/New_York")
SCOPE_BOUNDARY = (
    "Same actual entry, contract, quantity, and prospective quote tape; current-profile versus "
    "legacy mechanics only. Observational counterfactual evidence, not causal proof or "
    "cross-profile playbook evidence (tradelab ADR-011)."
)
FILL_MODEL = (
    "A trigger never fills on its triggering observation. Each long-option exit fills at the "
    "first later-sequence, fresh, non-crossed quote's executable bid after configured latency. "
    "Mid, last, ask fallback, and last-mark imputation are forbidden."
)


@dataclass(slots=True, frozen=True)
class QuoteTapeMark:
    sequence: int
    source: str
    quote_at: datetime
    received_at: datetime
    bid: float | None
    ask: float | None
    last: float | None = None


@dataclass(slots=True, frozen=True)
class ExitEdgeCase:
    cohort_id: str
    trade_id: str
    deployment_id: str
    symbol: str
    option_symbol: str
    entry_timestamp: datetime
    entry_premium: float
    quantity: int
    profile: ProfileExitFields
    profile_config: dict[str, Any]
    legacy_config: dict[str, Any]
    policy_config_hash: str
    quotes: tuple[QuoteTapeMark, ...]


@dataclass(slots=True, frozen=True)
class PolicyOutcome:
    policy: str
    exit_timestamp: str
    exit_rule: str
    realized_pnl_usd: float
    time_in_trade_seconds: float
    legs: tuple[dict[str, Any], ...]


class ProspectiveQuoteTapeRepository:
    """Separate experiment store; never share this DB with the trading runtime.

    Callers must feed quotes from an existing cache/feed or isolated low-priority
    quota. This class performs no network or broker calls. ``try_*`` methods are
    fail-open with respect to trading: failures return False for experiment
    censoring and are never raised into the caller's live decision path.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS exit_edge_cohorts (
                  cohort_id TEXT PRIMARY KEY, trade_id TEXT NOT NULL UNIQUE,
                  deployment_id TEXT NOT NULL, symbol TEXT NOT NULL,
                  option_symbol TEXT NOT NULL, entry_timestamp TEXT NOT NULL,
                  entry_premium REAL NOT NULL, quantity INTEGER NOT NULL,
                  profile_config TEXT NOT NULL, legacy_config TEXT NOT NULL,
                  policy_config_hash TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS exit_edge_quote_tape (
                  cohort_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                  source TEXT NOT NULL,
                  quote_at TEXT NOT NULL, received_at TEXT NOT NULL,
                  bid REAL, ask REAL, last REAL, spread_pct REAL, freshness_ms REAL NOT NULL,
                  PRIMARY KEY (cohort_id, sequence)
                );
                """
            )

    def try_initialize(self) -> bool:
        try:
            self.initialize()
            return True
        except (OSError, sqlite3.Error):
            return False

    def try_register_cohort(self, payload: dict[str, Any]) -> bool:
        try:
            self.register_cohort(payload)
            return True
        except (KeyError, OSError, sqlite3.Error, TypeError, ValueError):
            return False

    def register_cohort(self, payload: dict[str, Any]) -> None:
        profile = payload["profile"]
        legacy = payload["legacy"]
        digest = policy_config_hash(profile, legacy)
        values = (
            str(payload["cohort_id"]), str(payload["trade_id"]), str(payload["deployment_id"]),
            str(payload["symbol"]), str(payload["option_symbol"]),
            _parse_datetime(payload["entry_timestamp"]).isoformat(), float(payload["entry_premium"]),
            int(payload["quantity"]), _canonical_json(profile), _canonical_json(legacy), digest,
            datetime.now(UTC).isoformat(),
        )
        with sqlite3.connect(self.path) as conn:
            existing = conn.execute(
                "SELECT trade_id, option_symbol, entry_premium, quantity, policy_config_hash "
                "FROM exit_edge_cohorts WHERE cohort_id=?", (values[0],)
            ).fetchone()
            identity = (values[1], values[4], values[6], values[7], values[10])
            if existing is not None:
                if tuple(existing) != identity:
                    raise ValueError("cohort identity or frozen policy config changed")
                return
            conn.execute(
                "INSERT INTO exit_edge_cohorts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", values
            )

    def try_append_quote(self, cohort_id: str, quote: QuoteTapeMark) -> bool:
        try:
            self.append_quote(cohort_id, quote)
            return True
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return False

    def append_quote(self, cohort_id: str, quote: QuoteTapeMark) -> None:
        freshness_ms = (quote.received_at - quote.quote_at).total_seconds() * 1000
        spread_pct = (
            (quote.ask - quote.bid) / quote.bid
            if quote.bid is not None and quote.bid > 0 and quote.ask is not None
            else None
        )
        values = (
            cohort_id, quote.sequence, quote.source, quote.quote_at.isoformat(), quote.received_at.isoformat(),
            quote.bid, quote.ask, quote.last, spread_pct, freshness_ms,
        )
        with sqlite3.connect(self.path) as conn:
            existing = conn.execute(
                "SELECT source,quote_at,received_at,bid,ask,last,spread_pct,freshness_ms FROM exit_edge_quote_tape "
                "WHERE cohort_id=? AND sequence=?", (cohort_id, quote.sequence)
            ).fetchone()
            if existing is not None:
                if tuple(existing) != values[2:]:
                    raise ValueError("sequence conflict in append-only quote tape")
                return
            last_seq = conn.execute(
                "SELECT MAX(sequence) FROM exit_edge_quote_tape WHERE cohort_id=?", (cohort_id,)
            ).fetchone()[0]
            if last_seq is not None and quote.sequence <= int(last_seq):
                raise ValueError("out-of-order quote sequence")
            conn.execute("INSERT INTO exit_edge_quote_tape VALUES (?,?,?,?,?,?,?,?,?,?)", values)


def policy_config_hash(profile: dict[str, Any], legacy: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json({"profile": profile, "legacy": legacy}).encode()).hexdigest()


def load_fixture_cases(path: str | Path) -> list[ExitEdgeCase]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    items = payload.get("cases") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ValueError("fixture must contain a cases list")
    return [_case_from_mapping(item) for item in items]


def analyze_cases(
    cases: list[ExitEdgeCase], *, max_freshness_ms: int = 2_000,
    max_sequence_gap: int = 1, fill_latency_ms: int = 0,
) -> dict[str, Any]:
    rows = [
        _analyze_case(case, max_freshness_ms=max_freshness_ms,
                      max_sequence_gap=max_sequence_gap, fill_latency_ms=fill_latency_ms)
        for case in cases
    ]
    paired = [row for row in rows if row["status"] == "paired"]
    deltas = [float(row["paired_delta_pnl_usd"]) for row in paired]
    return {
        "schema_version": 2, "report_type": "prospective_paired_replay",
        "generated_at": datetime.now(UTC).isoformat(), "scope_boundary": SCOPE_BOUNDARY,
        "fill_model": FILL_MODEL, "summary": _summary(deltas, len(rows)), "cases": rows,
    }


def build_historical_coverage_report(
    db_path: str | Path, *, start: date | str, end: date | str,
) -> dict[str, Any]:
    """Audit historical pairing eligibility without manufacturing outcomes."""
    start_day = date.fromisoformat(start) if isinstance(start, str) else start
    end_day = date.fromisoformat(end) if isinstance(end, str) else end
    start_at = datetime.combine(start_day, datetime.min.time(), tzinfo=UTC).isoformat()
    end_at = datetime.combine(end_day, datetime.max.time(), tzinfo=UTC).isoformat()
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        trades = conn.execute(
            "SELECT trade_id,deployment_id,option_symbol,entry_timestamp,exit_filled_at "
            "FROM trade_sessions WHERE status='closed' AND entry_timestamp BETWEEN ? AND ?",
            (start_at, end_at),
        ).fetchall()
        marks = conn.execute(
            "SELECT created_at,payload FROM events WHERE event_type='shadow_mark' "
            "AND created_at BETWEEN ? AND ? ORDER BY id", (start_at, end_at)
        ).fetchall()
        profile_rows = conn.execute(
            "SELECT payload FROM events WHERE event_type='profile_exit_shadow' "
            "AND created_at BETWEEN ? AND ?", (start_at, end_at)
        ).fetchall()
    by_trade: dict[str, list[datetime]] = {}
    required_quote_fields = {"trade_id", "quote_at", "received_at", "sequence", "bid", "ask"}
    profile_complete = 0
    for row in profile_rows:
        try:
            payload = json.loads(row["payload"])
            if required_quote_fields.issubset(payload):
                profile_complete += 1
        except (TypeError, json.JSONDecodeError):
            pass
    for row in marks:
        try:
            payload = json.loads(row["payload"])
            trade_id = str(payload["trade_id"])
            by_trade.setdefault(trade_id, []).append(_parse_datetime(row["created_at"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    no_marks = 0
    no_post_exit = 0
    with_post_exit = 0
    for trade in trades:
        tape = by_trade.get(str(trade["trade_id"]), [])
        if not tape:
            no_marks += 1
            continue
        exited = _optional_datetime(trade["exit_filled_at"])
        if exited is not None and any(mark > exited for mark in tape):
            with_post_exit += 1
        else:
            no_post_exit += 1
    total = len(trades)
    return {
        "schema_version": 2, "report_type": "historical_pairing_coverage",
        "generated_at": datetime.now(UTC).isoformat(), "scope_boundary": SCOPE_BOUNDARY,
        "window": {"start": start_day.isoformat(), "end": end_day.isoformat()},
        "verdict": "historical_data_ineligible_for_paired_outcome_estimation",
        "counts": {
            "closed_trades": total, "trades_without_trade_keyed_shadow_marks": no_marks,
            "trades_with_marks_but_no_post_exit_mark": no_post_exit,
            "trades_with_any_post_exit_mark": with_post_exit,
            "profile_events_with_all_prospective_quote_fields": profile_complete,
            "eligible_paired_trades": 0,
        },
        "blocking_reasons": [
            "policy configs were not frozen and hashed at entry",
            "profile_exit_shadow lacks immutable trade/cohort key and provider quote timestamp",
            "historical quote capture generally stops at the authoritative exit",
            "no guaranteed next-tick executable-bid fill after both virtual triggers",
        ],
    }


def write_exit_edge_report(report: dict[str, Any], output_dir: str | Path) -> tuple[Path, Path]:
    target = Path(output_dir); target.mkdir(parents=True, exist_ok=True)
    json_path = target / "exit_edge_lab.json"; md_path = target / "exit_edge_lab.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def render_markdown(report: dict[str, Any]) -> str:
    if report["report_type"] == "historical_pairing_coverage":
        c = report["counts"]
        return "\n".join([
            "# Exit Edge Lab — Historical Coverage", "", f"**Verdict:** `{report['verdict']}`", "",
            f"- Closed trades: {c['closed_trades']}",
            f"- No trade-keyed marks: {c['trades_without_trade_keyed_shadow_marks']}",
            f"- Marks but no post-exit mark: {c['trades_with_marks_but_no_post_exit_mark']}",
            f"- Any post-exit mark: {c['trades_with_any_post_exit_mark']}",
            f"- Eligible complete pairs: {c['eligible_paired_trades']}", "",
            "Historical P&L buckets are observational and confounded; they are not paired evidence.", "",
            f"**Boundary:** {report['scope_boundary']}", "",
        ])
    s = report["summary"]
    lines = ["# Exit Edge Lab — Prospective Paired Replay", "", f"**Boundary:** {report['scope_boundary']}",
             "", f"**Fill model:** {report['fill_model']}", "", f"- Cases: {s['case_count']}",
             f"- Paired: {s['paired_count']}", f"- Insufficient: {s['insufficient_count']}",
             f"- Mean profile-minus-legacy P&L: {s['mean_paired_delta_pnl_usd']}",
             f"- Confidence: {s['confidence']['indicator']}", ""]
    return "\n".join(lines)


def _analyze_case(case: ExitEdgeCase, *, max_freshness_ms: int, max_sequence_gap: int,
                  fill_latency_ms: int) -> dict[str, Any]:
    base = {"cohort_id": case.cohort_id, "trade_id": case.trade_id,
            "deployment_id": case.deployment_id, "policy_config_hash": case.policy_config_hash,
            "quote_count": len(case.quotes)}
    problem = _tape_problem(case, max_freshness_ms, max_sequence_gap)
    if problem:
        return {**base, "status": "insufficient_data", "insufficient_reason": problem}
    profile = _replay(case, "profile", fill_latency_ms, max_freshness_ms)
    legacy = _replay(case, "legacy", fill_latency_ms, max_freshness_ms)
    bids = [q.bid for q in case.quotes if q.bid is not None and q.bid > 0]
    row = {**base, "mfe_pct": round((max(bids)-case.entry_premium)/case.entry_premium*100, 2),
           "mae_pct": round((min(bids)-case.entry_premium)/case.entry_premium*100, 2)}
    if profile is None or legacy is None:
        missing = [name for name, value in (("profile", profile), ("legacy", legacy)) if value is None]
        return {**row, "status": "insufficient_data",
                "insufficient_reason": "right_censored:" + ",".join(missing),
                "profile_outcome": asdict(profile) if profile else None,
                "legacy_outcome": asdict(legacy) if legacy else None}
    return {**row, "status": "paired", "insufficient_reason": None,
            "profile_outcome": asdict(profile), "legacy_outcome": asdict(legacy),
            "paired_delta_pnl_usd": round(profile.realized_pnl_usd-legacy.realized_pnl_usd, 2),
            "paired_delta_time_in_trade_seconds": round(profile.time_in_trade_seconds-legacy.time_in_trade_seconds, 3)}


def _replay(case: ExitEdgeCase, policy: str, latency_ms: int, max_freshness_ms: int) -> PolicyOutcome | None:
    state = ProfileExitState.new(case.entry_premium, seed_quantity=case.quantity)
    remaining = case.quantity; pnl = 0.0; legs: list[dict[str, Any]] = []
    pending: tuple[QuoteTapeMark, str, int, bool] | None = None
    stop_pct = float(case.legacy_config["stop_loss_pct"])
    target_pct = case.legacy_config.get("profit_target_pct")
    for quote in case.quotes:
        if pending is not None:
            trigger, rule, qty, partial = pending
            age_ms = (quote.received_at-quote.quote_at).total_seconds()*1000
            elapsed_ms = (quote.received_at-trigger.received_at).total_seconds()*1000
            if quote.sequence > trigger.sequence and elapsed_ms >= latency_ms and _executable(quote, age_ms, max_freshness_ms):
                fill = float(quote.bid); qty = min(qty, remaining)
                pnl += (fill-case.entry_premium)*qty*100; remaining -= qty
                legs.append({"trigger_sequence": trigger.sequence, "fill_sequence": quote.sequence,
                             "fill_at": quote.received_at.isoformat(), "rule": rule, "quantity": qty,
                             "fill_bid": fill})
                pending = None
                if remaining <= 0 or not partial:
                    return PolicyOutcome(policy, quote.received_at.isoformat(), rule, round(pnl, 2),
                                         round((quote.received_at-case.entry_timestamp).total_seconds(), 3), tuple(legs))
                continue
            continue
        age_ms = (quote.received_at-quote.quote_at).total_seconds()*1000
        if not _executable(quote, age_ms, max_freshness_ms):
            continue
        bid = float(quote.bid)
        if policy == "profile":
            decision = evaluate_profile_exit(
                fields=case.profile, entry_premium=case.entry_premium, quantity=case.quantity,
                market=ProfileMarketView(current_premium=bid,
                    bar_time_et=quote.quote_at.astimezone(ET).time().replace(tzinfo=None), bid=bid, ask=quote.ask, last=quote.last),
                entry_time=case.entry_timestamp, now=quote.quote_at, state=state, require_bar_time_for_eod=True)
            if decision.exit:
                partial = decision.fsm_action is ProfileFsmAction.PARTIAL_SCALE
                pending = (quote, decision.rule.value, decision.exit_quantity or remaining, partial)
        else:
            rule = None
            if bid <= case.entry_premium*(1-stop_pct): rule = "legacy_option_stop"
            elif target_pct is not None and bid >= case.entry_premium*(1+float(target_pct)): rule = "legacy_full_target"
            elif quote.quote_at.astimezone(ET).strftime("%H:%M") >= str(case.legacy_config.get("hard_flat_time_et", "15:55")):
                rule = "legacy_eod_flat"
            if rule: pending = (quote, rule, remaining, False)
    return None


def _tape_problem(case: ExitEdgeCase, max_freshness_ms: int, max_sequence_gap: int) -> str | None:
    if case.entry_premium <= 0 or case.quantity <= 0: return "invalid_entry"
    if policy_config_hash(case.profile_config, case.legacy_config) != case.policy_config_hash:
        return "policy_config_hash_mismatch"
    if len(case.quotes) < 2: return "quote_tape_too_short_for_next_tick_fill"
    previous = None
    for quote in case.quotes:
        if not quote.source:
            return "missing_quote_source"
        if previous is not None:
            if quote.sequence <= previous.sequence: return "duplicate_or_out_of_order_sequence"
            if quote.sequence-previous.sequence > max_sequence_gap: return "sequence_gap"
            if quote.quote_at < previous.quote_at or quote.received_at < previous.received_at:
                return "out_of_order_timestamp"
        age = (quote.received_at-quote.quote_at).total_seconds()*1000
        if age < 0: return "quote_received_before_provider_timestamp"
        if age > max_freshness_ms: return "stale_quote_gap"
        if quote.bid is None or quote.bid <= 0: return "missing_executable_bid"
        if quote.ask is None or quote.ask <= 0: return "missing_ask"
        if quote.ask < quote.bid: return "crossed_quote"
        previous = quote
    return None


def _executable(q: QuoteTapeMark, age_ms: float, limit: int) -> bool:
    return bool(q.bid is not None and q.bid > 0 and q.ask is not None and q.ask >= q.bid and 0 <= age_ms <= limit)


def _summary(deltas: list[float], case_count: int) -> dict[str, Any]:
    n=len(deltas); wins=sum(x>0 for x in deltas); low,high=_wilson(wins,n)
    indicator="insufficient_sample" if n<8 else ("directional_profile_edge" if low>0.5 else "inconclusive")
    return {"case_count":case_count,"paired_count":n,"insufficient_count":case_count-n,
            "total_paired_delta_pnl_usd":round(sum(deltas),2) if deltas else None,
            "mean_paired_delta_pnl_usd":round(fmean(deltas),2) if deltas else None,
            "confidence":{"indicator":indicator,"positive_pairs":wins,
                          "positive_pair_rate_wilson_95":[round(low,4),round(high,4)] if n else None}}


def _wilson(k:int,n:int,z:float=1.96)->tuple[float,float]:
    if not n:return 0.0,1.0
    p=k/n; d=1+z*z/n; c=(p+z*z/(2*n))/d; m=z*math.sqrt((p*(1-p)+z*z/(4*n))/n)/d
    return max(0,c-m),min(1,c+m)


def _case_from_mapping(item: dict[str, Any]) -> ExitEdgeCase:
    profile=dict(item["profile"]); legacy=dict(item["legacy"])
    digest=str(item.get("policy_config_hash") or policy_config_hash(profile,legacy))
    return ExitEdgeCase(str(item["cohort_id"]),str(item["trade_id"]),str(item["deployment_id"]),
        str(item["symbol"]),str(item["option_symbol"]),_parse_datetime(item["entry_timestamp"]),
        float(item["entry_premium"]),int(item["quantity"]),
        ProfileExitFields.from_exit_params(str(profile.get("profile_exit_id") or "unknown_profile"),profile,
                                           fallback_stop_pct=float(profile.get("stop_loss_pct") or 0.45)),
        profile,legacy,digest,tuple(QuoteTapeMark(int(q["sequence"]),str(q["source"]),_parse_datetime(q["quote_at"]),
            _parse_datetime(q["received_at"]),_float_or_none(q.get("bid")),_float_or_none(q.get("ask")),
            _float_or_none(q.get("last"))) for q in item.get("quotes",[])))


def _canonical_json(value: Any)->str:return json.dumps(value,sort_keys=True,separators=(",",":"))
def _float_or_none(value:Any)->float|None:return None if value is None else float(value)
def _parse_datetime(value:Any)->datetime:
    parsed=datetime.fromisoformat(str(value).replace("Z","+00:00"))
    if parsed.tzinfo is None: raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(UTC)
def _optional_datetime(value:Any)->datetime|None:return None if value in (None,"") else _parse_datetime(value)


__all__=["ProspectiveQuoteTapeRepository","QuoteTapeMark","analyze_cases",
         "build_historical_coverage_report","load_fixture_cases","policy_config_hash",
         "render_markdown","write_exit_edge_report"]
