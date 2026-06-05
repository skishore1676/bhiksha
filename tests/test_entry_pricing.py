from bhiksha.execution.order_manager import PublicQuote
from bhiksha.execution.pricing import EntryPricingPolicy, select_entry_limit


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
