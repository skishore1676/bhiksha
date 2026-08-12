"""Small read-only observer for the replacement research path.

The observer consumes one app-owned JSON input.  It evaluates typed conditions
against completed bars, accepts only eligible read-only quote facts, records
synthetic mark observations, and appends factual events.  It has no dependency
on the research producer, the shared experiment contracts, or the live runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

APP_INPUT_SCHEMA = "research.app_input.v1"
RUN_RECORD_SCHEMA = "research.run.v1"
ZERO_EFFECTS = {
    "broker": 0,
    "orders": 0,
    "auth": 0,
    "schedule": 0,
    "external_send": 0,
}


def canonical_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamps must be non-empty RFC 3339 strings")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _number(value: Any, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError(f"{field} must be finite" + (" and positive" if positive else ""))
    return number


def _bar(raw: Mapping[str, Any]) -> dict[str, Any]:
    required = {"timestamp", "open", "high", "low", "close", "completed"}
    if set(raw) - required - {"volume", "bar_id"}:
        raise ValueError("bar contains an unsupported field")
    if not required <= set(raw):
        raise ValueError("bar is missing a required field")
    if raw["completed"] is not True:
        raise ValueError("only completed bars may be observed")
    bar = {
        "timestamp": _timestamp(_utc(raw["timestamp"])),
        "open": _number(raw["open"], "open"),
        "high": _number(raw["high"], "high"),
        "low": _number(raw["low"], "low"),
        "close": _number(raw["close"], "close"),
        "completed": True,
    }
    if bar["high"] < max(bar["open"], bar["close"]):
        raise ValueError("bar high does not contain open and close")
    if bar["low"] > min(bar["open"], bar["close"]):
        raise ValueError("bar low does not contain open and close")
    if "volume" in raw and raw["volume"] is not None:
        bar["volume"] = _number(raw["volume"], "volume")
    if raw.get("bar_id") is not None:
        bar["bar_id"] = str(raw["bar_id"])
    return bar


def _quote(raw: Mapping[str, Any]) -> dict[str, Any]:
    required = {"quote_id", "quote_time", "bid", "ask", "last"}
    if set(raw) != required:
        raise ValueError(
            "quote must declare exactly quote_id, quote_time, bid, ask, and last"
        )
    bid = None if raw["bid"] is None else _number(raw["bid"], "bid")
    ask = None if raw["ask"] is None else _number(raw["ask"], "ask")
    last = None if raw["last"] is None else _number(raw["last"], "last")
    if bid is not None and bid < 0:
        raise ValueError("bid cannot be negative")
    if ask is not None and ask < 0:
        raise ValueError("ask cannot be negative")
    if bid is not None and ask is not None and ask < bid:
        raise ValueError("ask cannot be below bid")
    return {
        "quote_id": str(raw["quote_id"]),
        "quote_time": _timestamp(_utc(raw["quote_time"])),
        "bid": bid,
        "ask": ask,
        "last": last,
    }


def _validate_condition(condition: Mapping[str, Any]) -> None:
    allowed = {
        "type",
        "level",
        "bars",
        "range_low",
        "range_high",
        "buffer",
        "start_at",
        "end_at",
    }
    if set(condition) - allowed:
        raise ValueError("condition contains an unsupported field")
    kind = condition.get("type")
    if kind not in {
        "cross_above",
        "cross_below",
        "hold_above",
        "hold_below",
        "range_breakout",
    }:
        raise ValueError("condition type is not a supported typed primitive")
    if kind in {"cross_above", "cross_below", "hold_above", "hold_below"}:
        _number(condition.get("level"), "condition.level")
    if kind in {"hold_above", "hold_below"}:
        if not isinstance(condition.get("bars"), int) or condition["bars"] < 1:
            raise ValueError("hold condition bars must be a positive integer")
    if kind == "range_breakout":
        _number(condition.get("range_low"), "condition.range_low")
        _number(condition.get("range_high"), "condition.range_high")
        _number(condition.get("buffer"), "condition.buffer")
        if condition["range_low"] > condition["range_high"]:
            raise ValueError("range_low must not exceed range_high")
    if "start_at" not in condition or "end_at" not in condition:
        raise ValueError("typed condition requires start_at and end_at")
    if _utc(condition["end_at"]) < _utc(condition["start_at"]):
        raise ValueError("condition end_at precedes start_at")


def validate_app_input(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the frozen app input and every candidate containment boundary."""

    payload = dict(raw)
    if payload.get("schema") != APP_INPUT_SCHEMA:
        raise ValueError("invalid research app input schema")
    if payload.get("mode") != "shadow":
        raise ValueError("research observer accepts only mode=shadow")
    if any(key in payload for key in ("campaign", "campaign_manifest", "runtime_attestation")):
        raise ValueError("research app input cannot carry campaign or runtime manifests")
    candidates = payload.get("frozen_candidates")
    if not isinstance(candidates, Mapping) or not candidates:
        raise ValueError("frozen_candidates must be a non-empty object")
    for candidate_id, raw_candidate in candidates.items():
        if not isinstance(candidate_id, str) or not isinstance(raw_candidate, Mapping):
            raise ValueError("frozen candidate entries must be named objects")
        candidate = dict(raw_candidate)
        supplied_hash = candidate.pop("candidate_hash", None)
        if supplied_hash != canonical_hash(candidate):
            raise ValueError(f"frozen candidate hash mismatch: {candidate_id}")
        if candidate.get("candidate_id") != candidate_id:
            raise ValueError(f"frozen candidate key mismatch: {candidate_id}")

    arms = payload.get("arms")
    if not isinstance(arms, Mapping) or set(arms) != {"control", "treatment"}:
        raise ValueError("app input must contain control and treatment arms")
    candidate_ids = set(candidates)
    for arm_name, raw_arm in arms.items():
        if not isinstance(raw_arm, Mapping):
            raise ValueError(f"{arm_name} arm must be an object")
        selected = raw_arm.get("candidate_ids")
        if not isinstance(selected, list) or not selected:
            raise ValueError(f"{arm_name} arm must select candidates")
        if not set(selected) <= candidate_ids:
            raise ValueError(f"{arm_name} arm invented a candidate")
        if len(selected) != len(set(selected)):
            raise ValueError(f"{arm_name} arm contains duplicate candidates")
    treatment_agent = arms["treatment"].get("realized_agent")
    if not isinstance(treatment_agent, Mapping):
        raise ValueError("treatment arm must include realized agent metadata")
    for field in ("route", "provider", "model"):
        if not isinstance(treatment_agent.get(field), str) or not treatment_agent[field]:
            raise ValueError(f"realized agent {field} must be non-empty")

    observation = payload.get("observation")
    if not isinstance(observation, Mapping):
        raise ValueError("app input observation must be an object")
    scenarios = observation.get("scenarios")
    if not isinstance(scenarios, Mapping) or set(scenarios) != candidate_ids:
        raise ValueError("app input scenarios must exactly cover frozen candidates")
    for candidate_id, raw_scenario in scenarios.items():
        if not isinstance(raw_scenario, Mapping):
            raise ValueError(f"scenario must be an object: {candidate_id}")
        bars = raw_scenario.get("bars")
        quotes = raw_scenario.get("quotes")
        condition = raw_scenario.get("entry_condition")
        exit_config = raw_scenario.get("exit")
        if not isinstance(bars, list) or not bars:
            raise ValueError(f"scenario bars must be non-empty: {candidate_id}")
        if not isinstance(quotes, list):
            raise ValueError(f"scenario quotes must be a list: {candidate_id}")
        if not isinstance(condition, Mapping):
            raise ValueError(f"scenario entry_condition must be an object: {candidate_id}")
        _validate_condition(condition)
        if not isinstance(exit_config, Mapping) or exit_config.get("type") != "take_profit_or_stop":
            raise ValueError(f"unsupported exit behavior: {candidate_id}")
        _number(exit_config.get("risk_pct"), "exit.risk_pct", positive=True)
        _number(exit_config.get("target_r"), "exit.target_r")
        _number(exit_config.get("stop_r"), "exit.stop_r")
        _number(exit_config.get("cost_r", 0.0), "exit.cost_r")
        normalized_bars = [_bar(item) for item in bars if isinstance(item, Mapping)]
        if len(normalized_bars) != len(bars):
            raise ValueError(f"scenario bars must contain objects: {candidate_id}")
        if normalized_bars != sorted(normalized_bars, key=lambda item: item["timestamp"]):
            raise ValueError(f"scenario bars must be ordered: {candidate_id}")
        for item in quotes:
            if not isinstance(item, Mapping):
                raise ValueError(f"scenario quotes must contain objects: {candidate_id}")
            _quote(item)
        policy = raw_scenario.get("quote_policy")
        if not isinstance(policy, Mapping):
            raise ValueError(f"scenario quote_policy must be an object: {candidate_id}")
        if int(policy.get("max_age_seconds", -1)) < 0:
            raise ValueError("quote max_age_seconds must be non-negative")
        _number(policy.get("max_spread_pct"), "quote max_spread_pct", positive=True)

    input_hash = payload.get("input_hash")
    body = {key: value for key, value in payload.items() if key != "input_hash"}
    if input_hash != canonical_hash(body):
        raise ValueError("research app input hash mismatch")
    return payload


def _trigger(bars: Sequence[Mapping[str, Any]], condition: Mapping[str, Any]) -> tuple[bool, datetime | None, str]:
    _validate_condition(condition)
    start = _utc(condition["start_at"])
    end = _utc(condition["end_at"])
    usable = [item for item in bars if start <= _utc(item["timestamp"]) <= end]
    kind = condition["type"]
    if kind in {"cross_above", "cross_below"}:
        level = float(condition["level"])
        for previous, current in zip(usable, usable[1:]):
            previous_close = float(previous["close"])
            current_close = float(current["close"])
            crossed = (
                previous_close <= level < current_close
                if kind == "cross_above"
                else previous_close >= level > current_close
            )
            if crossed:
                return True, _utc(current["timestamp"]), "typed_condition_satisfied"
        return False, None, "no_typed_cross_in_window"
    if kind in {"hold_above", "hold_below"}:
        count = int(condition["bars"])
        if len(usable) < count:
            return False, None, "insufficient_completed_bars"
        level = float(condition["level"])
        tail = usable[-count:]
        hit = all(
            (float(item["close"]) >= level if kind == "hold_above" else float(item["close"]) <= level)
            for item in tail
        )
        return (hit, _utc(tail[-1]["timestamp"]) if hit else None, "typed_condition_satisfied" if hit else "hold_side_not_maintained")
    upper = float(condition["range_high"]) + float(condition["buffer"])
    lower = float(condition["range_low"]) - float(condition["buffer"])
    for item in usable:
        close = float(item["close"])
        if close > upper or close < lower:
            return True, _utc(item["timestamp"]), "typed_condition_satisfied"
    return False, None, "range_bounds_not_broken"


def _eligible_quote(
    raw: Mapping[str, Any],
    *,
    at: datetime,
    policy: Mapping[str, Any],
) -> dict[str, Any] | None:
    quote = _quote(raw)
    quote_at = _utc(quote["quote_time"])
    age = (at - quote_at).total_seconds()
    if age < 0 or age > int(policy["max_age_seconds"]):
        return None
    bid = quote["bid"]
    ask = quote["ask"]
    if bid is not None and ask is not None:
        mark = (bid + ask) / 2.0
        if mark <= 0 or (ask - bid) / mark > float(policy["max_spread_pct"]):
            return None
    else:
        mark = quote["last"]
    if mark is None or mark <= 0:
        return None
    return {**quote, "mark": mark}


def _latest_quote(
    quotes: Sequence[Mapping[str, Any]],
    *,
    at: datetime,
    policy: Mapping[str, Any],
) -> dict[str, Any] | None:
    eligible = [
        item
        for raw in quotes
        if (item := _eligible_quote(raw, at=at, policy=policy)) is not None
        and _utc(item["quote_time"]) <= at
    ]
    return max(eligible, key=lambda item: (_utc(item["quote_time"]), item["quote_id"])) if eligible else None


def _observe_candidate(
    run_id: str,
    candidate_id: str,
    candidate: Mapping[str, Any],
    scenario: Mapping[str, Any],
    input_hash: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    bars = [_bar(item) for item in scenario["bars"]]
    quotes = [_quote(item) for item in scenario["quotes"]]
    triggered, trigger_at, trigger_reason = _trigger(bars, scenario["entry_condition"])
    base = {
        "event_id": f"{run_id}:{candidate_id}",
        "run_id": run_id,
        "input_hash": input_hash,
        "candidate_id": candidate_id,
        "symbol": candidate["symbol"],
        "triggered": triggered,
        "trigger_at": _timestamp(trigger_at) if trigger_at else None,
        "trigger_reason": trigger_reason,
        "mark_type": "counterfactual_mark_not_fill",
    }
    if not triggered or trigger_at is None:
        result = {
            "candidate_id": candidate_id,
            "symbol": candidate["symbol"],
            "status": "not_triggered",
            "triggered": False,
            "closed": False,
            "gross_r": None,
            "net_r": None,
            "reason": trigger_reason,
        }
        return result, {**base, "status": result["status"], "limitation": None}

    policy = scenario["quote_policy"]
    entry = _latest_quote(quotes, at=trigger_at, policy=policy)
    if entry is None:
        result = {
            "candidate_id": candidate_id,
            "symbol": candidate["symbol"],
            "status": "missing_data",
            "triggered": True,
            "closed": False,
            "gross_r": None,
            "net_r": None,
            "reason": "entry_quote_missing_or_ineligible",
        }
        return result, {**base, "status": result["status"], "limitation": result["reason"]}

    # The replacement keeps the old observer's scientific behavior: the
    # synthetic calculation is based on the immutable quote mark, never a
    # broker fill or an order-side price.
    entry_price = entry["mark"]
    risk_pct = float(scenario["exit"]["risk_pct"])
    exit_config = scenario["exit"]
    exit_quote: dict[str, Any] | None = None
    exit_r: float | None = None
    exit_reason = "no_terminal_exit_quote"
    for raw_quote in sorted(quotes, key=lambda item: (_utc(item["quote_time"]), item["quote_id"])):
        quote_at = _utc(raw_quote["quote_time"])
        if quote_at <= trigger_at:
            continue
        eligible = _eligible_quote(raw_quote, at=quote_at, policy=policy)
        if eligible is None:
            continue
        exit_price = eligible["mark"]
        current_r = (exit_price - entry_price) / (entry_price * risk_pct)
        if current_r >= float(exit_config["target_r"]):
            exit_quote, exit_r, exit_reason = eligible, current_r, "target_r"
            break
        if current_r <= float(exit_config["stop_r"]):
            exit_quote, exit_r, exit_reason = eligible, current_r, "stop_r"
            break
    if exit_quote is None or exit_r is None:
        result = {
            "candidate_id": candidate_id,
            "symbol": candidate["symbol"],
            "status": "open",
            "triggered": True,
            "closed": False,
            "entry_mark": entry["mark"],
            "gross_r": None,
            "net_r": None,
            "reason": "exit_quote_missing_or_terminal_rule_not_hit",
        }
        return result, {
            **base,
            "status": result["status"],
            "entry_quote_id": entry["quote_id"],
            "limitation": result["reason"],
        }
    net_r = exit_r - float(exit_config.get("cost_r", 0.0))
    result = {
        "candidate_id": candidate_id,
        "symbol": candidate["symbol"],
        "status": "closed",
        "triggered": True,
        "closed": True,
        "entry_mark": entry["mark"],
        "exit_mark": exit_quote["mark"],
        "gross_r": exit_r,
        "net_r": net_r,
        "reason": exit_reason,
    }
    return result, {
        **base,
        "status": result["status"],
        "entry_quote_id": entry["quote_id"],
        "exit_quote_id": exit_quote["quote_id"],
        "gross_r": exit_r,
        "net_r": net_r,
        "limitation": None,
    }


def _append_events(path: Path, events: Sequence[Mapping[str, Any]]) -> int:
    existing: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            prior = json.loads(line)
            existing[str(prior["event_id"])] = canonical_hash(
                {key: value for key, value in prior.items() if key != "event_hash"}
            )
    pending: list[str] = []
    for event in events:
        body = dict(event)
        event_id = str(body["event_id"])
        expected = canonical_hash(body)
        if event_id in existing:
            if existing[event_id] != expected:
                raise ValueError(f"event idempotency conflict: {event_id}")
            continue
        body["event_hash"] = expected
        pending.append(json.dumps(body, ensure_ascii=False, sort_keys=True))
    if pending:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(pending) + "\n")
    return len(pending)


def observe_app_input(
    raw: Mapping[str, Any],
    *,
    events_path: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    payload = validate_app_input(raw)
    frozen = payload["frozen_candidates"]
    scenarios = payload["observation"]["scenarios"]
    candidate_ids = list(frozen)
    results: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    for candidate_id in candidate_ids:
        result, event = _observe_candidate(
            str(payload["run_id"]),
            candidate_id,
            frozen[candidate_id],
            scenarios[candidate_id],
            str(payload["input_hash"]),
        )
        results[candidate_id] = result
        events.append(event)
    _append_events(Path(events_path).expanduser().resolve(), events)
    closed = [item for item in results.values() if item["closed"]]
    triggered = [item for item in results.values() if item["triggered"]]
    metric_values = [float(item["net_r"]) for item in closed]
    limitations = list(payload.get("limitations", []))
    limitations.extend(
        f"{item['candidate_id']}:{item['reason']}"
        for item in results.values()
        if item["reason"] in {"entry_quote_missing_or_ineligible", "exit_quote_missing_or_terminal_rule_not_hit"}
    )
    body: dict[str, Any] = {
        "schema": RUN_RECORD_SCHEMA,
        "experiment_id": payload["experiment_id"],
        "experiment_version": payload["experiment_version"],
        "run_id": payload["run_id"],
        "mode": "shadow",
        "input_hash": payload["input_hash"],
        "inputs": {
            "candidate_pool_hash": payload["source"]["pool_hash"],
            "frozen_candidate_ids": candidate_ids,
            "arm_candidate_ids": {
                name: list(arm["candidate_ids"]) for name, arm in payload["arms"].items()
            },
        },
        "outputs": {
            "control": list(payload["arms"]["control"]["candidate_ids"]),
            "treatment": list(payload["arms"]["treatment"]["candidate_ids"]),
        },
        "realized_agent": dict(payload["arms"]["treatment"]["realized_agent"]),
        "observation": {
            "triggered": len(triggered),
            "closed": len(closed),
            "primary_metric": sum(metric_values) / len(metric_values) if metric_values else None,
            "candidate_results": list(results.values()),
        },
        "effects": {
            "broker": 0,
            "orders": 0,
            "auth": 0,
            "schedule": 0,
            "external_send": 0,
        },
        "status": "succeeded" if not limitations[1:] else "partial",
        "limitations": limitations,
        "event_count": len(events),
    }
    body["content_hash"] = canonical_hash(body)
    if output_path is not None:
        destination = Path(output_path).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    return body


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("input must contain one JSON object")
    return payload


def _no_data_run(output_path: Path, *, reason: str) -> dict[str, Any]:
    """Write a healthy, explicit no-data receipt for a scheduled dry run.

    A disabled-first observer must be safe to leave installed before the
    producer has prepared the next app input.  Missing input is therefore a
    meaningful broker-inert result, not a fabricated empty experiment run.
    Malformed or unreadable input still raises and fails closed.
    """

    body: dict[str, Any] = {
        "schema": RUN_RECORD_SCHEMA,
        "mode": "shadow",
        "status": "no_data",
        "reason": reason,
        "input_hash": None,
        "observation": None,
        "effects": dict(ZERO_EFFECTS),
        "limitations": [reason],
        "event_count": 0,
    }
    body["content_hash"] = canonical_hash(body)
    destination = output_path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return body


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m bhiksha.research_observer")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run = observe_app_input(
            _read_json(args.input),
            events_path=args.events,
            output_path=args.output,
        )
    except FileNotFoundError:
        run = _no_data_run(args.output, reason="app_input_missing")
    receipt = {
        "schema": "research.observer_receipt.v1",
        "status": run["status"],
        "run_id": run.get("run_id"),
        "input_hash": run["input_hash"],
        "run_record_hash": run["content_hash"],
        "effects": run["effects"],
        "event_count": run["event_count"],
    }
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
