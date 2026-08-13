from __future__ import annotations

import copy
import inspect
import plistlib
from pathlib import Path

import pytest

from bhiksha.experiments import cartographer_shadow as subject


def _batch() -> dict[str, object]:
    body = {
        "schema": "market_cartographer.daily_recommendation_batch.v1",
        "mode": "shadow_research",
        "campaign": {"campaign_id": "alpha", "version": 1},
        "run_id": "run-1",
        "as_of": "2026-08-11T20:00:00+00:00",
        "source": {"pool_hash": "sha256:pool"},
        "recommendations": [
            {
                "recommendation_id": "alpha:control:SPY",
                "arm": "control",
                "symbol": "SPY",
                "direction": "long",
                "evaluation_horizons_sessions": [1, 5],
            },
            {
                "recommendation_id": "alpha:challenger:QQQ",
                "arm": "challenger",
                "symbol": "QQQ",
                "direction": "abstain",
                "evaluation_horizons_sessions": [1, 5],
            },
        ],
        "effects": subject.zero_effects(),
    }
    return {**body, "batch_hash": subject.canonical_hash(body)}


def _facts(batch: dict[str, object]) -> dict[str, object]:
    body = {
        "schema": "bhiksha.cartographer_market_facts.v1",
        "batch_hash": batch["batch_hash"],
        "source_as_of": batch["as_of"],
        "points": [
            {
                "symbol": "SPY",
                "horizon_sessions": 1,
                "entry_price": 100.0,
                "entry_at": "2026-08-12T13:30:00+00:00",
                "exit_price": 102.0,
                "exit_at": "2026-08-12T20:00:00+00:00",
                "source": {"provider": "fixture"},
            }
        ],
        "effects": subject.zero_effects(),
    }
    return {**body, "facts_hash": subject.canonical_hash(body)}


def test_observer_closes_available_horizon_and_keeps_missing_pending() -> None:
    batch = _batch()
    receipt = subject.build_observation(batch, _facts(batch), observed_at="2026-08-13T00:00:00Z")
    by_id = {
        (row["recommendation_id"], row["horizon_sessions"]): row
        for row in receipt["observations"]
    }
    assert by_id[("alpha:control:SPY", 1)]["directional_return"] == 0.02
    assert by_id[("alpha:control:SPY", 5)]["status"] == "pending_market_data"
    assert by_id[("alpha:challenger:QQQ", 1)]["status"] == "abstained"
    assert receipt["effects"] == subject.zero_effects()


def test_tampered_batch_fails_closed() -> None:
    batch = copy.deepcopy(_batch())
    batch["recommendations"][0]["direction"] = "short"
    with pytest.raises(ValueError, match="hash mismatch"):
        subject.build_observation(batch, _facts(_batch()), observed_at="2026-08-13T00:00:00Z")


def test_observer_module_has_no_money_path_imports() -> None:
    source = inspect.getsource(subject)
    for forbidden in (
        "bhiksha.active_plan",
        "bhiksha.execution",
        "bhiksha.integrations",
        "bhiksha.options",
    ):
        assert forbidden not in source


def test_observer_runs_after_cartographer_retry_window() -> None:
    payload = plistlib.loads(
        Path("scripts/launchd/com.bhiksha.cartographer-shadow.plist.template").read_bytes()
    )
    times = {
        (item["Hour"], item["Minute"]) for item in payload["StartCalendarInterval"]
    }

    assert times == {(7, 30)}
    assert len(payload["StartCalendarInterval"]) == 5
