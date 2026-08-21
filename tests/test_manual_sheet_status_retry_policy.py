from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import threading
import time
from types import SimpleNamespace

from bhiksha.integrations import manual_sheet_status


def test_manual_status_writebacks_allow_one_bounded_retry_after_startup_metadata(
    tmp_path, monkeypatch
) -> None:
    class _FakeClient:
        sheet_name = "manual_entry"
        api_retries = 4

    client = _FakeClient()
    monkeypatch.setattr(
        manual_sheet_status,
        "GoogleSheetTableClient",
        lambda **kwargs: client,
    )

    writer = manual_sheet_status.ManualSheetStatusWriter.from_active_plan(
        active_plan={
            "source": {
                "spreadsheet_id": "spreadsheet123",
                "manual_sheet_name": "manual_entry",
            }
        },
        deployments=[],
        credentials_path=tmp_path / "credentials.json",
    )

    assert writer is None
    assert client.api_retries == 1


def test_manual_status_writebacks_are_serialized_per_sheet_row() -> None:
    class _FakeClient:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.statuses: list[str] = []
            self.guard = threading.Lock()

        def update_row_cells(self, *, row_index: int, values: dict[str, object]) -> None:
            assert row_index == 17
            with self.guard:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.02)
            self.statuses.append(str(values["bhiksha_status"]))
            with self.guard:
                self.active -= 1

    client = _FakeClient()
    writer = manual_sheet_status.ManualSheetStatusWriter(
        client=client,  # type: ignore[arg-type]
        row_index_by_deployment={"cartographer-1": 17},
    )
    deployment = SimpleNamespace(deployment_id="cartographer-1")
    event_at = datetime(2026, 8, 20, 13, 35, tzinfo=UTC)

    async def run() -> None:
        await asyncio.gather(
            writer._write_status(  # noqa: SLF001 - ordering is the contract under test
                deployment,  # type: ignore[arg-type]
                status="triggered",
                event_at=event_at,
                note="triggered",
            ),
            writer._write_status(  # noqa: SLF001 - ordering is the contract under test
                deployment,  # type: ignore[arg-type]
                status="entry_planned",
                event_at=event_at,
                note="planned",
            ),
        )

    asyncio.run(run())

    assert client.max_active == 1
    assert client.statuses == ["triggered", "entry_planned"]


def test_manual_status_writebacks_serialize_shared_client_across_rows() -> None:
    class _FakeClient:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.rows: list[int] = []
            self.guard = threading.Lock()

        def update_row_cells(self, *, row_index: int, values: dict[str, object]) -> None:
            with self.guard:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.02)
            self.rows.append(row_index)
            with self.guard:
                self.active -= 1

    client = _FakeClient()
    writer = manual_sheet_status.ManualSheetStatusWriter(
        client=client,  # type: ignore[arg-type]
        row_index_by_deployment={"cartographer-1": 17, "cartographer-2": 18},
    )
    event_at = datetime(2026, 8, 20, 13, 35, tzinfo=UTC)

    async def run() -> None:
        await asyncio.gather(
            writer._write_status(  # noqa: SLF001 - shared-client serialization contract
                SimpleNamespace(deployment_id="cartographer-1"),  # type: ignore[arg-type]
                status="triggered",
                event_at=event_at,
                note="first",
            ),
            writer._write_status(  # noqa: SLF001 - shared-client serialization contract
                SimpleNamespace(deployment_id="cartographer-2"),  # type: ignore[arg-type]
                status="triggered",
                event_at=event_at,
                note="second",
            ),
        )

    asyncio.run(run())

    assert client.max_active == 1
    assert client.rows == [17, 18]


def test_cartographer_terminal_write_self_heals_disabled_latch_and_skips_intermediate() -> None:
    class _FakeClient:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def update_row_cells(self, *, row_index: int, values: dict[str, object]) -> None:
            assert row_index == 17
            self.payloads.append(values)

    client = _FakeClient()
    writer = manual_sheet_status.ManualSheetStatusWriter(
        client=client,  # type: ignore[arg-type]
        row_index_by_deployment={"cartographer-1": 17},
    )
    deployment = SimpleNamespace(
        deployment_id="cartographer-1",
        source=SimpleNamespace(metadata={"source_owner": "market_cartographer"}),
    )
    event_at = datetime(2026, 8, 20, 13, 35, tzinfo=UTC)
    plan = SimpleNamespace(
        option_symbol="NVDA260828C00190000",
        quantity=1,
        estimated_entry_price=1.25,
        entry_timestamp=event_at,
        trade_id="trade-1",
    )

    async def run() -> None:
        assert await writer.mark_entry_planned(  # type: ignore[arg-type]
            deployment,  # type: ignore[arg-type]
            plan=plan,
            mode="shadow",
        ) is None
        assert await writer.mark_closed(
            deployment,  # type: ignore[arg-type]
            trade_id="trade-1",
            note="stop_loss",
            event_at=event_at,
        ) is None

    asyncio.run(run())

    assert client.payloads == [
        {
            "bhiksha_status": "closed",
            "bhiksha_last_event_at": event_at.isoformat(),
            "bhiksha_last_note": "stop_loss",
            "bhiksha_last_trade_id": "trade-1",
            "enabled": False,
        }
    ]


def test_cartographer_terminal_write_heals_failed_trigger_write() -> None:
    class _FakeClient:
        def __init__(self) -> None:
            self.calls = 0
            self.payloads: list[dict[str, object]] = []

        def update_row_cells(self, *, row_index: int, values: dict[str, object]) -> None:
            assert row_index == 17
            self.calls += 1
            if self.calls == 1:
                raise OSError("transient trigger write failure")
            self.payloads.append(values)

    client = _FakeClient()
    writer = manual_sheet_status.ManualSheetStatusWriter(
        client=client,  # type: ignore[arg-type]
        row_index_by_deployment={"cartographer-1": 17},
    )
    deployment = SimpleNamespace(
        deployment_id="cartographer-1",
        source=SimpleNamespace(metadata={"source_owner": "market_cartographer"}),
    )
    event_at = datetime(2026, 8, 20, 13, 35, tzinfo=UTC)

    async def run() -> str | None:
        trigger_error = await writer._write_status(  # noqa: SLF001 - failure contract
            deployment,  # type: ignore[arg-type]
            status="triggered",
            event_at=event_at,
            note="triggered",
            disable_row=True,
        )
        assert trigger_error == "transient trigger write failure"
        return await writer.mark_closed(
            deployment,  # type: ignore[arg-type]
            trade_id="trade-1",
            note="stop_loss",
            event_at=event_at,
        )

    assert asyncio.run(run()) is None
    assert client.payloads[-1]["bhiksha_status"] == "closed"
    assert client.payloads[-1]["enabled"] is False


def test_non_cartographer_intermediate_status_remains_visible() -> None:
    class _FakeClient:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def update_row_cells(self, *, row_index: int, values: dict[str, object]) -> None:
            self.payloads.append(values)

    client = _FakeClient()
    writer = manual_sheet_status.ManualSheetStatusWriter(
        client=client,  # type: ignore[arg-type]
        row_index_by_deployment={"manual-1": 12},
    )
    deployment = SimpleNamespace(
        deployment_id="manual-1",
        source=SimpleNamespace(metadata={}),
    )
    event_at = datetime(2026, 8, 20, 13, 35, tzinfo=UTC)
    plan = SimpleNamespace(
        option_symbol="AAPL260828C00200000",
        quantity=1,
        estimated_entry_price=1.0,
        entry_timestamp=event_at,
        trade_id="manual-trade",
    )

    asyncio.run(
        writer.mark_entry_planned(  # type: ignore[arg-type]
            deployment,  # type: ignore[arg-type]
            plan=plan,
            mode="shadow",
        )
    )

    assert client.payloads[0]["bhiksha_status"] == "entry_planned"
    assert "enabled" not in client.payloads[0]
