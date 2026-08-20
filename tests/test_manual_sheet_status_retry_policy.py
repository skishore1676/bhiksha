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
