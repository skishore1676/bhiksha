"""Conservative settled-cash guardrails for cash brokerage accounts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import os
from zoneinfo import ZoneInfo

from bhiksha.domain.models import CashBudgetDay, CashBudgetReservation, TradeRecord
from bhiksha.persistence.repository import CashBudgetRepository, NullCashBudgetRepository
from bhiksha.state.position_tracker import TrackedPosition

ET = ZoneInfo("America/New_York")
ACTIVE_RESERVATION_STATUSES = frozenset({"reserved", "consumed"})


@dataclass(slots=True, frozen=True)
class CashGuardResult:
    enforced: bool
    blocked: bool
    reason: str | None = None
    details: dict[str, float | str | bool | None] = field(default_factory=dict)


class CashGuard:
    """Tracks a conservative daily buying budget for cash accounts."""

    def __init__(
        self,
        *,
        order_manager,
        repository: CashBudgetRepository | None = None,
    ) -> None:
        self.order_manager = order_manager
        self.repository = repository or NullCashBudgetRepository()

    async def reserve_entry(
        self,
        *,
        trade_id: str,
        required_cash: float,
        timestamp: datetime,
    ) -> CashGuardResult:
        mode = cash_guard_mode()
        if mode == "off":
            return CashGuardResult(enforced=False, blocked=False)

        account_type = await self._account_type()
        if not _should_enforce(mode, account_type):
            return CashGuardResult(
                enforced=False,
                blocked=False,
                details={"cash_guard_mode": mode, "account_type": account_type},
            )

        trade_date = trade_date_et(timestamp)
        day = await self.repository.get_day(trade_date)
        if day is None:
            try:
                portfolio = await self.order_manager.get_portfolio()
            except Exception as exc:
                return CashGuardResult(
                    enforced=True,
                    blocked=True,
                    reason="cash_guard_portfolio_unavailable",
                    details={"error": str(exc), "cash_guard_mode": mode, "account_type": account_type},
                )
            broker_cash = _portfolio_cash_only_buying_power(portfolio)
            if broker_cash is None:
                return CashGuardResult(
                    enforced=True,
                    blocked=True,
                    reason="cash_guard_cash_unavailable",
                    details={"cash_guard_mode": mode, "account_type": account_type},
                )
            buffer_pct = cash_guard_buffer_pct()
            day = CashBudgetDay(
                trade_date=trade_date,
                account_type=account_type,
                broker_cash_only_buying_power=broker_cash,
                usable_budget=round(max(0.0, broker_cash * (1.0 - buffer_pct)), 2),
                buffer_pct=buffer_pct,
            )
            await self.repository.upsert_day(day)

        totals = await self.repository.reservation_totals(trade_date)
        consumed_cash = sum(
            totals.get(status, 0.0)
            for status in ACTIVE_RESERVATION_STATUSES
        )
        remaining_budget = round(max(0.0, day.usable_budget - consumed_cash), 2)
        required_cash = round(max(required_cash, 0.0), 2)
        if required_cash > remaining_budget:
            return CashGuardResult(
                enforced=True,
                blocked=True,
                reason="insufficient_internal_settled_cash_budget",
                details={
                    "required_cash": required_cash,
                    "remaining_budget": remaining_budget,
                    "usable_budget": day.usable_budget,
                    "broker_cash_only_buying_power": day.broker_cash_only_buying_power,
                    "buffer_pct": day.buffer_pct,
                    "account_type": account_type,
                    "cash_guard_mode": mode,
                },
            )

        await self.repository.upsert_reservation(
            CashBudgetReservation(
                trade_id=trade_id,
                trade_date=trade_date,
                amount=required_cash,
                status="reserved",
            )
        )
        return CashGuardResult(
            enforced=True,
            blocked=False,
            details={
                "reserved_cash": required_cash,
                "remaining_budget": round(max(0.0, remaining_budget - required_cash), 2),
                "usable_budget": day.usable_budget,
                "broker_cash_only_buying_power": day.broker_cash_only_buying_power,
                "buffer_pct": day.buffer_pct,
                "account_type": account_type,
                "cash_guard_mode": mode,
            },
        )

    async def finalize_entry(self, trade_id: str) -> None:
        reservation = await self.repository.get_reservation(trade_id)
        if reservation is None or reservation.status == "consumed":
            return
        await self.repository.mark_reservation_status(trade_id, "consumed")

    async def release_entry(self, trade_id: str) -> None:
        reservation = await self.repository.get_reservation(trade_id)
        if reservation is None or reservation.status == "released":
            return
        await self.repository.mark_reservation_status(trade_id, "released")

    async def sync_positions(self, positions: list[TrackedPosition], trades: list[TradeRecord]) -> None:
        mode = cash_guard_mode()
        if mode == "off":
            return
        account_type = await self._account_type()
        if not _should_enforce(mode, account_type):
            return
        for position in positions:
            if position.trade_id is None or position.entry_timestamp is None:
                continue
            reservation = await self.repository.get_reservation(position.trade_id)
            if reservation is None:
                amount = _position_cash_cost(position)
                if amount is None:
                    continue
                await self._ensure_day(position.entry_timestamp, account_type=account_type)
                await self.repository.upsert_reservation(
                    CashBudgetReservation(
                        trade_id=position.trade_id,
                        trade_date=trade_date_et(position.entry_timestamp),
                        amount=amount,
                        status="consumed",
                    )
                )
                continue
            if reservation.status == "reserved":
                await self.repository.mark_reservation_status(position.trade_id, "consumed")
        for trade in trades:
            if trade.trade_id is None or trade.entry_timestamp is None:
                continue
            if trade.status != "pending_entry":
                continue
            reservation = await self.repository.get_reservation(trade.trade_id)
            if reservation is not None:
                continue
            amount = _trade_cash_cost(trade)
            if amount is None:
                continue
            await self._ensure_day(trade.entry_timestamp, account_type=account_type)
            await self.repository.upsert_reservation(
                CashBudgetReservation(
                    trade_id=trade.trade_id,
                    trade_date=trade_date_et(trade.entry_timestamp),
                    amount=amount,
                    status="reserved",
                )
            )

    async def _ensure_day(self, timestamp: datetime, *, account_type: str | None) -> CashBudgetDay | None:
        trade_date = trade_date_et(timestamp)
        day = await self.repository.get_day(trade_date)
        if day is not None:
            return day
        try:
            portfolio = await self.order_manager.get_portfolio()
        except Exception:
            return None
        broker_cash = _portfolio_cash_only_buying_power(portfolio)
        if broker_cash is None:
            return None
        day = CashBudgetDay(
            trade_date=trade_date,
            account_type=account_type,
            broker_cash_only_buying_power=broker_cash,
            usable_budget=round(max(0.0, broker_cash * (1.0 - cash_guard_buffer_pct())), 2),
            buffer_pct=cash_guard_buffer_pct(),
        )
        await self.repository.upsert_day(day)
        return day

    async def _account_type(self) -> str | None:
        try:
            payload = await self.order_manager.get_account_info()
        except Exception:
            return None
        account_type = payload.get("brokerageAccountType")
        if not account_type:
            return None
        return str(account_type).upper()


def cash_guard_mode() -> str:
    raw = os.getenv("BHIKSHA_CASH_GUARD_MODE")
    if raw:
        normalized = raw.strip().lower()
        if normalized in {"auto", "on", "off"}:
            return normalized
    legacy = os.getenv("BHIKSHA_ENFORCE_CASH_ONLY_BUYING_POWER")
    if legacy is None:
        return "auto"
    return "on" if legacy.strip().lower() in {"1", "true", "yes", "on"} else "off"


def cash_guard_buffer_pct() -> float:
    raw = os.getenv("BHIKSHA_CASH_GUARD_BUFFER_PCT")
    if raw is None:
        return 0.05
    try:
        parsed = float(raw)
    except ValueError:
        return 0.05
    return min(max(parsed, 0.0), 0.5)


def trade_date_et(timestamp: datetime) -> str:
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=ZoneInfo("UTC"))
    return timestamp.astimezone(ET).date().isoformat()


def _should_enforce(mode: str, account_type: str | None) -> bool:
    if mode == "on":
        return True
    if mode == "off":
        return False
    if account_type == "MARGIN":
        return False
    return True


def _portfolio_cash_only_buying_power(portfolio: dict) -> float | None:
    buying_power = portfolio.get("buyingPower") or {}
    value = buying_power.get("cashOnlyBuyingPower")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _position_cash_cost(position: TrackedPosition) -> float | None:
    if position.entry_price is None or position.quantity <= 0:
        return None
    return round(position.entry_price * position.quantity * 100, 2)


def _trade_cash_cost(trade: TradeRecord) -> float | None:
    if trade.entry_price is None or trade.quantity <= 0:
        return None
    return round(trade.entry_price * trade.quantity * 100, 2)
