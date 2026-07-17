from __future__ import annotations

from bhiksha.integrations import manual_sheet_status


def test_manual_status_writebacks_disable_retries_after_startup_metadata(
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
    assert client.api_retries == 0
