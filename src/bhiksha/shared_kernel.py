"""Import helper for the Mala/Bhiksha shared kernel during the refactor."""

from __future__ import annotations

import sys
from pathlib import Path


def ensure_kernel_on_path() -> None:
    try:
        import mala_bhiksha_kernel  # noqa: F401

        return
    except ModuleNotFoundError:
        pass

    repo_root = Path(__file__).resolve().parents[2]
    sibling_src = repo_root.parent / "mala-bhiksha-kernel" / "src"
    if sibling_src.exists():
        sys.path.insert(0, str(sibling_src))
