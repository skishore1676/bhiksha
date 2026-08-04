from __future__ import annotations

from pathlib import Path

from mala_bhiksha_kernel import canonical_sha256

from bhiksha.ops.chart_scenario_sheet import (
    HEADERS,
    KEY_COLUMNS,
    RECEIPT_SCHEMA,
    REQUEST_SCHEMA,
    SHEET_NAME,
    project_sheet_upsert_request,
)


def _request(row: list[object]) -> dict:
    body = {
        "schema": REQUEST_SCHEMA,
        "spreadsheet_id": "sheet-1",
        "sheet_name": SHEET_NAME,
        "key_columns": list(KEY_COLUMNS),
        "header_hash": canonical_sha256(list(HEADERS)),
        "values": [list(HEADERS), row],
        "expected_reread": {
            "header": f"{SHEET_NAME}!A1:AK1",
            "row_count": 1,
            "receipt_schema": RECEIPT_SCHEMA,
            "require_exact_values": True,
        },
        "effects": {"broker": False, "orders": False, "authorization": False},
    }
    return {**body, "content_hash": canonical_sha256(body)}


def _row() -> list[object]:
    values = {header: f"fixture-{header}" for header in HEADERS}
    values.update(
        {
            "arm": "chart_deterministic",
            "rank": 1,
            "campaign_id": "campaign-1",
            "run_id": "run-1",
            "scenario_id": "scenario-1",
            "net_r": None,
            "triggered_at": None,
            "terminal_at": None,
            "option_contract": None,
            "terminal_reason": None,
            "comparable": False,
            "quarantine_reason": None,
            "authorization_mode": "shadow",
            "source_type": "chart_scenario_experiment",
        }
    )
    return [values[header] for header in HEADERS]


class _SheetClient:
    spreadsheet_id = "sheet-1"
    sheet_name = SHEET_NAME

    def __init__(self) -> None:
        self.rows = [
            {
                "row_index": 2,
                "campaign_id": "",
                "run_id": "",
                "arm": "",
                "scenario_id": "",
                "comparable": False,
            }
        ]

    def read_headers(self):
        return list(HEADERS)

    def read_rows(self):
        return [dict(row) for row in self.rows]

    def update_exact_rows(self, *, headers, rows):
        assert headers == list(HEADERS)
        for row_index, values in rows:
            payload = dict(zip(HEADERS, values, strict=True))
            payload["row_index"] = row_index
            self.rows = [row for row in self.rows if int(row["row_index"]) != row_index]
            self.rows.append(payload)


def test_sheet_projection_uses_key_occupancy_and_exact_reread(tmp_path: Path) -> None:
    client = _SheetClient()
    request = _request(_row())
    receipt_path = (
        tmp_path / "artifacts" / "chart_scenarios" / "projection.receipt.json"
    )

    first = project_sheet_upsert_request(
        request,
        client=client,  # type: ignore[arg-type]
        receipt_path=receipt_path,
    )
    second = project_sheet_upsert_request(
        request,
        client=client,  # type: ignore[arg-type]
        receipt_path=receipt_path,
    )

    assert first["inserted_count"] == 1
    assert first["updated_count"] == 0
    assert second["inserted_count"] == 0
    assert second["updated_count"] == 1
    assert second["exact_reread"] is True
    assert second["effects"]["sheet_tab"] == SHEET_NAME
    assert receipt_path.is_file()
