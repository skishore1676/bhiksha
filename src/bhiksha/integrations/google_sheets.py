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
        escaped_sheet_name = self.sheet_name.replace("'", "''")
        result = (
            self.service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.spreadsheet_id,
                range=f"'{escaped_sheet_name}'!{range_suffix}",
            )
            .execute()
        )
        values = result.get("values", [])
        if not values:
            return []
        header_row_number = 0
        header_start_index = 0
        headers: list[str] = []
        for row_number, row in enumerate(values, start=1):
            non_empty_indexes = [index for index, value in enumerate(row) if str(value).strip()]
            if len(non_empty_indexes) < 2:
                continue
            header_row_number = row_number
            header_start_index = non_empty_indexes[0]
            headers = [str(header).strip() for header in row[header_start_index:] if str(header).strip()]
            break
        if not headers:
            return []
        rows: list[dict[str, Any]] = []
        for row_number, row in enumerate(values[header_row_number:], start=header_row_number + 1):
            relevant = list(row[header_start_index:])
            if not any(str(value).strip() for value in relevant):
                continue
            padded = relevant + [""] * (len(headers) - len(relevant))
            payload = dict(zip(headers, padded, strict=False))
            payload["row_index"] = row_number
            rows.append(payload)
        return rows

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
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
        )
        return build("sheets", "v4", credentials=credentials)

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
