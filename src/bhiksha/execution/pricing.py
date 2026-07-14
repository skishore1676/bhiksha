"""Single-leg entry limit pricing for fast Bhiksha option entries."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from bhiksha.execution.order_manager import PublicQuote, round_price

EntryPricingMode = Literal["passive", "balanced", "urgent", "cross"]
EntryExecutionProfileName = Literal["patient", "balanced", "urgent"]


@dataclass(frozen=True, slots=True)
class EntryExecutionProfile:
    name: EntryExecutionProfileName
    initial_spread_fraction: float
    reprice_checkpoints_seconds: tuple[int, ...]
    reprice_spread_fractions: tuple[float, ...]
    cancel_after_seconds: int


ENTRY_EXECUTION_PROFILES: dict[EntryExecutionProfileName, EntryExecutionProfile] = {
    "patient": EntryExecutionProfile("patient", 0.25, (60, 180), (0.50, 0.70), 300),
    "balanced": EntryExecutionProfile("balanced", 0.35, (30, 90), (0.60, 0.85), 150),
    "urgent": EntryExecutionProfile("urgent", 0.50, (15, 30), (0.80, 1.00), 60),
}


@dataclass(frozen=True, slots=True)
class EntryPricingPolicy:
    mode: EntryPricingMode = "urgent"
    urgent_spread_pct: float = 0.25
    passive_spread_pct: float = 0.25
    cross_tight_spread_pct: float = 0.03
    spread_fraction: float | None = None
    require_two_sided_quote: bool = True
    require_open_interest: bool = True

    @classmethod
    def from_execution_params(cls, params: dict[str, Any] | None = None) -> "EntryPricingPolicy":
        raw = params or {}
        nested = raw.get("entry_pricing")
        if isinstance(nested, dict):
            raw = {**raw, **nested}
        mode = str(raw.get("entry_pricing_mode") or raw.get("mode") or "urgent").strip().lower()
        if mode not in {"passive", "balanced", "urgent", "cross"}:
            mode = "urgent"
        return cls(
            mode=mode,  # type: ignore[arg-type]
            urgent_spread_pct=_as_float(
                _first_present(raw, "entry_pricing_urgent_spread_pct", "urgent_spread_pct"),
                0.25,
            ),
            passive_spread_pct=_as_float(
                _first_present(raw, "entry_pricing_passive_spread_pct", "passive_spread_pct"),
                0.25,
            ),
            cross_tight_spread_pct=_as_float(
                _first_present(raw, "entry_pricing_cross_tight_spread_pct", "cross_tight_spread_pct"),
                0.03,
            ),
            spread_fraction=_optional_fraction(
                _first_present(raw, "entry_pricing_spread_fraction", "spread_fraction")
            ),
            require_two_sided_quote=_as_bool(
                _first_present(raw, "entry_pricing_require_two_sided_quote", "require_two_sided_quote"),
                True,
            ),
            require_open_interest=_as_bool(
                _first_present(raw, "entry_pricing_require_open_interest", "require_open_interest"),
                True,
            ),
        )


@dataclass(frozen=True, slots=True)
class EntryPricingResult:
    policy: EntryPricingPolicy
    quote: PublicQuote
    limit_price: float | None
    block_reasons: list[str]

    @property
    def approved(self) -> bool:
        return self.limit_price is not None and not self.block_reasons

    def evidence(self) -> dict[str, Any]:
        bid = self.quote.bid
        ask = self.quote.ask
        mid = quote_mid(self.quote)
        spread_abs = quote_spread_abs(self.quote)
        payload = {
            "option_symbol": self.quote.symbol,
            "pricing_mode": self.policy.mode,
            "selected_limit_price": self.limit_price,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "last": self.quote.last,
            "open_interest": self.quote.open_interest,
            "spread_abs": spread_abs,
            "spread_pct": self.quote.spread_pct,
            "quote_timestamp": self.quote.quote_timestamp,
            "quote_outcome": self.quote.outcome,
            "block_reasons": list(self.block_reasons),
            "policy": asdict(self.policy),
        }
        return payload


def select_entry_limit(
    quote: PublicQuote,
    execution_params: dict[str, Any] | None = None,
    *,
    policy: EntryPricingPolicy | None = None,
) -> EntryPricingResult:
    active_policy = policy or EntryPricingPolicy.from_execution_params(execution_params)
    block_reasons = _quote_blocks(quote, execution_params or {}, active_policy)
    if block_reasons:
        return EntryPricingResult(policy=active_policy, quote=quote, limit_price=None, block_reasons=block_reasons)

    bid = float(quote.bid)  # guarded by _quote_blocks
    ask = float(quote.ask)  # guarded by _quote_blocks
    spread = ask - bid
    mid = (bid + ask) / 2.0

    if active_policy.spread_fraction is not None:
        limit_price = bid + spread * active_policy.spread_fraction
    elif active_policy.mode == "cross" or (
        active_policy.mode == "urgent"
        and quote.spread_pct is not None
        and quote.spread_pct <= active_policy.cross_tight_spread_pct
    ):
        limit_price = ask
    elif active_policy.mode == "balanced":
        limit_price = mid
    elif active_policy.mode == "passive":
        limit_price = mid - spread * active_policy.passive_spread_pct
    else:
        limit_price = mid + spread * active_policy.urgent_spread_pct

    return EntryPricingResult(
        policy=active_policy,
        quote=quote,
        limit_price=round_price(max(0.01, min(limit_price, ask))),
        block_reasons=[],
    )


def get_entry_execution_profile(value: Any) -> EntryExecutionProfile | None:
    normalized = str(value or "").strip().lower()
    return ENTRY_EXECUTION_PROFILES.get(normalized)  # type: ignore[arg-type]


def resolve_initial_spread_fraction(
    execution_params: dict[str, Any],
) -> tuple[float | None, EntryExecutionProfile | None]:
    profile = get_entry_execution_profile(execution_params.get("entry_execution_profile"))
    explicit = _optional_fraction(execution_params.get("entry_pricing_spread_fraction"))
    if explicit is not None:
        return explicit, profile
    return (profile.initial_spread_fraction if profile is not None else None), profile


def build_entry_profile_comparison(
    quote: PublicQuote,
    execution_params: dict[str, Any],
    *,
    open_interest_percentile: float | None,
) -> dict[str, dict[str, Any]]:
    """Price all named profiles from one quote without granting order authority."""
    comparison: dict[str, dict[str, Any]] = {}
    for name, profile in ENTRY_EXECUTION_PROFILES.items():
        effective_fraction = scale_spread_fraction(
            profile.initial_spread_fraction,
            enabled=True,
            open_interest_percentile=open_interest_percentile,
        )
        params = {**execution_params, "entry_pricing_spread_fraction": effective_fraction}
        result = select_entry_limit(quote, params)
        savings_vs_ask = None
        if result.limit_price is not None and quote.ask is not None:
            savings_vs_ask = round_price(float(quote.ask) - result.limit_price)
        comparison[name] = {
            "base_spread_fraction": profile.initial_spread_fraction,
            "effective_spread_fraction": effective_fraction,
            "quote_limit_price": result.limit_price,
            "savings_vs_ask": savings_vs_ask,
            "block_reasons": list(result.block_reasons),
            "reprice_checkpoints_seconds": list(profile.reprice_checkpoints_seconds),
            "reprice_spread_fractions": list(profile.reprice_spread_fractions),
            "cancel_after_seconds": profile.cancel_after_seconds,
        }
    return comparison


def quote_mid(quote: PublicQuote) -> float | None:
    if quote.bid is None or quote.ask is None:
        return None
    return round_price((float(quote.bid) + float(quote.ask)) / 2.0)


def quote_spread_abs(quote: PublicQuote) -> float | None:
    if quote.bid is None or quote.ask is None:
        return None
    return round_price(float(quote.ask) - float(quote.bid))


def scale_spread_fraction(base_fraction: float, *, enabled: bool, open_interest_percentile: float | None) -> float:
    """Moderate bid-to-ask movement using current-chain liquidity evidence."""
    base = max(0.0, min(float(base_fraction), 1.0))
    if not enabled:
        return base
    percentile = 0.0 if open_interest_percentile is None else float(open_interest_percentile)
    return base * max(0.0, min(percentile, 1.0))


def _quote_blocks(quote: PublicQuote, execution_params: dict[str, Any], policy: EntryPricingPolicy) -> list[str]:
    blocks: list[str] = []
    if policy.require_two_sided_quote:
        if quote.bid is None or quote.ask is None or quote.bid <= 0 or quote.ask <= 0:
            blocks.append("public_quote_missing_bid_ask")
        elif quote.ask < quote.bid:
            blocks.append("public_quote_crossed_bid_ask")
    elif quote.entry_reference_price is None:
        blocks.append("public_quote_missing_price")

    if policy.require_open_interest and quote.open_interest is None:
        blocks.append("public_open_interest_missing")
    elif quote.open_interest is not None and quote.open_interest <= 0:
        blocks.append("public_open_interest_missing")

    min_oi = _optional_float(execution_params.get("min_open_interest"))
    if min_oi is not None and quote.open_interest is not None and quote.open_interest < min_oi:
        blocks.append("public_open_interest_below_minimum")

    max_spread_pct = _optional_float(execution_params.get("max_bid_ask_spread_pct"))
    if max_spread_pct is not None:
        if quote.spread_pct is None:
            blocks.append("public_spread_unavailable")
        elif quote.spread_pct > max_spread_pct:
            blocks.append("public_spread_above_maximum")
    return blocks


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _optional_fraction(value: Any) -> float | None:
    parsed = _optional_float(value)
    if parsed is None or parsed < 0.0 or parsed > 1.0:
        return None
    return parsed


def _first_present(raw: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in raw:
            return raw[key]
    return None


def _optional_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
