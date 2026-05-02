"""Environment loading helpers."""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    """Load simple KEY=VALUE pairs into os.environ.

    Existing environment variables win over the file so local shell overrides
    still work.
    """
    file_path = Path(path)
    if not file_path.exists():
        return

    for raw_line in file_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def get_mala_evidence_sheet_name(default: str = "strategy catalog") -> str:
    """Return the Mala-owned evidence worksheet name.

    ``STRATEGY_CATALOG_SHEET_NAME`` is kept as a backward-compatible alias for
    older deployments.
    """
    return (
        os.getenv("MALA_EVIDENCE_SHEET_NAME")
        or os.getenv("STRATEGY_CATALOG_SHEET_NAME")
        or default
    )
