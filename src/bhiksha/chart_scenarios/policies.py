"""Executable, content-addressed observation policies for the shadow lane."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from mala_bhiksha_kernel import canonical_sha256
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import OptionQuoteSnapshot, as_utc


class CostModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["market-context-cost-model.v1"]
    contract_multiplier: int = Field(gt=0)
    contracts: Literal[1]
    entry_fee_per_contract_usd: float = Field(ge=0, allow_inf_nan=False)
    exit_fee_per_contract_usd: float = Field(ge=0, allow_inf_nan=False)
    entry_slippage_per_contract_usd: float = Field(ge=0, allow_inf_nan=False)
    exit_slippage_per_contract_usd: float = Field(ge=0, allow_inf_nan=False)
    content_hash: str | None = None

    @model_validator(mode="after")
    def bind_hash(self) -> CostModel:
        payload = self.model_dump(mode="json", exclude={"content_hash"})
        computed = canonical_sha256(payload)
        if (
            self.content_hash is not None
            and self.content_hash.removeprefix("sha256:") != computed
        ):
            raise ValueError("cost model content_hash mismatch")
        object.__setattr__(self, "content_hash", computed)
        return self

    @property
    def total_round_trip_cost_usd(self) -> float:
        return self.contracts * (
            self.entry_fee_per_contract_usd
            + self.exit_fee_per_contract_usd
            + self.entry_slippage_per_contract_usd
            + self.exit_slippage_per_contract_usd
        )


class QuoteEligibilityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["market-context-quote-eligibility.v1"]
    require_bid_ask: bool
    allow_last_fallback: bool
    max_spread_pct: float = Field(gt=0, le=1, allow_inf_nan=False)
    max_quote_age_seconds: int = Field(ge=0)
    require_positive_mark: bool
    content_hash: str | None = None

    @model_validator(mode="after")
    def bind_hash(self) -> QuoteEligibilityPolicy:
        if self.require_bid_ask and self.allow_last_fallback:
            raise ValueError("require_bid_ask cannot allow last fallback")
        payload = self.model_dump(mode="json", exclude={"content_hash"})
        computed = canonical_sha256(payload)
        if (
            self.content_hash is not None
            and self.content_hash.removeprefix("sha256:") != computed
        ):
            raise ValueError("quote policy content_hash mismatch")
        object.__setattr__(self, "content_hash", computed)
        return self

    def eligible(self, quote: OptionQuoteSnapshot, *, evaluated_at: datetime) -> bool:
        now = as_utc(evaluated_at)
        age = (now - quote.quote_time).total_seconds()
        if age < 0 or age > self.max_quote_age_seconds:
            return False
        if self.require_bid_ask and (quote.bid is None or quote.ask is None):
            return False
        if quote.bid is not None and quote.ask is not None:
            if quote.bid < 0 or quote.ask < quote.bid:
                return False
            mark = (quote.bid + quote.ask) / 2.0
            if mark <= 0:
                return False
            if (quote.ask - quote.bid) / mark > self.max_spread_pct:
                return False
        elif not self.allow_last_fallback:
            return False
        mark = quote.mark
        if self.require_positive_mark and (mark is None or mark <= 0):
            return False
        return mark is not None


__all__ = ["CostModel", "QuoteEligibilityPolicy"]
