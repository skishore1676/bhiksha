"""Sync Bhiksha's active plan from the Google Sheets control plane."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
from typing import Iterator

from bhiksha.active_plan.compiler import (
    compile_active_plan_from_google_sheets,
    require_release_safe_coverage,
)
from bhiksha.config.environment import get_mala_evidence_sheet_name, get_operator_defaults_sheet_name, load_dotenv
from bhiksha.execution.pricing import (
    resolve_entry_reprice_max_chase_pct,
    resolve_initial_spread_fraction,
)
from bhiksha.evidence.bindings import DEFAULT_EVIDENCE_BINDINGS_PATH
from bhiksha.experiments.auto_shadow import resolve_mala_packet_root
from bhiksha.strategy.capabilities import CAPABILITY_MANIFEST_ENV, DEFAULT_CAPABILITY_MANIFEST_PATH
from bhiksha.tools.generate_runtime_capabilities import generate_runtime_capability_manifest


_ACTIVE_PLAN_ID_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}"
)


def normalize_active_plan_id(
    value: str | None,
    *,
    source: str,
) -> str | None:
    """Fail closed on blank or malformed explicit plan identities."""

    if value is None:
        return None
    normalized = value.strip()
    if (
        not normalized
        or normalized != value
        or _ACTIVE_PLAN_ID_PATTERN.fullmatch(normalized) is None
    ):
        raise ValueError(
            f"{source} must be a nonblank stable active-plan id using only "
            "letters, digits, '.', '_', ':', or '-'"
        )
    return normalized


def active_plan_id_from_env() -> str | None:
    if "BHIKSHA_ACTIVE_PLAN_ID" not in os.environ:
        return None
    return normalize_active_plan_id(
        os.environ.get("BHIKSHA_ACTIVE_PLAN_ID"),
        source="BHIKSHA_ACTIVE_PLAN_ID",
    )


@dataclass(slots=True, frozen=True)
class SyncActivePlanResult:
    active_plan_path: Path
    active_plan_id: str
    summary: dict[str, object]
    suppressed: list[dict[str, object]]
    changed: bool
    log_path: Path
    attempt: int
    lane_config: dict[str, dict[str, object]] = field(default_factory=dict)
    lane_config_changes: list[dict[str, object]] = field(default_factory=list)


# Per-lane fields that change trade economics; a silent change to any of these
# must show up in the sync log and stdout.
_LANE_CONFIG_FIELDS: tuple[tuple[str, str], ...] = (
    ("strategy", "key"),
    ("strategy", "version"),
    ("strategy", "params"),
    ("exit", "stop_loss_pct"),
    ("exit", "option_profit_target_pct"),
    ("exit", "profit_target_multiple"),
    ("exit", "use_profit_target"),
    ("exit", "hard_flat_time_et"),
    ("risk", "max_trade_premium_usd"),
    ("risk", "max_contracts"),
    ("execution", "shadow_only"),
    ("execution", "runtime_mode"),
    ("execution", "dte_min"),
    ("execution", "dte_max"),
    ("execution", "dte_fallback_policy"),
    ("execution", "option_mapping"),
    ("execution", "target_abs_delta_min"),
    ("execution", "target_abs_delta_max"),
    ("execution", "entry_window_start_et"),
    ("execution", "entry_window_end_et"),
    ("execution", "entry_execution_profile"),
    ("execution", "min_open_interest"),
    ("execution", "max_bid_ask_spread_pct"),
    ("execution", "entry_pricing_spread_fraction"),
    ("execution", "entry_pricing_oi_percentile_scale"),
    ("execution", "entry_reprice_enabled"),
    ("execution", "entry_reprice_checkpoints_seconds"),
    ("execution", "entry_reprice_cancel_after_seconds"),
    ("execution", "entry_reprice_spread_fractions"),
    ("execution", "entry_reprice_max_chase_pct"),
    ("exit", "risk_envelope_live_mode"),
    ("exit", "risk_envelope_live_candidate_id"),
    ("exit", "risk_envelope_live_candidate_overlay_hash"),
    ("exit", "risk_envelope_live_authorization_id"),
    ("exit", "risk_envelope_live_start_at"),
    ("exit", "risk_envelope_live_expires_at"),
    ("exit", "risk_envelope_live_authorized_deployment_id"),
    ("exit", "risk_envelope_live_authorized_symbol"),
    ("exit", "risk_envelope_live_authorized_active_plan_id"),
    ("exit", "risk_envelope_live_rollback_action"),
    ("exit", "risk_envelope_live_max_premium_cap_fraction"),
    ("exit", "risk_envelope_live_max_quote_age_ms"),
    ("exit", "risk_envelope_live_max_spread_pct"),
    ("exit", "exit_policy_id"),
    ("exit", "exit_policy_hash"),
)


def lane_config_snapshot(plan: dict[str, object]) -> dict[str, dict[str, object]]:
    """Extract the per-lane economic config from an active-plan payload."""
    lanes: dict[str, dict[str, object]] = {}
    for deployment in plan.get("deployments") or []:
        if not isinstance(deployment, dict):
            continue
        deployment_id = str(deployment.get("deployment_id") or "")
        if not deployment_id:
            continue
        lane: dict[str, object] = {"symbol": deployment.get("symbol")}
        for domain, field in _LANE_CONFIG_FIELDS:
            payload = deployment.get(domain)
            lane[field] = payload.get(field) if isinstance(payload, dict) else None
        execution = deployment.get("execution")
        if isinstance(execution, dict):
            initial_fraction, profile = resolve_initial_spread_fraction(execution)
            lane["effective_entry_pricing_spread_fraction"] = initial_fraction
            lane["effective_entry_pricing_oi_percentile_scale"] = bool(
                execution.get("entry_pricing_oi_percentile_scale") or profile is not None
            )
            lane["effective_entry_reprice_enabled"] = _first_not_none(
                execution.get("entry_reprice_enabled"),
                True if profile is not None else None,
            )
            lane["effective_entry_reprice_checkpoints_seconds"] = _first_not_none(
                execution.get("entry_reprice_checkpoints_seconds"),
                list(profile.reprice_checkpoints_seconds) if profile is not None else None,
            )
            lane["effective_entry_reprice_cancel_after_seconds"] = _first_not_none(
                execution.get("entry_reprice_cancel_after_seconds"),
                profile.cancel_after_seconds if profile is not None else None,
            )
            lane["effective_entry_reprice_spread_fractions"] = _first_not_none(
                execution.get("entry_reprice_spread_fractions"),
                list(profile.reprice_spread_fractions) if profile is not None else None,
            )
            lane["effective_entry_reprice_max_chase_pct"] = resolve_entry_reprice_max_chase_pct(execution)
        lanes[deployment_id] = lane
    return lanes


def diff_lane_configs(
    previous: dict[str, dict[str, object]],
    current: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    """Return per-lane changes between two lane-config snapshots."""
    changes: list[dict[str, object]] = []
    for deployment_id in sorted(previous.keys() | current.keys()):
        before = previous.get(deployment_id)
        after = current.get(deployment_id)
        if before is None:
            changes.append({"deployment_id": deployment_id, "change": "added", "config": after})
            continue
        if after is None:
            changes.append({"deployment_id": deployment_id, "change": "removed", "config": before})
            continue
        changed_fields = {
            field: {"before": before.get(field), "after": after.get(field)}
            for field in after
            if before.get(field) != after.get(field)
        }
        if changed_fields:
            changes.append({"deployment_id": deployment_id, "change": "modified", "fields": changed_fields})
    return changes


def _read_previous_lane_config(path: Path) -> dict[str, dict[str, object]]:
    if not path.exists():
        return {}
    try:
        return lane_config_snapshot(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return {}


def _first_not_none(*values: object) -> object | None:
    return next((value for value in values if value is not None), None)


def _require_staged_packets_for_plan(
    plan_payload: dict[str, object], staged_packet_root: Path
) -> None:
    referenced: set[str] = set()
    for deployment in plan_payload.get("deployments") or []:
        if not isinstance(deployment, dict):
            continue
        source = deployment.get("source")
        metadata = source.get("metadata") if isinstance(source, dict) else None
        if not isinstance(metadata, dict):
            continue
        for key in ("evidence_packet_id", "observation_evidence_packet_id"):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                referenced.add(value)
    retained: dict[str, int] = {}
    for manifest_path in staged_packet_root.glob("*/manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"staged evidence manifest is unreadable: {manifest_path}: {exc}"
            ) from exc
        packet_id = manifest.get("evidence_packet_id")
        if isinstance(packet_id, str):
            retained[packet_id] = retained.get(packet_id, 0) + 1
    invalid = sorted(packet_id for packet_id in referenced if retained.get(packet_id) != 1)
    if invalid:
        raise ValueError(
            "active-plan candidate references evidence packets that are not "
            "retained exactly once in the staged canonical store: "
            + ", ".join(invalid)
        )


def _restore_optional_bytes(path: Path, prior: bytes | None) -> None:
    if prior is None:
        path.unlink(missing_ok=True)
    else:
        _write_if_changed(path, prior.decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    default_google_sheet_id = os.getenv("GOOGLE_SHEET_ID")
    default_credentials_path = os.getenv("GOOGLE_API_CREDENTIALS_PATH")
    default_catalog_sheet_name = get_mala_evidence_sheet_name()
    default_defaults_sheet_name = get_operator_defaults_sheet_name()
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
    parser.add_argument(
        "--evidence-bindings",
        default=os.getenv("BHIKSHA_EVIDENCE_BINDINGS_PATH", str(DEFAULT_EVIDENCE_BINDINGS_PATH)),
        help="Immutable experiment-to-deployment binding registry",
    )
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
    args.active_plan_id = (
        normalize_active_plan_id(
            args.active_plan_id,
            source="--active-plan-id",
        )
        if args.active_plan_id is not None
        else active_plan_id_from_env()
    )

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
                evidence_bindings_path=args.evidence_bindings,
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
            for deployment_id, lane in sorted(result.lane_config.items()):
                print(
                    f"LANE_CONFIG {deployment_id} symbol={lane.get('symbol')}"
                    f" mode={'shadow' if lane.get('shadow_only') else 'live'}"
                    f" stop_loss_pct={lane.get('stop_loss_pct')}"
                    f" profit_target_pct={lane.get('option_profit_target_pct')}"
                    f" use_profit_target={lane.get('use_profit_target')}"
                    f" max_trade_premium_usd={lane.get('max_trade_premium_usd')}"
                    f" max_contracts={lane.get('max_contracts')}"
                    f" dte={lane.get('dte_min')}-{lane.get('dte_max')}"
                    f" dte_fallback={lane.get('dte_fallback_policy')}"
                    f" risk_envelope_mode={lane.get('risk_envelope_live_mode')}"
                    f" risk_envelope_auth={lane.get('risk_envelope_live_authorization_id')}"
                    f" risk_envelope_start={lane.get('risk_envelope_live_start_at')}"
                    f" risk_envelope_expires={lane.get('risk_envelope_live_expires_at')}"
                    f" risk_envelope_rollback={lane.get('risk_envelope_live_rollback_action')}"
                    f" min_open_interest={lane.get('min_open_interest')}"
                    f" max_spread_pct={lane.get('max_bid_ask_spread_pct')}"
                    f" entry_profile={lane.get('entry_execution_profile')}"
                    f" entry_fraction={lane.get('entry_pricing_spread_fraction')}"
                    f" effective_entry_fraction={lane.get('effective_entry_pricing_spread_fraction')}"
                    f" reprice_enabled={lane.get('entry_reprice_enabled')}"
                    f" reprice_cancel_seconds={lane.get('entry_reprice_cancel_after_seconds')}"
                    f" reprice_max_chase_pct={lane.get('entry_reprice_max_chase_pct')}"
                    f" effective_reprice_max_chase_pct={lane.get('effective_entry_reprice_max_chase_pct')}"
                )
            print(f"LANE_CONFIG_CHANGE_COUNT={len(result.lane_config_changes)}")
            for change in result.lane_config_changes:
                print("LANE_CONFIG_CHANGED " + json.dumps(change, sort_keys=True))
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
    evidence_bindings_path: str | Path | None = DEFAULT_EVIDENCE_BINDINGS_PATH,
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
    runtime_bindings_path = resolved_output.with_name("evidence_bindings_v1.json")
    seed_bindings_path = (
        runtime_bindings_path
        if runtime_bindings_path.exists()
        else (
            Path(evidence_bindings_path)
            if evidence_bindings_path is not None
            and Path(evidence_bindings_path).exists()
            else None
        )
    )
    canonical_packet_root = resolve_mala_packet_root()
    if canonical_packet_root is None or not canonical_packet_root.is_dir():
        raise ValueError(
            "canonical Mala evidence packet root is missing or unreadable; "
            "previous active plan and runtime registry preserved"
        )
    with tempfile.TemporaryDirectory(prefix="bhiksha-active-plan-stage-") as stage:
        stage_root = Path(stage)
        staged_bindings_path = stage_root / "evidence_bindings_v1.json"
        if seed_bindings_path is not None:
            shutil.copy2(seed_bindings_path, staged_bindings_path)
        staged_packet_root = stage_root / "evidence_packets"
        if canonical_packet_root is not None and canonical_packet_root.exists():
            shutil.copytree(canonical_packet_root, staged_packet_root)
        else:
            staged_packet_root.mkdir(parents=True)
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
                evidence_bindings_path=seed_bindings_path,
                auto_experiment_packet_root=staged_packet_root,
                auto_experiment_bindings_path=staged_bindings_path,
            )
        require_release_safe_coverage(compiled.plan.summary)
        plan_payload = compiled.plan.model_dump(mode="json")
        _require_staged_packets_for_plan(plan_payload, staged_packet_root)
        previous_lane_config = _read_previous_lane_config(resolved_output)
        lane_config = lane_config_snapshot(plan_payload)
        lane_config_changes = diff_lane_configs(previous_lane_config, lane_config)
        payload = json.dumps(plan_payload, indent=2, sort_keys=True) + "\n"

        # Only an accepted candidate may publish immutable packets or advance
        # the mutable runtime binding registry.  The tracked config remains an
        # immutable seed/fallback and is never rewritten by sync.
        prior_runtime_bindings = (
            runtime_bindings_path.read_bytes()
            if runtime_bindings_path.exists()
            else None
        )
        canonical_packet_registry = canonical_packet_root / "registry.json"
        prior_packet_registry = (
            canonical_packet_registry.read_bytes()
            if canonical_packet_registry.exists()
            else None
        )
        created_packet_dirs: list[Path] = []
        try:
            for staged_dir in sorted(staged_packet_root.iterdir()):
                if not staged_dir.is_dir():
                    continue
                destination = canonical_packet_root / staged_dir.name
                if not destination.exists():
                    created_packet_dirs.append(destination)
                    shutil.copytree(staged_dir, destination)
            staged_packet_registry = staged_packet_root / "registry.json"
            if staged_packet_registry.exists():
                _write_if_changed(
                    canonical_packet_registry,
                    staged_packet_registry.read_text(encoding="utf-8"),
                )
            if staged_bindings_path.exists():
                _write_if_changed(
                    runtime_bindings_path,
                    staged_bindings_path.read_text(encoding="utf-8"),
                )
            changed = _write_if_changed(resolved_output, payload)
        except Exception:
            _restore_optional_bytes(runtime_bindings_path, prior_runtime_bindings)
            _restore_optional_bytes(canonical_packet_registry, prior_packet_registry)
            for created_dir in reversed(created_packet_dirs):
                if created_dir.exists():
                    shutil.rmtree(created_dir)
            raise
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
        lane_config=lane_config,
        lane_config_changes=lane_config_changes,
    )
    return SyncActivePlanResult(
        active_plan_path=resolved_output,
        active_plan_id=compiled.plan.active_plan_id,
        summary=compiled.plan.summary,
        suppressed=compiled.plan.suppressed,
        changed=changed,
        log_path=log_path,
        attempt=attempt,
        lane_config=lane_config,
        lane_config_changes=lane_config_changes,
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
    lane_config: dict[str, dict[str, object]] | None = None,
    lane_config_changes: list[dict[str, object]] | None = None,
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
        "lane_config": lane_config or {},
        "lane_config_changes": lane_config_changes or [],
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
