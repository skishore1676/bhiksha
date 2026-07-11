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
    "This is a modeled natural-bid fill with no displayed-size or slippage guarantee. Mid, last, "
    "ask fallback, and last-mark imputation are forbidden."
)
EVALUATOR_VERSION = "profile-evaluator-v1"
FILL_MODEL_VERSION = "next-fresh-natural-bid-v2"
SQLITE_NONBLOCKING_TIMEOUT_SECONDS = 0.001


@dataclass(slots=True, frozen=True)
class QuoteTapeMark:
    sequence: int
    source: str
    feed: str
    quote_at: datetime
    received_at: datetime
    bid: float | None
    ask: float | None
    last: float | None = None


@dataclass(slots=True, frozen=True)
class ExitEdgeCase:
    cohort_id: str
    trade_id: str
    cluster_id: str
    deployment_id: str
    symbol: str
    option_symbol: str
    entry_timestamp: datetime
    entry_premium: float
    quantity: int
    profile: ProfileExitFields
    profile_config: dict[str, Any]
    legacy_config: dict[str, Any]
    experiment: dict[str, Any]
    experiment_spec_hash: str
    quotes: tuple[QuoteTapeMark, ...]
    persisted_censor_reason: str | None = None


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

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=SQLITE_NONBLOCKING_TIMEOUT_SECONDS)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS exit_edge_cohorts (
                  cohort_id TEXT PRIMARY KEY, trade_id TEXT NOT NULL UNIQUE,
                  cluster_id TEXT NOT NULL,
                  deployment_id TEXT NOT NULL, symbol TEXT NOT NULL,
                  option_symbol TEXT NOT NULL, entry_timestamp TEXT NOT NULL,
                  entry_premium REAL NOT NULL, quantity INTEGER NOT NULL,
                  profile_config TEXT NOT NULL, legacy_config TEXT NOT NULL,
                  experiment_spec TEXT NOT NULL, experiment_spec_hash TEXT NOT NULL,
                  quote_source TEXT NOT NULL, quote_feed TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS exit_edge_quote_tape (
                  cohort_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                  source TEXT NOT NULL, feed TEXT NOT NULL,
                  quote_at TEXT NOT NULL, received_at TEXT NOT NULL,
                  bid REAL, ask REAL, last REAL, spread_pct REAL, freshness_ms REAL NOT NULL,
                  PRIMARY KEY (cohort_id, sequence),
                  FOREIGN KEY (cohort_id) REFERENCES exit_edge_cohorts(cohort_id)
                );
                CREATE TABLE IF NOT EXISTS exit_edge_censors (
                  cohort_id TEXT PRIMARY KEY, reason TEXT NOT NULL,
                  censored_at TEXT NOT NULL,
                  FOREIGN KEY (cohort_id) REFERENCES exit_edge_cohorts(cohort_id)
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
        experiment = _normalized_experiment(payload["experiment"])
        digest = experiment_spec_hash(profile, legacy, experiment)
        values = (
            str(payload["cohort_id"]), str(payload["trade_id"]), str(payload["cluster_id"]),
            str(payload["deployment_id"]), str(payload["symbol"]), str(payload["option_symbol"]),
            _parse_datetime(payload["entry_timestamp"]).isoformat(), float(payload["entry_premium"]),
            int(payload["quantity"]), _canonical_json(profile), _canonical_json(legacy),
            _canonical_json(experiment), digest, experiment["quote_source"], experiment["quote_feed"],
            datetime.now(UTC).isoformat(),
        )
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT trade_id,cluster_id,deployment_id,symbol,option_symbol,entry_timestamp,"
                "entry_premium,quantity,experiment_spec_hash,quote_source,quote_feed "
                "FROM exit_edge_cohorts WHERE cohort_id=?", (values[0],)
            ).fetchone()
            identity = (
                values[1], values[2], values[3], values[4], values[5], values[6],
                values[7], values[8], values[12], values[13], values[14],
            )
            if existing is not None:
                if tuple(existing) != identity:
                    raise ValueError("cohort identity or frozen policy config changed")
                return
            conn.execute(
                "INSERT INTO exit_edge_cohorts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values
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
            cohort_id, quote.sequence, quote.source, quote.feed,
            quote.quote_at.isoformat(), quote.received_at.isoformat(),
            quote.bid, quote.ask, quote.last, spread_pct, freshness_ms,
        )
        with self._connect() as conn:
            cohort = conn.execute(
                "SELECT quote_source,quote_feed FROM exit_edge_cohorts WHERE cohort_id=?", (cohort_id,)
            ).fetchone()
            if cohort is None:
                raise ValueError("orphan quote: cohort is not registered")
            if tuple(cohort) != (quote.source, quote.feed):
                raise ValueError("quote source/feed lineage changed")
            existing = conn.execute(
                "SELECT source,feed,quote_at,received_at,bid,ask,last,spread_pct,freshness_ms "
                "FROM exit_edge_quote_tape "
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
            conn.execute("INSERT INTO exit_edge_quote_tape VALUES (?,?,?,?,?,?,?,?,?,?,?)", values)

    def try_record_censor(self, cohort_id: str, reason: str) -> bool:
        try:
            self.record_censor(cohort_id, reason)
            return True
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return False

    def record_censor(self, cohort_id: str, reason: str) -> None:
        if not reason:
            raise ValueError("censor reason is required")
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM exit_edge_cohorts WHERE cohort_id=?", (cohort_id,)).fetchone() is None:
                raise ValueError("orphan censor: cohort is not registered")
            existing = conn.execute("SELECT reason FROM exit_edge_censors WHERE cohort_id=?", (cohort_id,)).fetchone()
            if existing is not None and existing[0] != reason:
                raise ValueError("persisted censor reason is immutable")
            conn.execute(
                "INSERT OR IGNORE INTO exit_edge_censors VALUES (?,?,?)",
                (cohort_id, reason, datetime.now(UTC).isoformat()),
            )

    def load_case(self, cohort_id: str) -> ExitEdgeCase:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cohort = conn.execute("SELECT * FROM exit_edge_cohorts WHERE cohort_id=?", (cohort_id,)).fetchone()
            if cohort is None:
                raise ValueError("cohort not found")
            quotes = conn.execute(
                "SELECT * FROM exit_edge_quote_tape WHERE cohort_id=? ORDER BY sequence", (cohort_id,)
            ).fetchall()
            censor = conn.execute("SELECT reason FROM exit_edge_censors WHERE cohort_id=?", (cohort_id,)).fetchone()
        mapping = {
            "cohort_id": cohort["cohort_id"], "trade_id": cohort["trade_id"],
            "cluster_id": cohort["cluster_id"], "deployment_id": cohort["deployment_id"],
            "symbol": cohort["symbol"], "option_symbol": cohort["option_symbol"],
            "entry_timestamp": cohort["entry_timestamp"], "entry_premium": cohort["entry_premium"],
            "quantity": cohort["quantity"], "profile": json.loads(cohort["profile_config"]),
            "legacy": json.loads(cohort["legacy_config"]),
            "experiment": json.loads(cohort["experiment_spec"]),
            "experiment_spec_hash": cohort["experiment_spec_hash"],
            "persisted_censor_reason": censor[0] if censor else None,
            "quotes": [dict(row) for row in quotes],
        }
        return _case_from_mapping(mapping)


def experiment_spec_hash(
    profile: dict[str, Any], legacy: dict[str, Any], experiment: dict[str, Any],
) -> str:
    value = {"profile": profile, "legacy": legacy, "experiment": _normalized_experiment(experiment)}
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def policy_config_hash(profile: dict[str, Any], legacy: dict[str, Any]) -> str:
    """Compatibility helper for callers that only need a policy fingerprint."""
    return hashlib.sha256(_canonical_json({"profile": profile, "legacy": legacy}).encode()).hexdigest()


def load_fixture_cases(path: str | Path) -> list[ExitEdgeCase]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    items = payload.get("cases") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise ValueError("fixture must contain a cases list")
    return [_case_from_mapping(item) for item in items]


def analyze_cases(cases: list[ExitEdgeCase]) -> dict[str, Any]:
    rows = [
        _analyze_case(case)
        for case in cases
    ]
    paired = [row for row in rows if row["status"] == "paired"]
    specs = sorted({row["experiment_spec_hash"] for row in rows})
    return {
        "schema_version": 3, "report_type": "prospective_paired_replay",
        "generated_at": datetime.now(UTC).isoformat(), "scope_boundary": SCOPE_BOUNDARY,
        "fill_model": FILL_MODEL, "experiment_spec_hashes": specs,
        "summary": _summary(paired, len(rows), heterogeneous_specs=len(specs) > 1), "cases": rows,
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
             f"- Labeled clusters: {s['cluster_count']}",
             f"- Mean profile-minus-legacy P&L: {s['mean_paired_delta_pnl_usd']}",
             f"- Median cluster-uplift 95% lower bound: "
             f"{s['confidence']['median_cluster_uplift_one_sided_95_lower_usd']}",
             f"- Confidence: {s['confidence']['indicator']} — {s['confidence']['reason']}",
             f"- Frozen experiment hashes: {', '.join(report['experiment_spec_hashes'])}", ""]
    return "\n".join(lines)


def _analyze_case(case: ExitEdgeCase) -> dict[str, Any]:
    max_freshness_ms = int(case.experiment["max_freshness_ms"])
    max_sequence_gap = int(case.experiment["max_sequence_gap"])
    fill_latency_ms = int(case.experiment["fill_latency_ms"])
    base = {"cohort_id": case.cohort_id, "trade_id": case.trade_id,
            "cluster_id": case.cluster_id, "deployment_id": case.deployment_id,
            "experiment_spec": case.experiment,
            "experiment_spec_hash": case.experiment_spec_hash,
            "quote_count": len(case.quotes)}
    if case.persisted_censor_reason:
        return {**base, "status": "insufficient_data",
                "insufficient_reason": f"persisted_censor:{case.persisted_censor_reason}"}
    problem = _tape_problem(case, max_freshness_ms, max_sequence_gap)
    if problem:
        return {**base, "status": "insufficient_data", "insufficient_reason": problem}
    profile = _replay(case, "profile", fill_latency_ms, max_freshness_ms)
    legacy = _replay(case, "legacy", fill_latency_ms, max_freshness_ms)
    terminal_at = max(
        _parse_datetime(outcome.exit_timestamp) for outcome in (profile, legacy) if outcome is not None
    ) if profile is not None or legacy is not None else None
    bids = [q.bid for q in case.quotes if q.bid is not None and q.bid > 0
            and q.quote_at >= case.entry_timestamp
            and (terminal_at is None or q.received_at <= terminal_at)]
    row = {**base,
           "holding_window_end": terminal_at.isoformat() if terminal_at is not None else None,
           "mfe_pct": round((max(bids)-case.entry_premium)/case.entry_premium*100, 2) if bids else None,
           "mae_pct": round((min(bids)-case.entry_premium)/case.entry_premium*100, 2) if bids else None}
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
    if experiment_spec_hash(case.profile_config, case.legacy_config, case.experiment) != case.experiment_spec_hash:
        return "experiment_spec_hash_mismatch"
    if (
        case.experiment["evaluator_version"] != EVALUATOR_VERSION
        or case.experiment["fill_model_version"] != FILL_MODEL_VERSION
    ):
        return "unsupported_evaluator_or_fill_model_version"
    if len(case.quotes) < 2: return "quote_tape_too_short_for_next_tick_fill"
    previous = None
    for quote in case.quotes:
        if not quote.source:
            return "missing_quote_source"
        if not quote.feed:
            return "missing_quote_feed"
        if (quote.source, quote.feed) != (
            case.experiment["quote_source"], case.experiment["quote_feed"]
        ):
            return "quote_source_or_feed_transition"
        if quote.quote_at < case.entry_timestamp or quote.received_at < case.entry_timestamp:
            return "quote_precedes_entry"
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


def _summary(
    paired: list[dict[str, Any]], case_count: int, *, heterogeneous_specs: bool,
) -> dict[str, Any]:
    deltas = [float(row["paired_delta_pnl_usd"]) for row in paired]
    n = len(deltas); wins = sum(x > 0 for x in deltas); low, high = _wilson(wins, n)
    clusters: dict[str, list[float]] = {}
    for row, delta in zip(paired, deltas):
        clusters.setdefault(str(row["cluster_id"]), []).append(delta)
    cluster_uplifts = [fmean(values) for values in clusters.values()]
    lower_bound = _one_sided_median_lower_bound(cluster_uplifts)
    total = sum(deltas) if deltas else None
    mean = fmean(deltas) if deltas else None
    if heterogeneous_specs:
        indicator = "heterogeneous_experiment_specs"
        reason = "Cases with different frozen experiment specs cannot share an uplift estimate."
    elif len(cluster_uplifts) < 8:
        indicator = "insufficient_cluster_sample"
        reason = "Fewer than 8 independent labeled clusters; no directional uplift claim."
    elif lower_bound is not None and lower_bound > 0 and total is not None and total > 0 and mean is not None and mean > 0:
        indicator = "directional_profile_uplift"
        reason = "The one-sided 95% distribution-free lower bound on median cluster uplift is positive."
    else:
        indicator = "inconclusive"
        reason = "The conservative cluster-uplift bound is nonpositive or aggregate uplift is nonpositive."
    return {
        "case_count": case_count, "paired_count": n, "insufficient_count": case_count-n,
        "cluster_count": len(cluster_uplifts), "cluster_labels_present": all(row.get("cluster_id") for row in paired),
        "homogeneous_experiment_spec": not heterogeneous_specs,
        "total_paired_delta_pnl_usd": round(total, 2) if total is not None else None,
        "mean_paired_delta_pnl_usd": round(mean, 2) if mean is not None else None,
        "confidence": {
            "indicator": indicator, "reason": reason,
            "inference_unit": "cluster_mean_paired_pnl_usd",
            "median_cluster_uplift_one_sided_95_lower_usd": round(lower_bound, 2) if lower_bound is not None else None,
            "positive_pairs_descriptive_only": wins,
            "positive_pair_rate_wilson_95_descriptive_only": [round(low,4),round(high,4)] if n else None,
        },
    }


def _one_sided_median_lower_bound(values: list[float], alpha: float = 0.05) -> float | None:
    """Distribution-free one-sided lower confidence bound for the population median."""
    n = len(values)
    if not n:
        return None
    chosen_k = 0
    cumulative = 0.0
    for failures in range(n):
        cumulative += math.comb(n, failures) * (0.5 ** n)
        candidate_k = failures + 1
        if cumulative <= alpha:
            chosen_k = candidate_k
        else:
            break
    if chosen_k == 0:
        return None
    return sorted(values)[chosen_k - 1]


def _wilson(k:int,n:int,z:float=1.96)->tuple[float,float]:
    if not n:return 0.0,1.0
    p=k/n; d=1+z*z/n; c=(p+z*z/(2*n))/d; m=z*math.sqrt((p*(1-p)+z*z/(4*n))/n)/d
    return max(0,c-m),min(1,c+m)


def _case_from_mapping(item: dict[str, Any]) -> ExitEdgeCase:
    profile=dict(item["profile"]); legacy=dict(item["legacy"])
    experiment = _normalized_experiment(item["experiment"])
    digest=str(item.get("experiment_spec_hash") or experiment_spec_hash(profile,legacy,experiment))
    return ExitEdgeCase(str(item["cohort_id"]),str(item["trade_id"]),str(item["cluster_id"]),str(item["deployment_id"]),
        str(item["symbol"]),str(item["option_symbol"]),_parse_datetime(item["entry_timestamp"]),
        float(item["entry_premium"]),int(item["quantity"]),
        ProfileExitFields.from_exit_params(str(profile.get("profile_exit_id") or "unknown_profile"),profile,
                                           fallback_stop_pct=float(profile.get("stop_loss_pct") or 0.45)),
        profile,legacy,experiment,digest,tuple(QuoteTapeMark(int(q["sequence"]),str(q["source"]),str(q["feed"]),_parse_datetime(q["quote_at"]),
            _parse_datetime(q["received_at"]),_float_or_none(q.get("bid")),_float_or_none(q.get("ask")),
            _float_or_none(q.get("last"))) for q in item.get("quotes",[])),
        str(item["persisted_censor_reason"]) if item.get("persisted_censor_reason") else None)


def _normalized_experiment(value: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "fill_latency_ms": int(value["fill_latency_ms"]),
        "max_freshness_ms": int(value["max_freshness_ms"]),
        "max_sequence_gap": int(value["max_sequence_gap"]),
        "evaluator_version": str(value.get("evaluator_version") or EVALUATOR_VERSION),
        "fill_model_version": str(value.get("fill_model_version") or FILL_MODEL_VERSION),
        "quote_source": str(value["quote_source"]),
        "quote_feed": str(value["quote_feed"]),
    }
    if not normalized["quote_source"] or not normalized["quote_feed"]:
        raise ValueError("quote source/feed lineage is required")
    if normalized["fill_latency_ms"] < 0 or normalized["max_freshness_ms"] < 0:
        raise ValueError("experiment timing knobs must be nonnegative")
    if normalized["max_sequence_gap"] < 1:
        raise ValueError("max_sequence_gap must be positive")
    return normalized


def _canonical_json(value: Any)->str:return json.dumps(value,sort_keys=True,separators=(",",":"))
def _float_or_none(value:Any)->float|None:return None if value is None else float(value)
def _parse_datetime(value:Any)->datetime:
    parsed=datetime.fromisoformat(str(value).replace("Z","+00:00"))
    if parsed.tzinfo is None: raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(UTC)
def _optional_datetime(value:Any)->datetime|None:return None if value in (None,"") else _parse_datetime(value)


__all__=["ProspectiveQuoteTapeRepository","QuoteTapeMark","analyze_cases",
         "build_historical_coverage_report","experiment_spec_hash","load_fixture_cases","policy_config_hash",
         "render_markdown","write_exit_edge_report"]
