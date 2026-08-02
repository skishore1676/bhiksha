"""Persistence interface placeholders."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
import hashlib
from typing import Any

from bhiksha.domain.models import (
    CashBudgetDay,
    CashBudgetReservation,
    EntryRiskReservation,
    PartialFillRecord,
    TradeRecord,
)
from bhiksha.options.chain_snapshot import ChainSnapshotAttempt


class EventRepository(ABC):
    @abstractmethod
    async def append(self, event_type: str, payload: dict[str, Any]) -> None:
        """Persist an event payload."""


class NullEventRepository(EventRepository):
    """No-op event repository for tests or temporary use."""

    async def append(self, event_type: str, payload: dict[str, Any]) -> None:
        return None


class EvidenceIdentityEventRepository(EventRepository):
    """Decorate deployment-scoped events with immutable evidence lineage."""

    def __init__(
        self,
        delegate: EventRepository,
        deployment_identity: dict[str, dict[str, object]],
    ) -> None:
        self.delegate = delegate
        self.deployment_identity = {
            str(deployment_id): dict(identity)
            for deployment_id, identity in deployment_identity.items()
        }

    async def append(self, event_type: str, payload: dict[str, Any]) -> None:
        deployment_id = str(payload.get("deployment_id") or "")
        identity = self.deployment_identity.get(deployment_id)
        if identity is None:
            await self.delegate.append(event_type, payload)
            return
        evidence_identity = {
            key: identity.get(key)
            for key in (
                "active_plan_id",
                "plan_revision_id",
                "session_id",
                "research_run_id",
                "evidence_packet_id",
                "evidence_artifact_sha256",
                "evidence_artifact_uri",
                "canary_id",
                "canary_authorization_sha256",
            )
        }
        trade_id = str(payload.get("trade_id") or "")
        if trade_id:
            receipt_payload = "|".join(
                [
                    str(evidence_identity.get("plan_revision_id") or ""),
                    str(evidence_identity.get("session_id") or ""),
                    trade_id,
                    str(evidence_identity.get("evidence_packet_id") or ""),
                    str(
                        evidence_identity.get(
                            "canary_authorization_sha256"
                        )
                        or ""
                    ),
                ]
            )
            evidence_identity["fact_receipt_id"] = (
                "sha256:"
                + hashlib.sha256(receipt_payload.encode("utf-8")).hexdigest()
            )
        await self.delegate.append(
            event_type,
            {**payload, "evidence_identity": evidence_identity},
        )


class TradeStateRepository(ABC):
    @abstractmethod
    async def upsert_trade(self, record: TradeRecord) -> None:
        """Create or update the durable trade session."""

    @abstractmethod
    async def mark_closed(
        self,
        trade_id: str,
        *,
        exit_order_id: str | None = None,
        exit_price: float | None = None,
        exit_filled_quantity: int | None = None,
        exit_filled_at: datetime | None = None,
        exit_order_status: str | None = None,
        exit_order_type: str | None = None,
        exit_broker_payload: dict[str, Any] | None = None,
        exit_rule: str | None = None,
    ) -> None:
        """Mark a trade session as closed."""

    @abstractmethod
    async def get_open_trades(self) -> list[TradeRecord]:
        """Return open or pending trade sessions."""

    @abstractmethod
    async def get_recent_trades(self, *, limit: int = 100) -> list[TradeRecord]:
        """Return recent trade sessions, including recently closed rows."""

    @abstractmethod
    async def record_partial_fill(self, record: PartialFillRecord) -> int:
        """Persist a banked partial leg at submission time (ITEM B). Returns the row id."""

    @abstractmethod
    async def enrich_partial_fill(
        self,
        record_id: int,
        *,
        fill_price: float | None = None,
        fill_quantity: int | None = None,
        filled_at: datetime | None = None,
        order_status: str | None = None,
        order_type: str | None = None,
        broker_payload: dict[str, Any] | None = None,
    ) -> None:
        """Backfill a banked partial leg's confirmed fill truth once known (ITEM B)."""

    @abstractmethod
    async def get_unconfirmed_partial_fills(self, *, limit: int = 200) -> list[PartialFillRecord]:
        """Return banked partial legs still missing confirmed fill truth (ITEM B)."""

    @abstractmethod
    async def get_partial_fills(self, trade_id: str) -> list[PartialFillRecord]:
        """Return every banked partial leg recorded for a trade (ITEM B, report reconstruction)."""

    @abstractmethod
    async def get_partial_fills_for_trades(
        self, trade_ids: list[str]
    ) -> dict[str, list[PartialFillRecord]]:
        """Return banked partial legs for several trades in one repository read."""

    @abstractmethod
    async def increment_partial_fill_enrich_attempts(self, record_id: int) -> None:
        """Count one unresolved enrichment poll against a pending partial leg (audit fix 3)."""

    @abstractmethod
    async def mark_partial_fill_abandoned(self, record_id: int, *, reason: str) -> None:
        """Stop re-polling a partial leg that will never resolve, recording why (audit fix 3)."""

    @abstractmethod
    async def upsert_entry_risk_reservation(self, reservation: EntryRiskReservation) -> None:
        """Create or replace a final-sized entry risk reservation."""

    @abstractmethod
    async def get_active_entry_risk_reservations(self) -> list[EntryRiskReservation]:
        """Return reservations which still consume prospective risk headroom."""

    @abstractmethod
    async def mark_entry_risk_reservation_status(self, trade_id: str, status: str) -> None:
        """Transition a final-sized entry risk reservation."""


class NullTradeStateRepository(TradeStateRepository):
    async def upsert_trade(self, record: TradeRecord) -> None:
        return None

    async def mark_closed(
        self,
        trade_id: str,
        *,
        exit_order_id: str | None = None,
        exit_price: float | None = None,
        exit_filled_quantity: int | None = None,
        exit_filled_at: datetime | None = None,
        exit_order_status: str | None = None,
        exit_order_type: str | None = None,
        exit_broker_payload: dict[str, Any] | None = None,
        exit_rule: str | None = None,
    ) -> None:
        del (
            exit_price,
            exit_filled_quantity,
            exit_filled_at,
            exit_order_status,
            exit_order_type,
            exit_broker_payload,
            exit_rule,
        )
        return None

    async def get_open_trades(self) -> list[TradeRecord]:
        return []

    async def get_recent_trades(self, *, limit: int = 100) -> list[TradeRecord]:
        del limit
        return []

    async def record_partial_fill(self, record: PartialFillRecord) -> int:
        del record
        return 0

    async def enrich_partial_fill(
        self,
        record_id: int,
        *,
        fill_price: float | None = None,
        fill_quantity: int | None = None,
        filled_at: datetime | None = None,
        order_status: str | None = None,
        order_type: str | None = None,
        broker_payload: dict[str, Any] | None = None,
    ) -> None:
        del record_id, fill_price, fill_quantity, filled_at, order_status, order_type, broker_payload
        return None

    async def get_unconfirmed_partial_fills(self, *, limit: int = 200) -> list[PartialFillRecord]:
        del limit
        return []

    async def get_partial_fills(self, trade_id: str) -> list[PartialFillRecord]:
        del trade_id
        return []

    async def get_partial_fills_for_trades(
        self, trade_ids: list[str]
    ) -> dict[str, list[PartialFillRecord]]:
        return {trade_id: [] for trade_id in trade_ids}

    async def increment_partial_fill_enrich_attempts(self, record_id: int) -> None:
        del record_id
        return None

    async def mark_partial_fill_abandoned(self, record_id: int, *, reason: str) -> None:
        del record_id, reason
        return None

    async def upsert_entry_risk_reservation(self, reservation: EntryRiskReservation) -> None:
        del reservation
        return None

    async def get_active_entry_risk_reservations(self) -> list[EntryRiskReservation]:
        return []

    async def mark_entry_risk_reservation_status(self, trade_id: str, status: str) -> None:
        del trade_id, status
        return None


class CashBudgetRepository(ABC):
    @abstractmethod
    async def get_day(self, trade_date: str) -> CashBudgetDay | None:
        """Return the stored day budget for the trading date."""

    @abstractmethod
    async def upsert_day(self, day: CashBudgetDay) -> None:
        """Create or update the stored day budget."""

    @abstractmethod
    async def get_reservation(self, trade_id: str) -> CashBudgetReservation | None:
        """Return the reservation for the trade when present."""

    @abstractmethod
    async def upsert_reservation(self, reservation: CashBudgetReservation) -> None:
        """Create or update a reservation row."""

    @abstractmethod
    async def mark_reservation_status(self, trade_id: str, status: str) -> None:
        """Update a reservation status in place."""

    @abstractmethod
    async def reservation_totals(self, trade_date: str) -> dict[str, float]:
        """Return summed reservation amounts by status for the trading date."""


class NullCashBudgetRepository(CashBudgetRepository):
    async def get_day(self, trade_date: str) -> CashBudgetDay | None:
        del trade_date
        return None

    async def upsert_day(self, day: CashBudgetDay) -> None:
        del day
        return None

    async def get_reservation(self, trade_id: str) -> CashBudgetReservation | None:
        del trade_id
        return None

    async def upsert_reservation(self, reservation: CashBudgetReservation) -> None:
        del reservation
        return None

    async def mark_reservation_status(self, trade_id: str, status: str) -> None:
        del trade_id, status
        return None

    async def reservation_totals(self, trade_date: str) -> dict[str, float]:
        del trade_date
        return {"reserved": 0.0, "consumed": 0.0}


class ChainSnapshotRepository(ABC):
    """Persist the full candidate chain (per-contract, verdict-labeled) at
    selection-attempt time -- see options/chain_snapshot.py for what gets
    captured and why. Telemetry only: implementations must never let a
    failure here propagate to the caller (the trade matters more than the
    snapshot); callers still get an exception-free contract to rely on.
    """

    @abstractmethod
    async def record_attempt(self, attempt: ChainSnapshotAttempt) -> None:
        """Persist one selection attempt's summary row and per-contract rows."""

    @abstractmethod
    async def purge_older_than(self, cutoff: datetime) -> int:
        """Delete snapshot rows created before ``cutoff``. Returns rows deleted."""


class NullChainSnapshotRepository(ChainSnapshotRepository):
    """No-op chain-snapshot repository for tests or callers that opt out."""

    async def record_attempt(self, attempt: ChainSnapshotAttempt) -> None:
        del attempt
        return None

    async def purge_older_than(self, cutoff: datetime) -> int:
        del cutoff
        return 0
