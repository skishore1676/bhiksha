"""Token storage for Schwab OAuth credentials."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_tokens(token_file: str | Path) -> dict[str, Any] | None:
    path = Path(token_file)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_tokens(token_file: str | Path, payload: dict[str, Any]) -> None:
    path = Path(token_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

