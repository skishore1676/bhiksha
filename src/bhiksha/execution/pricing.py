"""Single-leg entry limit pricing for fast Bhiksha option entries."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import math
from typing import Any, Literal

from bhiksha.execution.order_manager import PublicQuote, round_price
from bhiksha.execution.quote_lineage import quote_timestamp_evidence

EntryPricingMode = Literal["passive", "balanced", "urgent", "cross", "price_seeking"]
EntryExecutionProfileName = Literal["patient", "balanced", "urgent"]


@dataclass(frozen=True, slots=True)
class EntryExecutionProfile:
    name: EntryExecutionProfileName
    initial_spread_fraction: float
    reprice_checkpoints_seconds: tuple[int, ...]
    reprice_spread_fractions: tuple[float, ...]
    cancel_after_seconds: int
    max_chase_pct: float


ENTRY_EXECUTION_PROFILES: dict[EntryExecutionProfileName, EntryExecutionProfile] = {
    "patient": EntryExecutionProfile("patient", 0.50, (60,), (1.00,), 300, 0.10),
    "balanced": EntryExecutionProfile("balanced", 0.50, (30,), (1.00,), 150, 0.15),
    "urgent": EntryExecutionProfile("urgent", 0.50, (15,), (1.00,), 60, 0.25),
}

# The Sheet's OI and spread values describe the preferred market. Hard quote
# sanity is separate and shared by every lane, without a second set of Sheet controls.
MAX_ABSURD_ENTRY_SPREAD_PCT = 1.50
PRICE_THROUGH_CONCESSION_USD = 0.10
ENTRY_POLICY_VERSION = "midpoint_entry_v2"


@dataclass(frozen=True, slots=True)
class EntryPricingPolicy:
    mode: EntryPricingMode = "urgent"
    require_two_sided_quote: bool = True
    require_open_interest: bool = True
    preferred_min_open_interest: int | None = None
    preferred_max_bid_ask_spread_pct: float | None = None

    @classmethod
    def from_execution_params(cls, params: dict[str, Any] | None = None) -> "EntryPricingPolicy":
        raw = params or {}
        nested = raw.get("entry_pricing")
        if isinstance(nested, dict):
            raw = {**raw, **nested}
        preferred_oi = _optional_int(_first_present(
            raw, "preferred_min_open_interest", "entry_pricing_preferred_min_open_interest"))
        if preferred_oi is None:
            preferred_oi = _optional_int(raw.get("min_open_interest"))
        preferred_spread = _optional_float(_first_present(
            raw, "preferred_max_bid_ask_spread_pct", "entry_pricing_preferred_max_bid_ask_spread_pct"))
        if preferred_spread is None:
            preferred_spread = _optional_float(raw.get("max_bid_ask_spread_pct"))
        mode = str(raw.get("entry_pricing_mode") or raw.get("mode") or "urgent").strip().lower()
        if mode not in {"passive", "balanced", "urgent", "cross", "price_seeking"}:
            mode = "urgent"
        return cls(
            mode=mode,  # type: ignore[arg-type]
            require_two_sided_quote=_as_bool(
                _first_present(raw, "entry_pricing_require_two_sided_quote", "require_two_sided_quote"),
                True,
            ),
            require_open_interest=_as_bool(
                _first_present(raw, "entry_pricing_require_open_interest", "require_open_interest"),
                True,
            ),
            preferred_min_open_interest=preferred_oi,
            preferred_max_bid_ask_spread_pct=preferred_spread,

        )


@dataclass(frozen=True, slots=True)
class EntryPricingResult:
    policy: EntryPricingPolicy
    quote: PublicQuote
    limit_price: float | None
    block_reasons: list[str]
    price_improvement_applied: bool = False
    observed_at: datetime | None = None
    max_entry_price: float | None = None
    original_mid: float | None = None
    wide_market: bool = False

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
            "price_improvement_applied": self.price_improvement_applied,
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
            "liquidity_warnings": liquidity_warnings(self.quote, self.policy),
            "liquidity_policy": ENTRY_POLICY_VERSION,
            "entry_policy_version": ENTRY_POLICY_VERSION,
            "max_entry_price": self.max_entry_price,
            "original_mid": self.original_mid,
            "wide_market": self.wide_market,
            "chase_reference": "original_midpoint",
            "policy": asdict(self.policy),
            **quote_timestamp_evidence(self.quote, self.observed_at or datetime.now(UTC)),
        }
        return payload


def select_entry_limit(
    quote: PublicQuote,
    execution_params: dict[str, Any] | None = None,
    *,
    policy: EntryPricingPolicy | None = None,
    observed_at: datetime | None = None,
) -> EntryPricingResult:
    params = execution_params or {}
    active_policy = policy or EntryPricingPolicy.from_execution_params(params)
    observed_at = observed_at or datetime.now(UTC)
    blocks = _quote_blocks(quote, params, active_policy, observed_at=observed_at)
    if blocks:
        return EntryPricingResult(active_policy, quote, None, blocks, observed_at=observed_at)
    bid, ask = float(quote.bid), float(quote.ask)
    mid = (bid + ask) / 2.0
    original_mid = _optional_float(params.get("entry_original_mid")) or mid
    wide = bool(params.get("entry_wide_market", is_wide_market(quote, active_policy)))
    ceiling = _optional_float(params.get("entry_price_ceiling"))
    if ceiling is None:
        chase = resolve_entry_reprice_max_chase_pct(params)
        ceiling = original_mid * (1.0 + (chase if chase is not None else .15))
        if wide:
            ceiling = min(ceiling, original_mid + PRICE_THROUGH_CONCESSION_USD)
    # An explicit limit remains an additional ceiling, never permission to chase.
    explicit = _optional_float(params.get("entry_price_through_target"))
    if explicit is not None:
        ceiling = min(ceiling, explicit)
    replacement = bool(params.get("entry_reprice_step"))
    proposed = (mid if wide else ask) if replacement else (bid + .25 * (ask-bid) if wide else mid)
    if not math.isfinite(ceiling) or ceiling <= 0:
        return EntryPricingResult(active_policy, quote, None, ["entry_price_ceiling_unusable"], observed_at=observed_at)
    limit = floor_entry_price(min(proposed, ask, ceiling))
    if limit <= 0:
        return EntryPricingResult(active_policy, quote, None, ["entry_price_ceiling_unusable"], observed_at=observed_at)
    return EntryPricingResult(active_policy, quote, limit, [], wide, observed_at,
                              ceiling, original_mid, wide)


def floor_entry_price(value: float, increment: float = .01) -> float:
    """Pure BUY-ceiling rounding, shared by broker and modeled decisions."""
    from bhiksha.execution.order_manager import snap_price
    return snap_price(value, increment, side="BUY")


def is_wide_market(quote: PublicQuote, policy: EntryPricingPolicy) -> bool:
    threshold = policy.preferred_max_bid_ask_spread_pct or .20
    return quote.spread_pct is not None and quote.spread_pct > threshold


def get_entry_execution_profile(value: Any) -> EntryExecutionProfile | None:
    normalized = str(value or "").strip().lower()
    return ENTRY_EXECUTION_PROFILES.get(normalized)  # type: ignore[arg-type]


def resolve_initial_spread_fraction(execution_params: dict[str, Any]) -> tuple[float, EntryExecutionProfile | None]:
    """Compatibility readback: normal markets now start at midpoint."""
    return .5, get_entry_execution_profile(execution_params.get("entry_execution_profile"))


def resolve_entry_reprice_max_chase_pct(execution_params: dict[str, Any]) -> float | None:
    explicit = _optional_fraction(execution_params.get("entry_reprice_max_chase_pct"))
    if explicit is not None:
        return explicit
    profile = get_entry_execution_profile(execution_params.get("entry_execution_profile"))
    return profile.max_chase_pct if profile is not None else None


def build_entry_profile_comparison(
    quote: PublicQuote,
    execution_params: dict[str, Any],
    *,
    open_interest_percentile: float | None,
) -> dict[str, dict[str, Any]]:
    """Price all named profiles from one quote without granting order authority."""
    comparison: dict[str, dict[str, Any]] = {}
    for name, profile in ENTRY_EXECUTION_PROFILES.items():
        result = select_entry_limit(quote, {**execution_params, "entry_execution_profile": name,
            "entry_reprice_max_chase_pct": profile.max_chase_pct})
        comparison[name] = {
            "base_spread_fraction": .5, "effective_spread_fraction": .25 if result.wide_market else .5,
            "quote_limit_price": result.limit_price,
            "max_entry_price": result.max_entry_price,
            "savings_vs_ask": round_price(float(quote.ask) - result.limit_price)
                if result.limit_price is not None and quote.ask is not None else None,
            "block_reasons": list(result.block_reasons),
            "reprice_checkpoints_seconds": list(profile.reprice_checkpoints_seconds),
            "reprice_spread_fractions": list(profile.reprice_spread_fractions),
            "cancel_after_seconds": profile.cancel_after_seconds, "max_chase_pct": profile.max_chase_pct,
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


def liquidity_warnings(quote: PublicQuote, policy: EntryPricingPolicy) -> list[str]:
    warnings: list[str] = []
    if (policy.preferred_min_open_interest is not None and quote.open_interest is not None
            and quote.open_interest < policy.preferred_min_open_interest):
        warnings.append("open_interest_below_preferred")
    if (policy.preferred_max_bid_ask_spread_pct is not None and quote.spread_pct is not None
            and quote.spread_pct > policy.preferred_max_bid_ask_spread_pct):
        warnings.append("spread_above_preferred")
    return warnings


def _quote_blocks(
    quote: PublicQuote, execution_params: dict[str, Any], policy: EntryPricingPolicy,
    *, price_through: bool = False, observed_at: datetime | None = None,
) -> list[str]:
    blocks: list[str] = []
    if any(value is not None and not math.isfinite(value) for value in (quote.bid, quote.ask, quote.open_interest)):
        return ["public_quote_nonfinite"]
    # Two-sided freshness is required for both initial and replacement decisions.
    status = quote_timestamp_evidence(quote, observed_at or datetime.now(UTC))["quote_timestamp_status"]
    if status != "current":
        blocks.append("public_quote_timestamp_missing" if status == "missing" else "public_quote_stale_or_unproven")
    if quote.bid is None or quote.ask is None or quote.bid <= 0 or quote.ask <= 0:
        blocks.append("public_quote_missing_bid_ask")
    elif quote.ask < quote.bid:
        blocks.append("public_quote_crossed_bid_ask")

    if policy.require_open_interest and quote.open_interest is None:
        blocks.append("public_open_interest_missing")
    elif quote.open_interest is not None and quote.open_interest <= 0:
        blocks.append("public_open_interest_missing")

    if quote.spread_pct is None and quote.bid is not None and quote.ask is not None:
        blocks.append("public_spread_unavailable")
    elif quote.spread_pct is not None and quote.spread_pct > MAX_ABSURD_ENTRY_SPREAD_PCT:
        blocks.append("public_spread_absurd")
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


def _optional_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None
