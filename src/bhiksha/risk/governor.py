"""Entry risk checks."""

from __future__ import annotations

from dataclasses import dataclass

from bhiksha.config.models import ConservativeRiskProfile


@dataclass(slots=True)
class RiskDecision:
    approved: bool
    reasons: list[str]


class RiskGovernor:
    """Conservative risk checks for the initial runtime."""

    def __init__(self, profile: ConservativeRiskProfile) -> None:
        self.profile = profile

    def check_entry(
        self,
        *,
        total_open_positions: int,
        symbol_open_positions: int,
        deployment_open_positions: int,
        proposed_trade_premium_usd: float,
    ) -> RiskDecision:
        reasons: list[str] = []

        if total_open_positions >= self.profile.max_open_positions_total:
            reasons.append("max_open_positions_total_reached")
        if symbol_open_positions >= self.profile.max_open_positions_per_symbol:
            reasons.append("max_open_positions_per_symbol_reached")
        if deployment_open_positions >= self.profile.max_open_positions_per_deployment:
            reasons.append("max_open_positions_per_deployment_reached")
        if proposed_trade_premium_usd > self.profile.max_trade_premium_usd:
            reasons.append("max_trade_premium_exceeded")

        return RiskDecision(approved=not reasons, reasons=reasons or ["approved"])

