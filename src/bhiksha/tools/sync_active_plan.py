"""Sync Bhiksha's active plan from the Google Sheets control plane."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Iterator

from bhiksha.active_plan.compiler import compile_active_plan_from_google_sheets
from bhiksha.config.environment import get_mala_evidence_sheet_name, load_dotenv
from bhiksha.strategy.capabilities import CAPABILITY_MANIFEST_ENV, DEFAULT_CAPABILITY_MANIFEST_PATH
from bhiksha.tools.generate_runtime_capabilities import generate_runtime_capability_manifest


@dataclass(slots=True, frozen=True)
class SyncActivePlanResult:
    active_plan_path: Path
    active_plan_id: str
    summary: dict[str, object]
    suppressed: list[dict[str, object]]
    changed: bool
    log_path: Path
    attempt: int


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    default_google_sheet_id = os.getenv("GOOGLE_SHEET_ID")
    default_credentials_path = os.getenv("GOOGLE_API_CREDENTIALS_PATH")
    default_catalog_sheet_name = get_mala_evidence_sheet_name()
    default_defaults_sheet_name = os.getenv("OPERATOR_DEFAULTS_SHEET_NAME")
    default_strategy_sheet_name = os.getenv("ACTIVE_STRATEGIES_SHEET_NAME", "active_strategies")
    default_manual_sheet_name = os.getenv("MANUAL_ENTRY_SHEET_NAME") or os.getenv("MANNUAL_ENTRY_SHEET_NAME") or "manual_entry"
    default_strategy_catalog_path = os.getenv("BHIKSHA_STRATEGY_CATALOG_PATH", "config/strategy_catalog")
    default_output_path = os.getenv("BHIKSHA_ACTIVE_PLAN_PATH", "artifacts/playbook/active_plan.json")
    default_log_dir = os.getenv("BHIKSHA_ACTIVE_PLAN_LOG_DIR", "artifacts/playbook/logs")
    default_runtime_capabilities_path = os.getenv(
        "BHIKSHA_RUNTIME_CAPABILITIES_PATH",
        "artifacts/capabilities/bhiksha_runtime_capabilities_v2.json",
    )
    default_source_name = os.getenv("BHIKSHA_ACTIVE_PLAN_SOURCE_NAME", "google_sheet_integration")
    default_interval_minutes = _env_float("BHIKSHA_ACTIVE_PLAN_SYNC_MINUTES")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--google-sheet-id", default=default_google_sheet_id, help="Google spreadsheet URL or ID")
    parser.add_argument("--credentials-path", default=default_credentials_path, help="Google service-account credentials JSON path")
    parser.add_argument("--catalog-sheet-name", default=default_catalog_sheet_name, help="Worksheet name for Mala_Evidence_v1")
    parser.add_argument("--defaults-sheet-name", default=default_defaults_sheet_name, help="Optional worksheet name for operator defaults")
    parser.add_argument("--strategy-sheet-name", default=default_strategy_sheet_name, help="Worksheet name for active strategies")
    parser.add_argument("--manual-sheet-name", default=default_manual_sheet_name, help="Worksheet name for manual entries")
    parser.add_argument("--strategy-catalog", default=default_strategy_catalog_path, help="Local Bhiksha strategy catalog path")
    parser.add_argument("--out", default=default_output_path, help="Where to write the active plan JSON")
    parser.add_argument("--log-dir", default=default_log_dir, help="Directory for dated active-plan sync logs")
    parser.add_argument(
        "--runtime-capabilities",
        default=default_runtime_capabilities_path,
        help="Where to write the verified Bhiksha runtime capability manifest before compiling",
    )
    parser.add_argument(
        "--skip-runtime-capability-refresh",
        action="store_true",
        help="Compile using the existing capability manifest instead of generating a fresh verified runtime snapshot",
    )
    parser.add_argument("--source-name", default=default_source_name, help="Recorded source.name for this plan")
    parser.add_argument("--active-plan-id", default=None, help="Optional explicit active_plan_id")
    parser.add_argument("--trading-date", default=None, help="Optional trading date in YYYY-MM-DD format")
    parser.add_argument("--interval-minutes", type=float, default=default_interval_minutes, help="When set, keep syncing on this interval")
    parser.add_argument("--iterations", type=int, default=1, help="Number of sync attempts to run; ignored unless interval is set")
    args = parser.parse_args(argv)

    if not args.google_sheet_id:
        parser.error("--google-sheet-id or GOOGLE_SHEET_ID is required")
    if not args.credentials_path:
        parser.error("--credentials-path or GOOGLE_API_CREDENTIALS_PATH is required")

    interval_minutes = args.interval_minutes
    if interval_minutes is not None and interval_minutes <= 0:
        parser.error("--interval-minutes must be > 0")
    iterations = max(args.iterations, 1)
    if interval_minutes is None:
        iterations = 1

    output_path = Path(args.out).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    log_dir = Path(args.log_dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, iterations + 1):
        try:
            result = sync_active_plan_once(
                spreadsheet_id=args.google_sheet_id,
                credentials_path=args.credentials_path,
                catalog_sheet_name=args.catalog_sheet_name,
                defaults_sheet_name=args.defaults_sheet_name,
                strategy_sheet_name=args.strategy_sheet_name,
                manual_sheet_name=args.manual_sheet_name,
                strategy_catalog_path=args.strategy_catalog,
                output_path=output_path,
                log_dir=log_dir,
                runtime_capabilities_path=None if args.skip_runtime_capability_refresh else args.runtime_capabilities,
                active_plan_id=args.active_plan_id,
                trading_date=args.trading_date,
                source_name=args.source_name,
                attempt=attempt,
            )
            print(f"ACTIVE_PLAN={result.active_plan_path}")
            print(f"ACTIVE_PLAN_ID={result.active_plan_id}")
            print("SUMMARY=" + json.dumps(result.summary, sort_keys=True))
            print(f"SUPPRESSED_COUNT={len(result.suppressed)}")
            print(f"ACTIVE_PLAN_UPDATED={'1' if result.changed else '0'}")
            print(f"SYNC_LOG={result.log_path}")
            print(f"SYNC_ATTEMPT={attempt}")
        except Exception as exc:
            log_path = _append_sync_log(
                log_dir=log_dir,
                started_at=datetime.now(UTC),
                status="error",
                attempt=attempt,
                output_path=output_path,
                changed=False,
                summary=None,
                suppressed=[],
                error=str(exc),
            )
            print(f"SYNC_LOG={log_path}")
            if interval_minutes is None or attempt >= iterations:
                raise
        if interval_minutes is None or attempt >= iterations:
            break
        time.sleep(interval_minutes * 60)
    return 0


def sync_active_plan_once(
    *,
    spreadsheet_id: str,
    credentials_path: str | Path,
    catalog_sheet_name: str,
    defaults_sheet_name: str | None,
    strategy_sheet_name: str,
    manual_sheet_name: str,
    strategy_catalog_path: str | Path,
    output_path: str | Path,
    log_dir: str | Path,
    runtime_capabilities_path: str | Path | None = None,
    active_plan_id: str | None = None,
    trading_date: str | None = None,
    source_name: str = "google_sheet_integration",
    attempt: int = 1,
) -> SyncActivePlanResult:
    resolved_output = Path(output_path).resolve()
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    resolved_log_dir = Path(log_dir).resolve()
    resolved_log_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(UTC)
    if runtime_capabilities_path is not None:
        capability_path = Path(runtime_capabilities_path).expanduser().resolve()
        generate_runtime_capability_manifest(
            declared_manifest_path=DEFAULT_CAPABILITY_MANIFEST_PATH,
            output_path=capability_path,
        )
    else:
        capability_path = None
    with _capability_manifest_override(capability_path):
        compiled = compile_active_plan_from_google_sheets(
            spreadsheet_id=spreadsheet_id,
            credentials_path=credentials_path,
            catalog_sheet_name=catalog_sheet_name,
            defaults_sheet_name=defaults_sheet_name,
            strategy_sheet_name=strategy_sheet_name,
            manual_sheet_name=manual_sheet_name,
            strategy_catalog_path=strategy_catalog_path,
            active_plan_id=active_plan_id,
            trading_date=trading_date,
            source_name=source_name,
        )
    payload = json.dumps(compiled.plan.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    changed = _write_if_changed(resolved_output, payload)
    log_path = _append_sync_log(
        log_dir=resolved_log_dir,
        started_at=started_at,
        status="ok",
        attempt=attempt,
        output_path=resolved_output,
        changed=changed,
        summary=compiled.plan.summary,
        suppressed=compiled.plan.suppressed,
        error=None,
    )
    return SyncActivePlanResult(
        active_plan_path=resolved_output,
        active_plan_id=compiled.plan.active_plan_id,
        summary=compiled.plan.summary,
        suppressed=compiled.plan.suppressed,
        changed=changed,
        log_path=log_path,
        attempt=attempt,
    )


def _write_if_changed(path: Path, payload: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == payload:
        return False
    _atomic_write_text(path, payload)
    return True


def _atomic_write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _env_float(name: str) -> float | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    return float(raw)


def _append_sync_log(
    *,
    log_dir: Path,
    started_at: datetime,
    status: str,
    attempt: int,
    output_path: Path,
    changed: bool,
    summary: dict | None,
    suppressed: list[dict],
    error: str | None,
) -> Path:
    log_path = log_dir / f"active_plan_sync_{started_at.date().isoformat()}.jsonl"
    entry = {
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "status": status,
        "attempt": attempt,
        "active_plan_path": str(output_path),
        "changed": changed,
        "summary": summary or {},
        "suppressed": suppressed,
        "error": error,
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return log_path


@contextmanager
def _capability_manifest_override(path: Path | None) -> Iterator[None]:
    if path is None:
        yield
        return
    previous = os.environ.get(CAPABILITY_MANIFEST_ENV)
    os.environ[CAPABILITY_MANIFEST_ENV] = str(path)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(CAPABILITY_MANIFEST_ENV, None)
        else:
            os.environ[CAPABILITY_MANIFEST_ENV] = previous


if __name__ == "__main__":
    raise SystemExit(main())
