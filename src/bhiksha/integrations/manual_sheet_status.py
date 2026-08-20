"""Write operator-facing manual row status back to the Google Sheet control plane."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bhiksha.config.models import DeploymentManifest
from bhiksha.domain.models import ExitPlan, SignalDecision, TradePlan
from bhiksha.integrations.google_sheets import GoogleSheetTableClient

STATUS_COLUMNS = [
    "bhiksha_status",
    "bhiksha_last_event_at",
    "bhiksha_last_note",
    "bhiksha_last_trade_id",
]


@dataclass(slots=True)
class ManualSheetStatusWriter:
    client: GoogleSheetTableClient
    row_index_by_deployment: dict[str, int]
    _row_locks: dict[int, asyncio.Lock] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    @classmethod
    def from_active_plan(
        cls,
        *,
        active_plan: dict[str, Any] | None,
        deployments: list[DeploymentManifest],
        credentials_path: str | Path | None,
    ) -> "ManualSheetStatusWriter | None":
        if active_plan is None or credentials_path is None:
            return None
        source = active_plan.get("source") or {}
        spreadsheet_id = source.get("spreadsheet_id")
        manual_sheet_name = source.get("manual_sheet_name")
        if not spreadsheet_id or not manual_sheet_name:
            return None
        client = GoogleSheetTableClient(
            spreadsheet_id=str(spreadsheet_id),
            sheet_name=str(manual_sheet_name),
            credentials_path=Path(credentials_path),
        )
        # Metadata discovery happens during startup and may use the full
        # control-plane retry budget. Intraday status writebacks get exactly
        # one transport retry: enough to recover the transient SSL/5xx class
        # seen in production without turning Sheet projection into an
        # unbounded execution-path dependency.
        client.api_retries = 1
        row_index_by_deployment: dict[str, int] = {}
        for deployment in deployments:
            if not _is_sheet_backed_manual(deployment):
                continue
            metadata = deployment.source.metadata or {}
            if metadata.get("sheet_name") != client.sheet_name:
                continue
            row_index = metadata.get("row_index")
            if isinstance(row_index, int) and row_index > 0:
                row_index_by_deployment[deployment.deployment_id] = row_index
        if not row_index_by_deployment:
            return None
        return cls(client=client, row_index_by_deployment=row_index_by_deployment)

    async def mark_signal_triggered(self, deployment: DeploymentManifest, decision: SignalDecision) -> str | None:
        return await self._write_status(
            deployment,
            status="triggered",
            event_at=decision.timestamp,
            note=_trim_note(
                f"{decision.direction.value if decision.direction else 'unknown'} trigger met: {', '.join(decision.reason)}"
            ),
            disable_row=True,
        )

    async def mark_entry_blocked(
        self,
        deployment: DeploymentManifest,
        *,
        event_at: datetime,
        note: str,
        trade_id: str | None = None,
    ) -> str | None:
        return await self._write_status(
            deployment,
            status="entry_blocked",
            event_at=event_at,
            note=_trim_note(note),
            trade_id=trade_id,
        )

    async def mark_entry_planned(
        self,
        deployment: DeploymentManifest,
        *,
        plan: TradePlan,
        mode: str,
    ) -> str | None:
        status = "entry_submitted" if mode == "live" else "entry_planned"
        note = _trim_note(
            f"{mode} option={plan.option_symbol} qty={plan.quantity} est={round(plan.estimated_entry_price, 2)}"
        )
        return await self._write_status(
            deployment,
            status=status,
            event_at=plan.entry_timestamp or datetime.now(UTC),
            note=note,
            trade_id=plan.trade_id,
        )

    async def mark_entry_error(
        self,
        deployment: DeploymentManifest,
        *,
        event_at: datetime,
        error: str,
    ) -> str | None:
        return await self._write_status(
            deployment,
            status="entry_error",
            event_at=event_at,
            note=_trim_note(error),
        )

    async def mark_exit_submitted(self, deployment: DeploymentManifest, *, plan: ExitPlan) -> str | None:
        return await self._write_status(
            deployment,
            status="exit_pending",
            event_at=datetime.now(UTC),
            note=_trim_note(f"option={plan.option_symbol} order_id={plan.order_id or 'pending'}"),
            trade_id=plan.trade_id,
        )

    async def mark_closed(
        self,
        deployment: DeploymentManifest,
        *,
        trade_id: str | None,
        note: str,
        event_at: datetime | None = None,
    ) -> str | None:
        return await self._write_status(
            deployment,
            status="closed",
            event_at=event_at or datetime.now(UTC),
            note=_trim_note(note),
            trade_id=trade_id,
        )

    async def _write_status(
        self,
        deployment: DeploymentManifest,
        *,
        status: str,
        event_at: datetime,
        note: str,
        trade_id: str | None = None,
        disable_row: bool = False,
    ) -> str | None:
        row_index = self.row_index_by_deployment.get(deployment.deployment_id)
        if row_index is None:
            return None
        payload = {
            "bhiksha_status": status,
            "bhiksha_last_event_at": event_at.astimezone(UTC).isoformat(),
            "bhiksha_last_note": note,
            "bhiksha_last_trade_id": trade_id or "",
        }
        if disable_row:
            payload["enabled"] = False
        # Cartographer status calls are dispatched without blocking execution.
        # Serialize writes to one physical row so a recovered earlier write
        # cannot overtake a later terminal/planned status.
        row_lock = self._row_locks.setdefault(row_index, asyncio.Lock())
        try:
            async with row_lock:
                await asyncio.to_thread(
                    self.client.update_row_cells,
                    row_index=row_index,
                    values=payload,
                )
        except Exception as exc:  # pragma: no cover - exercised via runtime fallback
            return str(exc)
        return None


def _is_sheet_backed_manual(deployment: DeploymentManifest) -> bool:
    metadata = deployment.source.metadata or {}
    return (
        deployment.source.origin == "active_sheet_manual"
        and metadata.get("row_index") is not None
        and metadata.get("sheet_name") is not None
    )


def _trim_note(value: str, *, max_length: int = 240) -> str:
    compact = " ".join(str(value).split())
    if len(compact) <= max_length:
        return compact
    return compact[: max_length - 3] + "..."


__all__ = ["ManualSheetStatusWriter", "STATUS_COLUMNS"]
