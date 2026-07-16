"""Cross-process exclusion for runtime starts and stopped-only admin work."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
from pathlib import Path
from typing import Iterator


def control_lock_path(pid_path: Path) -> Path:
    resolved = pid_path.resolve()
    return resolved.with_name(f"{resolved.stem}.control.lock")


@contextmanager
def runtime_control_lock(pid_path: Path) -> Iterator[Path]:
    """Serialize runtime startup with operations that require it stopped."""
    lock_path = control_lock_path(pid_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield lock_path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
