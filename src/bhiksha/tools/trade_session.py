"""Friendly entrypoint for the continuous trading session loop."""

from __future__ import annotations

from bhiksha.tools.dry_run_live_loop import main


if __name__ == "__main__":
    raise SystemExit(main())
