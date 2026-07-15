import pytest

from bhiksha.execution.order_manager import PublicQuote
from bhiksha.execution.pricing import (
    ENTRY_EXECUTION_PROFILES,
    EntryPricingPolicy,
    build_entry_profile_comparison,
    resolve_entry_reprice_max_chase_pct,
    resolve_initial_spread_fraction,
    scale_spread_fraction,
    select_entry_limit,
)


def test_urgent_entry_pricing_improves_inside_wider_spread() -> None:
    result = select_entry_limit(
        PublicQuote(symbol="QQQ260330P00558000", bid=2.70, ask=2.90, last=2.80, open_interest=550),
        {"max_bid_ask_spread_pct": 0.20, "min_open_interest": 100},
    )

    assert result.approved is True
    assert result.limit_price == 2.85
    assert result.evidence()["mid"] == 2.80
    assert result.evidence()["spread_abs"] == 0.20
    assert result.evidence()["pricing_mode"] == "urgent"


def test_urgent_entry_pricing_crosses_very_tight_spread() -> None:
    result = select_entry_limit(
        PublicQuote(symbol="IWM260330P00558000", bid=1.03, ask=1.06, last=1.05, open_interest=437),
        {"max_bid_ask_spread_pct": 0.20, "min_open_interest": 100},
    )

    assert result.approved is True
    assert result.limit_price == 1.06


def test_entry_pricing_requires_two_sided_quote_and_open_interest() -> None:
    result = select_entry_limit(
        PublicQuote(symbol="IWM260330P00558000", bid=None, ask=1.06, last=1.05, open_interest=None),
        {},
    )

    assert result.approved is False
    assert result.block_reasons == ["public_quote_missing_bid_ask", "public_open_interest_missing"]


def test_balanced_entry_pricing_uses_mid() -> None:
    result = select_entry_limit(
        PublicQuote(symbol="QQQ260330P00558000", bid=2.70, ask=2.90, last=2.80, open_interest=550),
        policy=EntryPricingPolicy(mode="balanced"),
    )

    assert result.limit_price == 2.80


def test_explicit_spread_fraction_prices_from_bid_toward_ask() -> None:
    result = select_entry_limit(
        PublicQuote(symbol="SMH260717P00280000", bid=2.70, ask=2.90, last=2.80, open_interest=5310),
        {"entry_pricing_spread_fraction": 0.25},
    )

    assert result.limit_price == 2.75
    assert result.evidence()["policy"]["spread_fraction"] == 0.25


def test_oi_percentile_scaling_never_makes_fraction_more_aggressive() -> None:
    assert scale_spread_fraction(0.70, enabled=True, open_interest_percentile=0.25) == pytest.approx(0.175)
    assert scale_spread_fraction(0.70, enabled=True, open_interest_percentile=None) == 0.0
    assert scale_spread_fraction(0.70, enabled=False, open_interest_percentile=0.25) == 0.70


def test_named_entry_profiles_have_distinct_bounded_ladders() -> None:
    assert ENTRY_EXECUTION_PROFILES["patient"].reprice_checkpoints_seconds == (60, 180)
    assert ENTRY_EXECUTION_PROFILES["balanced"].reprice_spread_fractions == (0.60, 0.85)
    assert ENTRY_EXECUTION_PROFILES["urgent"].cancel_after_seconds == 60
    assert ENTRY_EXECUTION_PROFILES["patient"].max_chase_pct == 0.10
    assert ENTRY_EXECUTION_PROFILES["balanced"].max_chase_pct == 0.15
    assert ENTRY_EXECUTION_PROFILES["urgent"].max_chase_pct == 0.25


def test_entry_profile_comparison_prices_all_profiles_without_order_authority() -> None:
    comparison = build_entry_profile_comparison(
        PublicQuote(symbol="SMH260717P00280000", bid=2.70, ask=2.90, last=2.80, open_interest=5310),
        {"min_open_interest": 50, "max_bid_ask_spread_pct": 0.12},
        open_interest_percentile=0.50,
    )

    assert set(comparison) == {"patient", "balanced", "urgent"}
    assert comparison["patient"]["quote_limit_price"] == 2.73
    assert comparison["balanced"]["quote_limit_price"] == 2.74
    assert comparison["urgent"]["quote_limit_price"] == 2.75
    assert comparison["patient"]["effective_spread_fraction"] == 0.125
    assert comparison["patient"]["max_chase_pct"] == 0.10


def test_explicit_initial_fraction_overrides_named_profile_default() -> None:
    fraction, profile = resolve_initial_spread_fraction(
        {
            "entry_execution_profile": "patient",
            "entry_pricing_spread_fraction": 0.40,
        }
    )

    assert profile is not None
    assert profile.name == "patient"
    assert fraction == 0.40


def test_explicit_reprice_chase_cap_overrides_named_profile_default() -> None:
    assert resolve_entry_reprice_max_chase_pct(
        {"entry_execution_profile": "patient", "entry_reprice_max_chase_pct": 0.05}
    ) == 0.05
    assert resolve_entry_reprice_max_chase_pct({"entry_execution_profile": "balanced"}) == 0.15
    assert resolve_entry_reprice_max_chase_pct({}) is None
