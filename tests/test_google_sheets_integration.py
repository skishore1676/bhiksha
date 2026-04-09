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
    assert captured["range"] == "'strategy catalog'!A1:Z2000"
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
    assert service.spreadsheets_api.values_api.range == "'active_strategy'!A1:Z2000"
