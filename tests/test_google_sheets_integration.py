from __future__ import annotations

from pathlib import Path

from bhiksha.integrations.google_sheets import GoogleSheetTableClient, spreadsheet_id_from_url


def test_spreadsheet_id_from_url_extracts_id() -> None:
    assert (
        spreadsheet_id_from_url("https://docs.google.com/spreadsheets/d/abc123XYZ987/edit#gid=0")
        == "abc123XYZ987"
    )


def test_google_sheet_client_quotes_sheet_names_in_ranges(tmp_path: Path) -> None:
    captured: dict[str, str] = {}

    class _Values:
        def get(self, *, spreadsheetId: str, range: str):
            captured["spreadsheetId"] = spreadsheetId
            captured["range"] = range
            return self

        def execute(self):
            return {
                "values": [
                    ["enabled", "strategy"],
                    ["TRUE", "market_impulse_spy_short_v1"],
                ]
            }

    class _Spreadsheets:
        def get(self, *, spreadsheetId: str, fields: str):
            captured["metadata_spreadsheetId"] = spreadsheetId
            captured["metadata_fields"] = fields
            return self

        def execute(self):
            return {"sheets": [{"properties": {"title": "strategy catalog"}}]}

        def values(self):
            return _Values()

    class _Service:
        def spreadsheets(self):
            return _Spreadsheets()

    client = GoogleSheetTableClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="strategy catalog",
        credentials_path=tmp_path / "credentials.json",
        service=_Service(),
    )

    rows = client.read_rows()

    assert captured["spreadsheetId"] == "spreadsheet123"
    assert captured["metadata_spreadsheetId"] == "spreadsheet123"
    assert captured["range"] == "'strategy catalog'!A1:ZZ2000"
    assert rows[0]["row_index"] == 2
    assert rows[0]["strategy"] == "market_impulse_spy_short_v1"


def test_google_sheet_client_resolves_nearby_sheet_names(tmp_path: Path) -> None:
    class _Values:
        def get(self, *, spreadsheetId: str, range: str):
            self.range = range
            return self

        def execute(self):
            return {"values": [["enabled"], ["TRUE"]]}

    class _Spreadsheets:
        def __init__(self):
            self.values_api = _Values()

        def get(self, *, spreadsheetId: str, fields: str):
            return self

        def execute(self):
            return {"sheets": [{"properties": {"title": "active_strategy"}}]}

        def values(self):
            return self.values_api

    class _Service:
        def __init__(self):
            self.spreadsheets_api = _Spreadsheets()

        def spreadsheets(self):
            return self.spreadsheets_api

    service = _Service()
    client = GoogleSheetTableClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="active_strategies",
        credentials_path=tmp_path / "credentials.json",
        service=service,
    )

    client.read_rows()

    assert client.sheet_name == "active_strategy"
    assert service.spreadsheets_api.values_api.range == "'active_strategy'!A1:ZZ2000"


def test_google_sheet_client_updates_row_cells_and_extends_headers(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class _Values:
        def get(self, *, spreadsheetId: str, range: str):
            captured.setdefault("gets", []).append((spreadsheetId, range))
            return self

        def batchUpdate(self, *, spreadsheetId: str, body: dict):
            captured.setdefault("batch_updates", []).append((spreadsheetId, body))
            return self

        def execute(self):
            gets = captured.get("gets", [])
            if len(gets) == 0:
                return {"sheets": [{"properties": {"title": "manual_entry"}}]}
            if len(gets) == 1:
                return {
                    "values": [
                        ["", "enabled", "notes"],
                        ["", "TRUE", "test row"],
                    ]
                }
            return {
                "values": [
                    ["", "enabled", "notes", "bhiksha_status", "bhiksha_last_note"],
                    ["", "TRUE", "test row", "", ""],
                ]
            }

    class _Spreadsheets:
        def get(self, *, spreadsheetId: str, fields: str):
            captured["metadata"] = (spreadsheetId, fields)
            return self

        def execute(self):
            return {"sheets": [{"properties": {"title": "manual_entry"}}]}

        def values(self):
            return _Values()

    class _Service:
        def spreadsheets(self):
            return _Spreadsheets()

    client = GoogleSheetTableClient(
        spreadsheet_id="spreadsheet123",
        sheet_name="manual_entry",
        credentials_path=tmp_path / "credentials.json",
        service=_Service(),
    )

    client.update_row_cells(
        row_index=2,
        values={
            "bhiksha_status": "triggered",
            "bhiksha_last_note": "trigger met",
            "enabled": False,
        },
    )

    updates = captured["batch_updates"]
    assert [item[0] for item in updates] == ["spreadsheet123", "spreadsheet123"]
    header_ranges = [item["range"] for item in updates[0][1]["data"]]
    cell_ranges = [item["range"] for item in updates[1][1]["data"]]
    assert updates[0][1]["valueInputOption"] == "USER_ENTERED"
    assert updates[1][1]["valueInputOption"] == "USER_ENTERED"
    assert "'manual_entry'!B1:E1" in header_ranges
    assert "'manual_entry'!D2" in cell_ranges
    assert "'manual_entry'!E2" in cell_ranges
    assert "'manual_entry'!B2" in cell_ranges
