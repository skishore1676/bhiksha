from __future__ import annotations

import ast
import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mala_bhiksha_kernel import (
    ConditionType,
    EntryCondition,
    ExitProfile,
    ManagementPolicySpec,
    ObservationWindow,
)

from bhiksha.chart_scenarios import (
    CompletedBar,
    OptionQuoteSnapshot,
    evaluate_condition,
    evaluate_exit_profile,
)
from bhiksha.chart_scenarios.policies import CostModel, QuoteEligibilityPolicy
from bhiksha.research_observer.observer import (
    APP_INPUT_SCHEMA,
    _eligible_quote,
    _trigger,
    canonical_hash,
    observe_app_input,
    validate_app_input,
    main,
)


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


def _scenario(symbol: str, *, exit_quote: bool = True) -> dict[str, object]:
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
                "bid": 1.5,
                "ask": 1.6,
                "last": 1.55,
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
    assert first["observation"]["primary_metric"] is not None
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


def _old_quote(raw: dict[str, object], snapshot_id: str) -> OptionQuoteSnapshot:
    return OptionQuoteSnapshot.from_mapping(
        {
            "snapshot_id": snapshot_id,
            "option_symbol": "SPY260807C00100000",
            "underlying_symbol": "SPY",
            "contract_type": "CALL",
            "expiration_date": "2026-08-07",
            "quote_time": raw["quote_time"],
            "source_id": "fixture",
            "bid": raw["bid"],
            "ask": raw["ask"],
            "last": raw["last"],
            "strike": 100.0,
            "delta": 0.4,
            "open_interest": 100,
            "scenario_id": None,
            "is_selected": True,
            "provenance": {},
            "acquired_at": raw["quote_time"],
            "raw_source": {},
            "raw_source_hash": None,
            "snapshot_hash": None,
        }
    )


def _old_quote_policy() -> QuoteEligibilityPolicy:
    return QuoteEligibilityPolicy(
        schema_version="market-context-quote-eligibility.v1",
        require_bid_ask=True,
        allow_last_fallback=False,
        max_spread_pct=0.2,
        max_quote_age_seconds=3600,
        require_positive_mark=True,
    )


def test_replacement_trigger_quote_and_exit_match_old_domain_facts(tmp_path: Path) -> None:
    payload = _input()
    scenario = payload["observation"]["scenarios"]["pool:SPY"]
    raw_bars = scenario["bars"]
    bars = [
        CompletedBar(
            datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00")),
            item["open"],
            item["high"],
            item["low"],
            item["close"],
        )
        for item in raw_bars
    ]
    window = ObservationWindow(
        start_at=scenario["entry_condition"]["start_at"],
        end_at=scenario["entry_condition"]["end_at"],
        market_timezone="America/New_York",
    )
    old_condition = EntryCondition(
        condition_type=ConditionType.CROSS_ABOVE,
        timeframe="39m",
        level=100.0,
        level_ref="fixture#level",
    )
    old_trigger = evaluate_condition(
        old_condition,
        bars,
        window,
        evaluated_at="2026-07-30T14:30:00Z",
    )
    new_triggered, new_trigger_at, _reason = _trigger(raw_bars, scenario["entry_condition"])
    assert old_trigger.triggered is True
    assert new_triggered is True
    assert new_trigger_at == datetime(2026, 7, 30, 14, tzinfo=UTC)

    old_entry = _old_quote(scenario["quotes"][0], "SPY-entry")
    old_exit = _old_quote(scenario["quotes"][1], "SPY-exit")
    old_quote_policy = _old_quote_policy()
    assert old_quote_policy.eligible(old_entry, evaluated_at=old_entry.quote_time)
    assert _eligible_quote(
        scenario["quotes"][0],
        at=old_entry.quote_time,
        policy=scenario["quote_policy"],
    ) is not None
    stale_raw = {**scenario["quotes"][0], "quote_time": "2026-07-30T12:00:00Z"}
    stale_at = datetime(2026, 7, 30, 14, tzinfo=UTC)
    assert old_quote_policy.eligible(
        _old_quote(stale_raw, "SPY-stale"), evaluated_at=stale_at
    ) is False
    assert _eligible_quote(
        stale_raw,
        at=stale_at,
        policy={"max_age_seconds": 3600, "max_spread_pct": 0.2},
    ) is None

    management_policy = ManagementPolicySpec(
        policy_id="fixture-trend-continuation",
        policy_schema_version="exit-policy.v1",
        stop_family="premium_pct",
        stop_anchor="filled_option_premium",
        exit_family="trend_continuation",
        target_model="staged_r",
        target_r=1.0,
        option_stop_fallback_pct=0.25,
        hard_flat_time_et="15:55",
        eod_flat=True,
    )
    old_exit_observation = evaluate_exit_profile(
        ExitProfile.TREND_CONTINUATION,
        old_entry,
        old_exit,
        entry_time=old_entry.quote_time,
        evaluated_at=old_exit.quote_time,
        management_policy=management_policy,
        cost_model=CostModel(
            schema_version="market-context-cost-model.v1",
            contract_multiplier=100,
            contracts=1,
            entry_fee_per_contract_usd=0.0,
            exit_fee_per_contract_usd=0.0,
            entry_slippage_per_contract_usd=0.0,
            exit_slippage_per_contract_usd=0.0,
        ),
        quote_eligibility_policy=old_quote_policy,
    )
    replacement = observe_app_input(payload, events_path=tmp_path / "events.jsonl")
    candidate_result = next(
        item
        for item in replacement["observation"]["candidate_results"]
        if item["candidate_id"] == "pool:SPY"
    )
    assert old_exit_observation.is_terminal
    assert candidate_result["status"] == "closed"
    assert candidate_result["net_r"] == pytest.approx(old_exit_observation.net_r)
