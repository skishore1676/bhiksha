"""Token storage for Schwab OAuth credentials."""

from __future__ import annotations

import json
import os
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
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    except OSError as exc:
        raise SchwabTokenStoreError(f"Unable to write Schwab token file {path}: {exc}") from exc
    finally:
        if temp_path.exists():
            temp_path.unlink()
