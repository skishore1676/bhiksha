"""Token storage for Schwab OAuth credentials."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class SchwabTokenStoreError(RuntimeError):
    """Raised when the Schwab token file exists but cannot be read or parsed."""


def read_tokens(token_file: str | Path) -> dict[str, Any] | None:
    path = Path(token_file)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SchwabTokenStoreError(f"Unable to read Schwab token file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SchwabTokenStoreError(f"Invalid JSON in Schwab token file {path}: {exc}") from exc


def write_tokens(token_file: str | Path, payload: dict[str, Any]) -> None:
    path = Path(token_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
