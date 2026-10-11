"""Read-only strategy/exit review feed. No broker, Sheet, or scheduler effects."""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from bhiksha.ops.exit_edge_lab import ProspectiveQuoteTapeRepository, analyze_cases
from bhiksha.ops.trade_observation import classify_trade_observation, index_terminal_entry_observations
from bhiksha.ops.weekly_scorecard import _augment_trade, _load_partials

CT = ZoneInfo("America/Chicago")
EVENT_TYPES = ("startup_config", "signal_evaluation", "signal_decision", "signal_outcome",
               "shadow_entry_modeled", "entry_reconcile_released", "entry_reprice_blocked",
               "entry_reprice_cancel_after_timeout")


def _stamp(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Evidence timestamps must include a timezone")
    return result.astimezone(UTC)


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _identity(deployment, revision):
    return str(deployment), str(revision or "unknown")


def summarize(inputs: dict) -> dict:
    """Pure normalization and aggregation; agent inferences are a separate input."""
    groups = {}
    signals = {}
    signal_details, trade_details, exit_details = [], [], []
    configs = {(c["deployment_id"], c["revision"]): c for c in inputs["configs"]}

    def group(key, symbol=None, lane=None):
        config = configs.get(key, {})
        # Historical lane is part of the grouping key, never today's arming state.
        lane = lane or config.get("lane", "unknown")
        # Consolidate revisions only when the entire frozen deployment content
        # is byte-equivalent after canonical JSON serialization.
        full = (key[0], config.get("config_fingerprint") or key[1], lane)
        if full not in groups:
            groups[full] = {"row_id": hashlib.sha256(json.dumps(full).encode()).hexdigest()[:20],
                "deployment_id": key[0], "revision": key[1], "revisions": set(), "lane": lane,
                "strategy": config.get("strategy", "unknown"), "version": config.get("version", "unknown"),
                "source": config.get("source", "unknown"), "symbol": symbol or config.get("symbol", "unknown"),
                "direction": config.get("direction", "unknown"), "entry_profile": config.get("entry_profile", "unknown"),
                "default_exit": config.get("default_exit", "unknown"), "evaluation_count": 0,
                "observed_sessions": set(), "signals_fired": 0, "signals_captured": 0,
                "filled": 0, "open": 0, "closed": 0, "unresolved": 0, "blocked": 0,
                "recorded_pnl_usd": None, "missing_economics": 0, "registered": 0,
                "exit_sets": defaultdict(list), "issues": set()}
        groups[full]["revisions"].add(key[1])
        return groups[full]

    inventory_revision = inputs.get("inventory_revision") or next(iter(configs), (None, None))[1]
    for key, config in configs.items():
        if key[1] == inventory_revision:
            group(key, config.get("symbol"))
    for event in inputs["events"]:
        p, kind = event["payload"], event["event_type"]
        if kind not in {"signal_evaluation", "signal_decision", "signal_outcome"}:
            continue
        ident = p.get("evidence_identity") or {}
        key = _identity(p.get("deployment_id", "unknown"), ident.get("plan_revision_id"))
        g = group(key, p.get("symbol"), p.get("mode"))
        if kind == "signal_evaluation":
            g["evaluation_count"] += 1
            g["observed_sessions"].add(_stamp(event["created_at"]).astimezone(CT).date().isoformat())
            if (p.get("features") or {}).get("evaluation_coverage") == "incomplete":
                g["issues"].add("incomplete signal evaluation")
        if kind != "signal_outcome" and p.get("signal") is not True:
            continue
        timestamp = p.get("timestamp") or event["created_at"]
        stable = str(p.get("signal_id") or f"{key[0]}:{timestamp}:{p.get('direction') or 'none'}")
        skey = (*key, stable)
        s = signals.setdefault(skey, {"group": g, "signal_id": stable,
            "observed_at": timestamp, "captured": False, "outcome": None, "trade_id": None})
        if s["group"]["lane"] == "unknown" and p.get("mode"):
            s["group"] = g
        if kind in {"signal_decision", "signal_outcome"}:
            s["captured"] = True
        if kind == "signal_outcome" and s["outcome"] != "filled" and not (
                p.get("outcome") == "pending_execution" and s["outcome"] not in {None, "pending_execution", "unknown"}):
            s["outcome"], s["trade_id"] = p.get("outcome"), p.get("trade_id")
    confirmed = {t["trade_id"] for t in inputs["trades"] if t["fill_confirmed"]}
    for s in signals.values():
        g = s.pop("group")
        g["signals_fired"] += 1
        g["signals_captured"] += int(s["captured"])
        if s["outcome"] in {None, "pending_execution", "unknown"} or (s["outcome"] == "filled" and s["trade_id"] not in confirmed):
            g["unresolved"] += 1
        elif s["outcome"] != "filled":
            g["blocked"] += 1
        signal_details.append({"row_id": g["row_id"], **s})

    trade_groups = {}
    for t in inputs["trades"]:
        g = group(_identity(t["deployment_id"], t.get("plan_revision_id")), t.get("symbol"), t["lane"])
        trade_groups[t["trade_id"]] = g
        trade_details.append({"row_id": g["row_id"], **t})
        if not t["fill_confirmed"]:
            g["issues"].add("entry fill unverified")
            continue
        g["filled"] += 1
        g["open" if t["is_open"] else "closed"] += 1
        if not t["is_open"]:
            pnl = t.get("realized_pnl_usd")
            if t.get("pnl_eligible") is False or not _finite(pnl):
                g["missing_economics"] += 1
                g["issues"].add("closed trade economics unavailable")
            else:
                g["recorded_pnl_usd"] = (g["recorded_pnl_usd"] or 0) + pnl

    for case in inputs["cases"]:
        g = trade_groups.get(case["trade_id"])
        if g is None:
            g = group(_identity(case["deployment_id"], None), case.get("symbol"),
                      (case.get("cohort_dimensions") or {}).get("runtime_mode", "unknown"))
            g["issues"].add("exit cohort has no matching trade identity")
        g["registered"] += 1
        profiles = (case.get("experiment_spec") or {}).get("named_profiles") or []
        outcomes = case.get("named_exit_outcomes") or {}
        if not profiles:
            g["issues"].add("historical non-named evaluator; not pooled with named exits")
        for profile in profiles:
            policy = profile["policy_id"]
            outcome = outcomes.get(policy) or {}
            status = (case.get("arm_evidence_status") or {}).get(policy, "unavailable")
            clean = status == "complete_clean" and _finite(outcome.get("realized_pnl_usd"))
            if case.get("insufficient_reason", "") and str(case["insufficient_reason"]).startswith("persisted_censor:"):
                clean = False
            record = {"row_id": g["row_id"], "trade_id": case["trade_id"], "policy": policy,
                "experiment_hash": case["experiment_spec_hash"], "evidence_status": status,
                "pnl_usd": outcome.get("realized_pnl_usd") if clean else None,
                "clean": clean, "exit_at": outcome.get("exit_timestamp"),
                "limitation": case.get("insufficient_reason"),
                "baseline": profiles[0]["policy_id"]}
            g["exit_sets"][case["experiment_spec_hash"]].append(record)
            exit_details.append(record)

    exit_columns = sorted({r["policy"] for r in exit_details})
    rows = []
    for g in groups.values():
        # A different frozen experiment gets its own report row. Funnel/actual
        # counts are repeated context, not additive across experiment rows.
        sets = g.pop("exit_sets") or {"none": []}
        for frozen, details in sorted(sets.items()):
            r = {**g, "row_id": g["row_id"] + ":" + hashlib.sha256(frozen.encode()).hexdigest()[:16], "experiment_hash": frozen,
                 "group_id": g["row_id"], "observed_sessions": len(g["observed_sessions"]),
                 "coverage": "partial" if g["evaluation_count"] else "unknown"}
            r["revisions"] = sorted(g["revisions"])
            r["revision"] = r["revisions"][0] if len(r["revisions"]) == 1 else f"{len(r['revisions'])} identical-config revisions"
            r["issues"] = sorted(g["issues"])
            if not g["evaluation_count"] and not g["signals_fired"]:
                r["signals_fired"] = r["signals_captured"] = None
            if g["missing_economics"]:
                r["recorded_pnl_usd"] = None
            elif r["recorded_pnl_usd"] is not None:
                r["recorded_pnl_usd"] = round(r["recorded_pnl_usd"], 2)
            by_trade = defaultdict(dict)
            policies = {d["policy"] for d in details}
            for d in details:
                by_trade[d["trade_id"]][d["policy"]] = d
            common = [arms for arms in by_trade.values() if policies and policies <= arms.keys()
                      and all(arms[p]["clean"] for p in policies)]
            r["common_comparisons"] = len(common)
            r["modeled_pnl"] = {p: round(sum(arms[p]["pnl_usd"] for arms in common), 2)
                                if common and p in policies else None for p in exit_columns}
            r["modeled_completed"] = {p: sum(d["clean"] for d in details if d["policy"] == p) for p in exit_columns}
            r["inference"] = None
            rows.append(r)
    return {"schema": "bhiksha.strategy-exit-report.v1", "window": inputs["window"],
            "generated_at": datetime.now(UTC).isoformat(), "fees_usd": 0,
            "exit_columns": exit_columns, "rows": sorted(rows, key=lambda r: (r["strategy"], r["symbol"], r["lane"], r["row_id"])),
            "signals": signal_details, "trades": trade_details, "exits": exit_details,
            "source_receipt": inputs.get("source_receipt", {})}


def extract(db_path, edge_db_path, start: date, end: date) -> dict:
    """Read a consistent transaction from each store. Stores are not atomic together."""
    if end < start:
        raise ValueError("end precedes start")
    lower = datetime.combine(start, time.min, CT).astimezone(UTC)
    upper = datetime.combine(end + timedelta(days=1), time.min, CT).astimezone(UTC)
    events, configs, trades = [], {}, []
    inventory_revision = None
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=.25)) as c:
        c.row_factory = sqlite3.Row
        c.execute("BEGIN")
        maximum = c.execute("SELECT MAX(id) FROM events").fetchone()[0]
        # Read sparse startup receipts first. Each revision is kept separate.
        anchor = 0
        for raw in c.execute("SELECT id,created_at,payload FROM events WHERE event_type='startup_config' ORDER BY id DESC"):
            at = _stamp(raw["created_at"])
            if at >= upper:
                continue
            payload = json.loads(raw["payload"])
            active = payload.get("active_plan") or {}
            revision = active.get("plan_revision_id") or "unknown"
            inventory_revision = inventory_revision or revision
            for d in payload.get("deployments") or []:
                s, x = d.get("strategy") or {}, d.get("execution") or {}
                source = d.get("source") or {}
                meta = source.get("metadata") or {}
                config = {"deployment_id": d["deployment_id"], "revision": revision,
                    "config_fingerprint": hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest(),
                    "strategy": meta.get("strategy_class") or meta.get("strategy_name") or s.get("key", "unknown"),
                    "version": s.get("version", "unknown"), "source": meta.get("source_owner") or source.get("origin", "unknown"),
                    "symbol": d.get("symbol"), "direction": (s.get("params") or {}).get("direction", "unknown"),
                    "lane": "shadow" if x.get("shadow_only") else "live",
                    "entry_profile": x.get("entry_execution_profile") or "legacy",
                    "default_exit": (d.get("exit") or {}).get("exit_policy_id", "unknown")}
                configs.setdefault((d["deployment_id"], revision), config)
            if at < lower:
                anchor = raw["id"]
                break
        placeholders = ",".join("?" for _ in EVENT_TYPES[1:])
        for raw in c.execute(f"SELECT id,created_at,event_type,payload FROM events WHERE id>=? AND id<=? AND event_type IN ({placeholders}) ORDER BY id", (anchor, maximum, *EVENT_TYPES[1:])):
            if not lower <= _stamp(raw["created_at"]) < upper:
                continue
            p = json.loads(raw["payload"])
            # Never export full startup configuration, broker payloads or credentials.
            allowed = ("deployment_id", "symbol", "timestamp", "signal", "direction", "signal_id", "mode",
                       "trade_id", "outcome", "evidence_identity", "reason", "safe_to_close", "order_status", "filled_quantity")
            slim = {k: p[k] for k in allowed if k in p}
            slim["features"] = {"evaluation_coverage": (p.get("features") or {}).get("evaluation_coverage")}
            # Classifier needs full terminal evidence, retained in memory only.
            events.append({"event_id": raw["id"], "created_at": raw["created_at"],
                           "event_type": raw["event_type"], "payload": p if raw["event_type"].startswith("entry_") else slim})
        terminal = index_terminal_entry_observations(events)
        modeled = {e["payload"].get("trade_id") for e in events if e["event_type"] == "shadow_entry_modeled"}
        raw_trades = [dict(r) for r in c.execute("SELECT * FROM trade_sessions WHERE entry_timestamp>=? AND entry_timestamp<?", (lower.isoformat(), upper.isoformat()))]
        partials = _load_partials(c, [t["trade_id"] for t in raw_trades])
        if partials:
            # The existing helper omits timestamps. Filter confirmed legs by
            # their actual fill time before reusing its economics calculation.
            for trade_id in partials:
                partials[trade_id] = [dict(r) for r in c.execute(
                    "SELECT * FROM trade_partial_fills WHERE trade_id=? AND filled_at<? ORDER BY id",
                    (trade_id, upper.isoformat()))]
        for raw in raw_trades:
            if raw.get("exit_filled_at") and _stamp(raw["exit_filled_at"]) >= upper:
                raw = {**raw, "status": "open", "exit_price": None, "exit_filled_at": None,
                       "exit_filled_quantity": None}
            t = _augment_trade(raw, partials.get(raw["trade_id"], []), {})
            observation = classify_trade_observation(t, terminal) or {}
            confirmed = bool(raw.get("option_symbol") and _finite(raw.get("entry_price")) and raw["entry_price"] > 0
                and t.get("original_entry_qty", 0) > 0 and raw.get("entry_order_id") and not str(raw.get("status", "")).startswith("pending"))
            if raw.get("entry_order_id") == "SHADOW_ENTRY":
                confirmed = confirmed and raw["trade_id"] in modeled
            if observation.get("pnl_eligible") is False:
                confirmed = False if observation.get("observation_outcome") != "EXCLUDED" else confirmed
            fields = ("trade_id", "deployment_id", "symbol", "option_symbol", "plan_revision_id", "lane", "entry_timestamp",
                      "exit_filled_at", "exit_policy_id", "entry_price", "exit_price", "original_entry_qty", "is_open", "realized_pnl_usd", "exit_attribution")
            trades.append({**{k: t.get(k) for k in fields}, "fill_confirmed": confirmed,
                           "pnl_eligible": observation.get("pnl_eligible", True)})
    repository = ProspectiveQuoteTapeRepository(edge_db_path, read_only=True, write_timeout_seconds=.25)
    # Reuse the application's pure replay evaluator. Bound to the report as-of.
    with repository._connect() as c:
        cohort_ids = [r[0] for r in c.execute("SELECT cohort_id FROM exit_edge_cohorts WHERE entry_timestamp>=? AND entry_timestamp<?", (lower.isoformat(), upper.isoformat()))]
    cases = analyze_cases([repository.load_case(i, observed_at_end=upper) for i in cohort_ids])["cases"]
    return {"window": {"start": start.isoformat(), "end": end.isoformat(), "timezone": "America/Chicago", "basis": "entry cohort; outcomes observed through window end"},
            "events": events, "configs": list(configs.values()), "trades": trades, "cases": cases,
            "inventory_revision": inventory_revision,
            "source_receipt": {"trading_db": str(Path(db_path).resolve()), "exit_db": str(Path(edge_db_path).resolve()),
                "max_event_id": maximum, "startup_anchor_event_id": anchor,
                "cross_store_snapshot": "sequential read-only; not atomic", "extracted_at": datetime.now(UTC).isoformat()}}


def attach_inferences(report, annotations):
    """Bind human/agent text to exact evidence, not a fragile spreadsheet row."""
    digest = report_digest(report)
    if annotations.get("report_digest") != digest:
        raise ValueError("Inference evidence digest does not match report")
    by_id = {r["row_id"]: r for r in report["rows"]}
    seen = set()
    for a in annotations.get("rows", []):
        if a["row_id"] not in by_id or a["row_id"] in seen:
            raise ValueError("Unknown or duplicate inference row identity")
        seen.add(a["row_id"])
        if a["assessment"] not in {"promising", "collecting", "evidence blocked", "not promising"}:
            raise ValueError("Invalid assessment")
        if not a.get("reason") or not annotations.get("author") or not annotations.get("written_at"):
            raise ValueError("Inference reason and authorship required")
        by_id[a["row_id"]]["inference"] = {"assessment": a["assessment"], "reason": a["reason"],
            "next_step": a.get("next_step") or "None", "author": annotations["author"],
            "written_at": annotations["written_at"], "report_digest": digest}
    return report


def report_digest(report):
    stable = {k: v for k, v in report.items() if k != "generated_at"}
    stable["rows"] = [{k: v for k, v in r.items() if k != "inference"} for r in report["rows"]]
    return hashlib.sha256(json.dumps(stable, sort_keys=True, allow_nan=False).encode()).hexdigest()


def summarize_daily(inputs):
    """Daily entry/signal cohorts; outcomes retain the full report as-of cutoff."""
    days = defaultdict(lambda: {"events": [], "trades": [], "cases": []})
    for event in inputs["events"]:
        payload = event["payload"]
        at = payload.get("timestamp") or event["created_at"]
        day = _stamp(at).astimezone(CT).date().isoformat()
        if inputs["window"]["start"] <= day <= inputs["window"]["end"]:
            days[day]["events"].append(event)
    trade_dates = {}
    for trade in inputs["trades"]:
        day = _stamp(trade["entry_timestamp"]).astimezone(CT).date().isoformat()
        days[day]["trades"].append(trade)
        trade_dates[trade["trade_id"]] = day
    for case in inputs["cases"]:
        day = trade_dates.get(case["trade_id"])
        if day is None:
            day = _stamp(case["entry_timestamp"]).astimezone(CT).date().isoformat()
        days[day]["cases"].append(case)
    result = None
    for day, records in sorted(days.items()):
        daily = summarize({**inputs, **records, "inventory_revision": "daily-observed-only"})
        if result is None:
            result = {**daily, "rows": [], "trades": [], "signals": [], "exits": []}
        for row in daily["rows"]:
            row["date"] = day
            dt = date.fromisoformat(day)
            row["week_start"] = (dt - timedelta(days=dt.weekday())).isoformat()
            row["row_id"] = day + ":" + row["row_id"]
            row["group_id"] = day + ":" + row["group_id"]
        for key in ("trades", "signals", "exits"):
            for detail in daily[key]:
                detail["row_id"] = day + ":" + detail["row_id"]
        for key in ("rows", "trades", "signals", "exits"):
            result[key].extend(daily[key])
    if result is None:
        result = {**summarize(inputs), "rows": [], "signals": [], "trades": [], "exits": []}
    result["schema"] = "bhiksha.strategy-exit-report.daily.v1"
    result["exit_columns"] = sorted({e["policy"] for e in result["exits"]})
    for row in result["rows"]:
        for policy in result["exit_columns"]:
            row["modeled_pnl"].setdefault(policy, None)
    return result
