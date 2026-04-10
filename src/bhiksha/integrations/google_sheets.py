"""Minimal Google Sheets helpers for Bhiksha control-plane integrations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any


_SHEET_ID_RE = re.compile(r"/d/(?P<sheet_id>[a-zA-Z0-9-_]+)")


def spreadsheet_id_from_url(url_or_id: str) -> str:
    match = _SHEET_ID_RE.search(url_or_id)
    if match is not None:
        return match.group("sheet_id")
    return url_or_id.strip()


@dataclass(slots=True)
class GoogleSheetTableClient:
    spreadsheet_id: str
    sheet_name: str
    credentials_path: Path
    service: Any | None = None

    def __post_init__(self) -> None:
        self.spreadsheet_id = spreadsheet_id_from_url(self.spreadsheet_id)
        self.credentials_path = Path(self.credentials_path).expanduser().resolve()
        if self.service is None:
            self.service = self._build_service()
        self.sheet_name = self._resolve_sheet_name(self.sheet_name)

    def read_rows(self, *, range_suffix: str = "A1:Z2000") -> list[dict[str, Any]]:
        values = self._read_values(range_suffix=range_suffix)
        layout = _detect_layout(values)
        if layout is None:
            return []
        rows: list[dict[str, Any]] = []
        for row_number, row in enumerate(values[layout.header_row_number :], start=layout.header_row_number + 1):
            relevant = list(row[layout.header_start_index :])
            if not any(str(value).strip() for value in relevant):
                continue
            padded = relevant + [""] * (len(layout.headers) - len(relevant))
            payload = dict(zip(layout.headers, padded, strict=False))
            payload["row_index"] = row_number
            rows.append(payload)
        return rows

    def ensure_columns(self, columns: list[str]) -> list[str]:
        values = self._read_values(range_suffix="A1:ZZ200")
        layout = _detect_layout(values)
        if layout is None:
            raise ValueError(f"Could not find a header row in Google Sheet tab {self.sheet_name!r}")
        headers = list(layout.headers)
        changed = False
        for column in columns:
            normalized = str(column).strip()
            if normalized and normalized not in headers:
                headers.append(normalized)
                changed = True
        if changed:
            start_col = _column_letters(layout.header_start_index)
            end_col = _column_letters(layout.header_start_index + len(headers) - 1)
            self._update_ranges(
                [
                    {
                        "range": self._quoted_range(f"{start_col}{layout.header_row_number}:{end_col}{layout.header_row_number}"),
                        "values": [headers],
                    }
                ]
            )
        return headers

    def update_row_cells(self, *, row_index: int, values: dict[str, Any]) -> None:
        if not values:
            return
        headers = self.ensure_columns(list(values))
        layout = self._read_layout()
        data = []
        for column_name, raw_value in values.items():
            column_index = layout.header_start_index + headers.index(column_name)
            cell_ref = f"{_column_letters(column_index)}{row_index}"
            data.append(
                {
                    "range": self._quoted_range(cell_ref),
                    "values": [[_sheet_cell_value(raw_value)]],
                }
            )
        self._update_ranges(data)

    def _build_service(self) -> Any:
        try:
            from google.oauth2.service_account import Credentials
            from googleapiclient.discovery import build
        except ModuleNotFoundError as exc:  # pragma: no cover
            raise RuntimeError(
                "Google Sheets dependencies are not installed. "
                "Install `google-api-python-client`, `google-auth`, and `google-auth-httplib2`."
            ) from exc

        credentials = Credentials.from_service_account_file(
            str(self.credentials_path),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        return build("sheets", "v4", credentials=credentials)

    def _read_layout(self) -> "_SheetLayout":
        values = self._read_values(range_suffix="A1:ZZ200")
        layout = _detect_layout(values)
        if layout is None:
            raise ValueError(f"Could not find a header row in Google Sheet tab {self.sheet_name!r}")
        return layout

    def _read_values(self, *, range_suffix: str) -> list[list[Any]]:
        result = (
            self.service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.spreadsheet_id,
                range=self._quoted_range(range_suffix),
            )
            .execute()
        )
        return result.get("values", [])

    def _update_ranges(self, data: list[dict[str, Any]]) -> None:
        (
            self.service.spreadsheets()
            .values()
            .batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={
                    "valueInputOption": "USER_ENTERED",
                    "data": data,
                },
            )
            .execute()
        )

    def _resolve_sheet_name(self, requested_name: str) -> str:
        titles = self._list_sheet_titles()
        if requested_name in titles:
            return requested_name
        requested_variants = _sheet_title_variants(requested_name)
        for title in titles:
            if _sheet_title_variants(title) & requested_variants:
                return title
        return requested_name

    def _list_sheet_titles(self) -> list[str]:
        result = (
            self.service.spreadsheets()
            .get(
                spreadsheetId=self.spreadsheet_id,
                fields="sheets.properties.title",
            )
            .execute()
        )
        return [
            str(sheet["properties"]["title"])
            for sheet in result.get("sheets", [])
            if isinstance(sheet, dict) and isinstance(sheet.get("properties"), dict) and sheet["properties"].get("title")
        ]

    def _quoted_range(self, suffix: str) -> str:
        escaped_sheet_name = self.sheet_name.replace("'", "''")
        return f"'{escaped_sheet_name}'!{suffix}"


@dataclass(slots=True, frozen=True)
class _SheetLayout:
    header_row_number: int
    header_start_index: int
    headers: list[str]


def _detect_layout(values: list[list[Any]]) -> _SheetLayout | None:
    if not values:
        return None
    for row_number, row in enumerate(values, start=1):
        non_empty_indexes = [index for index, value in enumerate(row) if str(value).strip()]
        if len(non_empty_indexes) < 2:
            continue
        header_start_index = non_empty_indexes[0]
        headers = [str(header).strip() for header in row[header_start_index:] if str(header).strip()]
        if headers:
            return _SheetLayout(
                header_row_number=row_number,
                header_start_index=header_start_index,
                headers=headers,
            )
    return None


def _column_letters(index: int) -> str:
    if index < 0:
        raise ValueError("Column index must be >= 0")
    letters = []
    current = index
    while True:
        current, remainder = divmod(current, 26)
        letters.append(chr(ord("A") + remainder))
        if current == 0:
            break
        current -= 1
    return "".join(reversed(letters))


def _sheet_cell_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return value


def _sheet_title_variants(value: str) -> set[str]:
    normalized = _normalize_sheet_title(value)
    variants = {normalized}
    if normalized.endswith("ies"):
        variants.add(normalized[:-3] + "y")
    if normalized.endswith("y"):
        variants.add(normalized[:-1] + "ies")
    if normalized.endswith("s"):
        variants.add(normalized[:-1])
    else:
        variants.add(normalized + "s")
    return {variant for variant in variants if variant}


def _normalize_sheet_title(value: str) -> str:
    slug = []
    for char in value.strip().lower():
        slug.append(char if char.isalnum() else "_")
    normalized = "".join(slug).strip("_")
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized


__all__ = ["GoogleSheetTableClient", "spreadsheet_id_from_url"]
