from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from mala_bhiksha_kernel import canonical_sha256

from bhiksha.ops.chart_scenario_sheet import (
    HEADERS,
    KEY_COLUMNS,
    RECEIPT_SCHEMA,
    REQUEST_SCHEMA,
    SHEET_NAME,
    SPREADSHEET_ID,
    project_sheet_upsert_request,
)
from bhiksha.tools.chart_scenario_sheet_project import _credentials_path
from tests.test_chart_scenarios import _plan


def _request(row: list[object]) -> dict:
    body = {
        "schema": REQUEST_SCHEMA,
        "spreadsheet_id": SPREADSHEET_ID,
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
    scenario = _plan().scenarios[0]
    values = {header: f"fixture-{header}" for header in HEADERS}
    values.update(
        {
            "arm": scenario.arm_id.value,
            "rank": 1,
            "symbol": scenario.symbol,
            "direction": scenario.direction.value,
            "exit_profile": scenario.exit_profile.value,
            "program_id": scenario.program_id,
            "experiment_family_id": scenario.experiment_family_id,
            "experiment_version": scenario.experiment_version,
            "campaign_id": scenario.campaign_id,
            "run_id": scenario.run_id,
            "scenario_id": scenario.scenario_id,
            "candidate_id": scenario.candidate_id,
            "component_manifest_hash": scenario.component_manifest_hash,
            "candidate_pool_hash": scenario.candidate_pool_hash,
            "scenario_hash": scenario.scenario_hash,
            "exit_policy_hash": scenario.exit_policy_hash,
            "chart_evidence_hash": ",".join(
                item.evidence_hash for item in scenario.chart_evidence_refs
            ),
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
    spreadsheet_id = SPREADSHEET_ID
    sheet_name = SHEET_NAME

    def __init__(self) -> None:
        self.update_calls = 0
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
        self.update_calls += 1
        assert headers == list(HEADERS)
        for row_index, values in rows:
            payload = dict(zip(HEADERS, values, strict=True))
            payload["row_index"] = row_index
            self.rows = [row for row in self.rows if int(row["row_index"]) != row_index]
            self.rows.append(payload)


class _FormattedSheetClient(_SheetClient):
    def update_exact_rows(self, *, headers, rows):
        super().update_exact_rows(headers=headers, rows=rows)
        for row in self.rows:
            for key, value in tuple(row.items()):
                if key == "row_index":
                    continue
                if isinstance(value, bool):
                    row[key] = "TRUE" if value else "FALSE"
                elif isinstance(value, (int, float)):
                    row[key] = str(value)


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
        plan=_plan(),
    )
    second = project_sheet_upsert_request(
        request,
        client=client,  # type: ignore[arg-type]
        receipt_path=receipt_path,
        plan=_plan(),
    )

    assert first["inserted_count"] == 1
    assert first["updated_count"] == 0
    assert second["inserted_count"] == 0
    assert second["updated_count"] == 1
    assert second["exact_reread"] is True
    assert second["effects"]["sheet_tab"] == SHEET_NAME
    assert receipt_path.is_file()


def test_sheet_projection_accepts_google_formatted_numeric_and_boolean_reread(
    tmp_path: Path,
) -> None:
    client = _FormattedSheetClient()
    receipt = project_sheet_upsert_request(
        _request(_row()),
        client=client,  # type: ignore[arg-type]
        receipt_path=(
            tmp_path
            / "artifacts"
            / "chart_scenarios"
            / "formatted-projection.receipt.json"
        ),
        plan=_plan(),
    )

    assert receipt["status"] == "succeeded"
    assert receipt["exact_reread"] is True


def test_sheet_projector_uses_existing_canonical_credentials_fallback(
    monkeypatch,
) -> None:
    monkeypatch.delenv("BHIKSHA_GOOGLE_SHEETS_CREDENTIALS_PATH", raising=False)
    monkeypatch.setenv("GOOGLE_API_CREDENTIALS_PATH", "/secure/google.json")
    assert _credentials_path() == "/secure/google.json"

    monkeypatch.setenv(
        "BHIKSHA_GOOGLE_SHEETS_CREDENTIALS_PATH", "/secure/chart-only.json"
    )
    assert _credentials_path() == "/secure/chart-only.json"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("spreadsheet_id", "attacker-sheet"),
        ("arm", "live_execution"),
        ("authorization_mode", "live"),
        ("source_type", "manual"),
        ("scenario_id", "unknown-scenario"),
        ("scenario_hash", "f" * 64),
    ],
)
def test_sheet_projection_rejects_uninstalled_or_effectful_rows_before_write(
    tmp_path: Path, field: str, value: str
) -> None:
    client = _SheetClient()
    request = deepcopy(_request(_row()))
    if field == "spreadsheet_id":
        request[field] = value
    else:
        request["values"][1][HEADERS.index(field)] = value
    request["content_hash"] = canonical_sha256(
        {key: item for key, item in request.items() if key != "content_hash"}
    )

    with pytest.raises(ValueError):
        project_sheet_upsert_request(
            request,
            client=client,  # type: ignore[arg-type]
            receipt_path=tmp_path / "artifacts/chart_scenarios/rejected.json",
            plan=_plan(),
        )
    assert client.update_calls == 0
