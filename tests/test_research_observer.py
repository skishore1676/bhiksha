from __future__ import annotations

import ast
import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from bhiksha.research_observer.observer import (
    APP_INPUT_SCHEMA,
    canonical_hash,
    observe_app_input,
    validate_app_input,
    main,
)
from bhiksha.research_observer.market_data import HistoricalResearchMarketData


def _candidate(symbol: str, rank: int) -> dict[str, object]:
    body = {
        "candidate_id": f"pool:{symbol}",
        "symbol": symbol,
        "source_rank": rank,
        "interest_score": float(10 - rank),
        "evidence_ids": [f"{symbol}:daily:trend"],
        "facts": {"symbol": symbol, "trend": "bullish"},
    }
    return {**body, "candidate_hash": canonical_hash(body)}


def _scenario(
    symbol: str,
    *,
    exit_quote: bool = True,
    exit_bid: float = 1.5,
    exit_ask: float = 1.6,
) -> dict[str, object]:
    quotes: list[dict[str, object]] = [
        {
            "quote_id": f"{symbol}-entry",
            "quote_time": "2026-07-30T14:00:00Z",
            "bid": 1.0,
            "ask": 1.1,
            "last": 1.05,
        }
    ]
    if exit_quote:
        quotes.append(
            {
                "quote_id": f"{symbol}-exit",
                "quote_time": "2026-07-30T14:30:00Z",
                "bid": exit_bid,
                "ask": exit_ask,
                "last": (exit_bid + exit_ask) / 2,
            }
        )
    return {
        "bars": [
            {
                "timestamp": "2026-07-30T13:30:00Z",
                "open": 99.0,
                "high": 100.0,
                "low": 98.5,
                "close": 99.0,
                "completed": True,
            },
            {
                "timestamp": "2026-07-30T14:00:00Z",
                "open": 99.0,
                "high": 101.5,
                "low": 98.8,
                "close": 101.0,
                "completed": True,
            },
        ],
        "entry_condition": {
            "type": "cross_above",
            "level": 100.0,
            "start_at": "2026-07-30T13:30:00Z",
            "end_at": "2026-07-30T15:00:00Z",
        },
        "quotes": quotes,
        "quote_policy": {"max_age_seconds": 3600, "max_spread_pct": 0.2},
        "exit": {
            "type": "take_profit_or_stop",
            "risk_pct": 0.25,
            "target_r": 1.0,
            "stop_r": -1.0,
            "cost_r": 0.0,
        },
    }


def _input() -> dict[str, object]:
    frozen = {"pool:SPY": _candidate("SPY", 1), "pool:QQQ": _candidate("QQQ", 2)}
    body = {
        "schema": APP_INPUT_SCHEMA,
        "experiment_id": "chart-ranker-v1",
        "experiment_version": 1,
        "experiment_spec_hash": "sha256:spec",
        "run_id": "run-1",
        "mode": "shadow",
        "source": {
            "pool_schema": "market_cartographer.research_scenario_pool.v1",
            "pool_hash": "sha256:pool",
            "pool_run_id": "pool-run",
            "pool_as_of": "2026-07-30T20:00:00+00:00",
        },
        "frozen_candidates": frozen,
        "arms": {
            "control": {"selector": "deterministic", "candidate_ids": ["pool:SPY", "pool:QQQ"]},
            "treatment": {
                "selector": "fixture",
                "candidate_ids": ["pool:QQQ", "pool:SPY"],
                "realized_agent": {
                    "route": "fixture",
                    "provider": "local",
                    "model": "fixture",
                    "degraded": False,
                },
            },
        },
        "observation": {
            "adapter": "bhiksha_research_observer_v1",
            "window": "session",
            "metric": "net_r",
            "scenarios": {
                "pool:SPY": _scenario("SPY"),
                "pool:QQQ": _scenario("QQQ", exit_quote=False),
            },
        },
        "limitations": ["fixture"],
    }
    return {**body, "input_hash": canonical_hash(body)}


def test_observer_emits_closed_and_missing_data_without_broker_effects(tmp_path: Path) -> None:
    first = observe_app_input(
        _input(),
        events_path=tmp_path / "events.jsonl",
        output_path=tmp_path / "run.json",
    )

    assert first["schema"] == "research.run.v1"
    assert first["observation"]["triggered"] == 2
    assert first["observation"]["closed"] == 1
    assert first["observation"]["primary_metric"] is None
    assert first["observation"]["comparison"]["status"] == "partial"
    assert first["status"] == "partial"
    assert first["effects"] == {
        "broker": 0,
        "orders": 0,
        "auth": 0,
        "schedule": 0,
        "external_send": 0,
    }
    assert any("exit_quote_missing" in item for item in first["limitations"])
    assert len((tmp_path / "events.jsonl").read_text().splitlines()) == 2


def test_event_append_is_idempotent_on_restart(tmp_path: Path) -> None:
    first = observe_app_input(_input(), events_path=tmp_path / "events.jsonl")
    second = observe_app_input(_input(), events_path=tmp_path / "events.jsonl")

    assert first["content_hash"] == second["content_hash"]
    assert second["event_count"] == 2
    assert len((tmp_path / "events.jsonl").read_text().splitlines()) == 2


def test_changed_content_under_same_run_id_fails_closed(tmp_path: Path) -> None:
    payload = _input()
    events = tmp_path / "events.jsonl"
    observe_app_input(payload, events_path=events)

    changed = copy.deepcopy(payload)
    candidate = dict(changed["frozen_candidates"]["pool:SPY"])
    candidate_body = {key: value for key, value in candidate.items() if key != "candidate_hash"}
    candidate_body["facts"] = {"symbol": "SPY", "trend": "invented"}
    candidate["facts"] = candidate_body["facts"]
    candidate["candidate_hash"] = canonical_hash(candidate_body)
    changed["frozen_candidates"]["pool:SPY"] = candidate
    changed["input_hash"] = canonical_hash(
        {key: value for key, value in changed.items() if key != "input_hash"}
    )
    with pytest.raises(ValueError, match="event idempotency conflict"):
        observe_app_input(changed, events_path=events)


def test_missing_input_is_a_healthy_no_data_receipt(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output = tmp_path / "run-record.json"

    assert main(
        [
            "--input",
            str(tmp_path / "not-ready.json"),
            "--events",
            str(tmp_path / "events.jsonl"),
            "--output",
            str(output),
        ]
    ) == 0

    receipt = json.loads(capsys.readouterr().out)
    record = json.loads(output.read_text())
    assert receipt["status"] == "no_data"
    assert receipt["run_id"] is None
    assert record["reason"] == "app_input_missing"
    assert record["effects"] == {
        "broker": 0,
        "orders": 0,
        "auth": 0,
        "schedule": 0,
        "external_send": 0,
    }
    assert not (tmp_path / "events.jsonl").exists()


def test_invented_candidate_and_non_shadow_input_are_rejected() -> None:
    unknown = copy.deepcopy(_input())
    unknown["arms"]["treatment"]["candidate_ids"].append("invented")
    unknown["input_hash"] = canonical_hash(
        {key: value for key, value in unknown.items() if key != "input_hash"}
    )
    with pytest.raises(ValueError, match="invented"):
        validate_app_input(unknown)

    live = copy.deepcopy(_input())
    live["mode"] = "live"
    live["input_hash"] = canonical_hash(
        {key: value for key, value in live.items() if key != "input_hash"}
    )
    with pytest.raises(ValueError, match="mode=shadow"):
        validate_app_input(live)


def test_observer_source_has_no_runtime_or_producer_imports() -> None:
    path = Path(__file__).parents[1] / "src/bhiksha/research_observer/observer.py"
    tree = ast.parse(path.read_text())
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert not any(
        name.startswith(("bhiksha.execution", "bhiksha.app", "market_cartographer", "tradelab"))
        for name in imported
    )


def test_malformed_typed_condition_is_rejected() -> None:
    payload = _input()
    payload["observation"]["scenarios"]["pool:SPY"]["entry_condition"]["type"] = "natural_language"
    payload["input_hash"] = canonical_hash(
        {key: value for key, value in payload.items() if key != "input_hash"}
    )
    with pytest.raises(ValueError, match="typed primitive"):
        validate_app_input(payload)


def _discriminating_input() -> dict[str, object]:
    frozen = {
        "pool:IWM": _candidate("IWM", 1),
        "pool:SPY": _candidate("SPY", 2),
        "pool:QQQ": _candidate("QQQ", 3),
    }
    body = {
        "schema": APP_INPUT_SCHEMA,
        "experiment_id": "chart-ranker-v1",
        "experiment_version": 1,
        "experiment_spec_hash": "sha256:spec",
        "run_id": "run-discriminating",
        "mode": "shadow",
        "source": {
            "pool_schema": "market_cartographer.research_scenario_pool.v1",
            "pool_hash": "sha256:pool",
            "pool_run_id": "pool-run",
            "pool_as_of": "2026-07-30T20:00:00+00:00",
        },
        "frozen_candidates": frozen,
        "arms": {
            "control": {
                "selector": "deterministic_score_v1",
                "candidate_ids": ["pool:IWM", "pool:SPY"],
                "rank_by_candidate": {"pool:IWM": 1, "pool:SPY": 2},
            },
            "treatment": {
                "selector": "bounded_fixture_ranker_v1",
                "candidate_ids": ["pool:IWM", "pool:QQQ"],
                "rank_by_candidate": {"pool:IWM": 1, "pool:QQQ": 2},
                "realized_agent": {
                    "route": "local_fixture",
                    "provider": "local",
                    "model": "bounded-fixture-ranker",
                    "degraded": False,
                },
            },
        },
        "observation": {
            "adapter": "bhiksha_research_observer_v1",
            "data_mode": "fixture",
            "window": "session",
            "metric": "net_r",
            "scenarios": {
                "pool:IWM": _scenario("IWM", exit_bid=1.4, exit_ask=1.5),
                "pool:SPY": _scenario("SPY"),
                "pool:QQQ": _scenario("QQQ", exit_bid=0.7, exit_ask=0.8),
            },
        },
        "limitations": [],
    }
    return {**body, "input_hash": canonical_hash(body)}


def test_arm_metrics_and_treatment_collapse_mutation_are_discriminating(tmp_path: Path) -> None:
    payload = _discriminating_input()
    run = observe_app_input(
        payload,
        events_path=tmp_path / "actual-events.jsonl",
    )

    assert run["status"] == "succeeded"
    assert run["observation"]["comparison"]["status"] == "interpretable"
    assert run["observation"]["comparison"]["difference"] != 0
    assert run["observation"]["arm_metrics"]["control"]["status"] == "complete"
    assert run["observation"]["arm_metrics"]["treatment"]["status"] == "complete"

    collapsed = copy.deepcopy(payload)
    collapsed["arms"]["treatment"]["candidate_ids"] = list(
        collapsed["arms"]["control"]["candidate_ids"]
    )
    collapsed["arms"]["treatment"]["rank_by_candidate"] = {
        candidate_id: rank
        for rank, candidate_id in enumerate(collapsed["arms"]["treatment"]["candidate_ids"], start=1)
    }
    collapsed["input_hash"] = canonical_hash(
        {key: value for key, value in collapsed.items() if key != "input_hash"}
    )
    collapsed_run = observe_app_input(
        collapsed,
        events_path=tmp_path / "collapsed-events.jsonl",
    )
    assert collapsed_run["observation"]["comparison"]["difference"] == 0


def test_candidate_limitation_without_fixture_disclaimer_is_not_success(tmp_path: Path) -> None:
    payload = _input()
    payload["limitations"] = []
    payload["input_hash"] = canonical_hash(
        {key: value for key, value in payload.items() if key != "input_hash"}
    )
    run = observe_app_input(payload, events_path=tmp_path / "events.jsonl")
    assert run["status"] == "partial"


def test_prospective_input_uses_separate_historical_adapter(tmp_path: Path) -> None:
    payload = _discriminating_input()
    historical: dict[str, object] = {
        "schema": "research.historical_market_data.v1",
        "mode": "historical_replay",
        "candidates": {},
    }
    for candidate_id, scenario in payload["observation"]["scenarios"].items():
        historical["candidates"][candidate_id] = {
            "bars": scenario["bars"],
            "quotes": scenario["quotes"],
        }
        del scenario["bars"]
        del scenario["quotes"]
    payload["observation"]["data_mode"] = "prospective"
    payload["input_hash"] = canonical_hash(
        {key: value for key, value in payload.items() if key != "input_hash"}
    )

    validate_app_input(payload)
    replay = observe_app_input(
        payload,
        events_path=tmp_path / "historical-events.jsonl",
        market_data=HistoricalResearchMarketData.from_mapping(historical),
    )
    assert replay["status"] == "succeeded"
    assert replay["inputs"]["data_mode"] == "prospective"
    assert replay["observation"]["comparison"]["status"] == "interpretable"

    invalid = copy.deepcopy(payload)
    invalid["observation"]["scenarios"]["pool:IWM"]["bars"] = []
    invalid["input_hash"] = canonical_hash(
        {key: value for key, value in invalid.items() if key != "input_hash"}
    )
    with pytest.raises(ValueError, match="future observation facts"):
        validate_app_input(invalid)
