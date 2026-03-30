"""Persistent token storage for Public broker auth."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_session(session_file: str | Path) -> dict[str, Any] | None:
    path = Path(session_file)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_session(session_file: str | Path, data: dict[str, Any]) -> None:
    path = Path(session_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")

