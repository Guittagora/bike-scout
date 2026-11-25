"""Google Sheets output.

``SheetsClient`` talks to the real Google Sheets API through gspread;
``MemorySheet`` implements the same interface in memory and is used for
``--dry-run`` and in tests.
"""

from __future__ import annotations

import logging
import re
from typing import Protocol

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]
_UPDATED_ROW_RE = re.compile(r"![A-Z]+(\d+)")


class Sheet(Protocol):
    def ensure_headers(self, headers: list[str]) -> list[list[str]]:
        """Make row 1 equal to ``headers``; return the existing data rows (row 2 onward)."""

    def update_row(self, row_idx: int, values: list[object]) -> None: ...

    def update_cell(self, row_idx: int, col_idx: int, value: object) -> None: ...

    def append_row(self, values: list[object]) -> int:
        """Append a row and return its 1-based index."""


def column_letter(n: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA."""
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def row_from_append_response(response: dict) -> int | None:
    """Extract the written row index from a Sheets ``values.append`` response."""
    updated = (response or {}).get("updates", {}).get("updatedRange", "")
    match = _UPDATED_ROW_RE.search(updated)
    return int(match.group(1)) if match else None


class SheetsClient:
    def __init__(self, sheet_name: str, credentials_path: str) -> None:
        import gspread  # imported lazily so dry-runs/tests don't need Google libs configured
        from google.oauth2.service_account import Credentials

        creds = Credentials.from_service_account_file(credentials_path, scopes=SCOPES)
        self.ws = gspread.authorize(creds).open(sheet_name).sheet1
        self.width = 0
        log.info("Connected to Google Sheet '%s'", sheet_name)

    def ensure_headers(self, headers: list[str]) -> list[list[str]]:
        self.width = len(headers)
        values = self.ws.get_all_values()
        if not values:
            self.ws.append_row(headers, value_input_option="USER_ENTERED")
            log.info("Initialised empty sheet with headers")
            return []
        if values[0][: len(headers)] != headers:
            log.warning("Sheet headers differ from expected layout; rewriting header row")
            self.ws.update(range_name=f"A1:{column_letter(len(headers))}1", values=[headers])
        return values[1:]

    def update_row(self, row_idx: int, values: list[object]) -> None:
        end = column_letter(max(self.width, len(values)))
        self.ws.update(range_name=f"A{row_idx}:{end}{row_idx}", values=[values], value_input_option="USER_ENTERED")

    def update_cell(self, row_idx: int, col_idx: int, value: object) -> None:
        self.ws.update_cell(row_idx, col_idx, value)

    def append_row(self, values: list[object]) -> int:
        response = self.ws.append_row(values, value_input_option="USER_ENTERED")
        row = row_from_append_response(response)
        return row if row is not None else len(self.ws.get_all_values())


class MemorySheet:
    """In-memory stand-in for a worksheet (dry runs and tests)."""

    def __init__(self, rows: list[list[str]] | None = None) -> None:
        self.rows: list[list[object]] = [list(r) for r in rows or []]

    def ensure_headers(self, headers: list[str]) -> list[list[str]]:
        if not self.rows:
            self.rows.append(list(headers))
            return []
        self.rows[0] = list(headers)
        return [list(map(str, r)) for r in self.rows[1:]]

    def update_row(self, row_idx: int, values: list[object]) -> None:
        self.rows[row_idx - 1] = list(values)

    def update_cell(self, row_idx: int, col_idx: int, value: object) -> None:
        row = self.rows[row_idx - 1]
        row.extend([""] * (col_idx - len(row)))
        row[col_idx - 1] = value

    def append_row(self, values: list[object]) -> int:
        self.rows.append(list(values))
        return len(self.rows)
