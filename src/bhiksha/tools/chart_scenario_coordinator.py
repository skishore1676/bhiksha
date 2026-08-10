"""Bhiksha-owned lifecycle coordinator for the chart-scenario experiment."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mala_bhiksha_kernel import ScenarioShadowEvent, canonical_sha256

from bhiksha.chart_scenarios.cycle import (
    CYCLE_RECEIPT_SCHEMA,
    validate_cycle_input,
)
from bhiksha.chart_scenarios.paths import require_experiment_path, run_artifact_paths
from bhiksha.chart_scenarios.repository import (
    ScenarioEventRepository,
    canonical_observation_slot_id,
)
from bhiksha.chart_scenarios.timeframes import CALENDAR_VERSION, xnys_session_dates
from bhiksha.chart_scenarios.validation import (
    INSTALL_RECEIPT_SCHEMA_VERSION,
    ShadowPlan,
    install_shadow_plan,
    read_installed_plan,
    validate_bundle,
)
from bhiksha.config.environment import load_dotenv
from bhiksha.ops.chart_scenario_sheet import SPREADSHEET_ID
from bhiksha.tools.chart_kernel_runtime import verify_kernel_runtime_from_env

SCHEMA = "bhiksha.chart-scenario-coordinator-contract.v1"
CAMPAIGN_CONFIG_SCHEMA = "bhiksha.chart-scenario-campaign-config.v1"
RECEIPT_SCHEMA = "bhiksha.chart-scenario-coordinator-receipt.v1"
CAMPAIGN_WINDOW_SCHEMA = "bhiksha.chart-scenario-campaign-window.v1"
CENTRAL = ZoneInfo("America/Chicago")
PREPARE_CUTOFF_MINUTES = 8 * 60 + 15
_CYCLE_RECEIPT_RE = re.compile(r"slot-(\d{4})\.receipt\.json")
_COMMON_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "PYTHONPATH",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "TMPDIR",
    }
)
_SCHWAB_MARKET_DATA_ENV = frozenset(
    {"SCHWAB_TOKEN_FILE", "SCHWAB_API_BASE_URL", "SCHWAB_TIMEOUT_SECONDS"}
)
_GOOGLE_SHEET_ENV = frozenset(
    {"BHIKSHA_GOOGLE_SHEETS_CREDENTIALS_PATH", "GOOGLE_API_CREDENTIALS_PATH"}
)
_RUNTIME_RECORD_SCHEMA = "bhiksha.chart-scenario-tool-runtime.v1"
_RUNTIME_RECORD_FIELDS = {
    "schema",
    "role",
    "checkout",
    "commit",
    "clean",
    "launcher",
    "launcher_sha256",
    "launcher_realpath",
    "launcher_realpath_sha256",
    "launcher_symlink_target",
    "interpreter",
    "interpreter_realpath",
    "interpreter_sha256",
    "interpreter_symlink_target",
    "runtime_version",
    "entrypoint",
    "entrypoint_sha256",
    "import_root",
    "import_root_sha256",
    "import_map",
    "dependency_identity",
    "installed_environment_identity",
    "argv_prefix",
    "captured_at",
    "record_path",
    "content_hash",
}
_TOOL_MODULES = {
    "birdclaw": "birdclaw.cli",
    "market_cartographer": "market_cartographer.cli",
    "tradelab": "scripts.market_context.__main__",
    "agent_broker": "agent_broker.cli",
}


def main(argv: list[str] | None = None) -> int:
    if os.getenv("BHIKSHA_SANITIZED_SUBPROCESS") != "1":
        load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contract-dir",
        default=os.getenv(
            "BHIKSHA_CHART_SCENARIO_DAILY_CONTRACT_DIR",
            "artifacts/chart_scenarios/daily-contracts",
        ),
        help="Directory containing one immutable YYYY-MM-DD.json run contract.",
    )
    parser.add_argument(
        "--campaign-config",
        default=os.getenv(
            "BHIKSHA_CHART_SCENARIO_CAMPAIGN_CONFIG",
            "artifacts/chart_scenarios/campaign-config.json",
        ),
        help="Content-addressed fixed campaign configuration used for daily preparation.",
    )
    parser.add_argument(
        "--phase",
        choices=["auto", "morning", "intraday", "after-close"],
        default="auto",
    )
    parser.add_argument("--at", type=datetime.fromisoformat)
    args = parser.parse_args(argv)
    if os.getenv("BHIKSHA_CHART_SCENARIO_SHADOW_ENABLED", "").lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        print(json.dumps({"status": "skipped", "reason": "install_opt_in_disabled"}))
        return 0
    _verify_kernel_source()
    now = args.at or datetime.now(CENTRAL)
    config = _validate_campaign_config(
        json.loads(Path(args.campaign_config).read_text(encoding="utf-8"))
    )
    local = now.astimezone(CENTRAL) if now.tzinfo else now.replace(tzinfo=CENTRAL)
    lock_path = require_experiment_path(
        Path("artifacts/chart_scenarios/locks") / f"{local.date().isoformat()}.lock",
        role="daily run coordinator lock",
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "skipped", "reason": "non_overlap_lock_held"}))
            return 0
        outside = _campaign_window_preflight(
            config, now=now, artifact_root=Path(args.contract_dir).parent
        )
        if outside is not None:
            print(json.dumps(outside, sort_keys=True))
            return 0
        return _main_locked(args, now, config=config)


def _main_locked(
    args: argparse.Namespace, now: datetime, *, config: Mapping[str, Any]
) -> int:
    try:
        contract_path = _resolve_daily_contract(Path(args.contract_dir), now)
    except FileNotFoundError:
        if not _before_prepare_cutoff(now):
            created = _record_missed_preparation(Path(args.contract_dir), now)
            if not created:
                print(
                    json.dumps(
                        {
                            "status": "skipped",
                            "reason": "missed_preparation_already_recorded",
                        }
                    )
                )
                return 0
            raise RuntimeError(
                "daily chart-scenario preparation missed the 08:15 CT cutoff"
            ) from None
        contract_path = _prepare_daily_contract(
            config,
            contract_dir=Path(args.contract_dir),
            now=now,
        )
    contract = _validate_contract(
        json.loads(contract_path.read_text(encoding="utf-8")),
        contract_path=contract_path,
    )
    for field in (
        "campaign_config_hash",
        "campaign_protocol_hash",
        "campaign_freeze_receipt_hash",
        "session_calendar_hash",
    ):
        if _normalized_hash(contract[field]) != _normalized_hash(config[field]):
            raise ValueError(f"daily contract {field} differs from campaign freeze")
    phase = (
        _phase(now, contract["target_session_window"])
        if args.phase == "auto"
        else args.phase
    )
    receipt = _run_phase(contract, phase=phase)
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["status"] in {"succeeded", "skipped"} else 2


def _run_phase(contract: dict[str, Any], *, phase: str) -> dict[str, Any]:
    paths = run_artifact_paths(contract["campaign_id"], contract["run_id"])
    paths.root.mkdir(parents=True, exist_ok=True)
    if contract["outcome"] == "no_plan":
        return _run_no_plan_phase(contract, paths=paths, phase=phase)
    prior = _completed_phase_receipt(paths, contract, phase=phase)
    if prior is not None and phase in {"morning", "after-close"}:
        return {
            "schema": RECEIPT_SCHEMA,
            "status": "skipped",
            "reason": "exact_idempotent_replay",
            "phase": phase,
            "campaign_id": contract["campaign_id"],
            "run_id": contract["run_id"],
            "contract_hash": contract["content_hash"],
            "campaign_config_hash": contract["campaign_config_hash"],
            "prior_receipt_hash": prior["content_hash"],
        }
    actions: list[dict[str, Any]] = []
    if phase == "morning":
        actions.append(_run_tradelab_lifecycle(contract, command="prepare-run"))
        _require_preopen_completion(contract, paths)
        actions.append(_install_or_verify_plan(contract, paths))
        _export_events(paths)
        _stage_tradelab_evidence(contract, paths)
        actions.append(_run_tradelab_lifecycle(contract, command="refresh-projection"))
        _project_if_present(contract, paths, actions)
    elif phase == "intraday":
        _observe_once(contract, paths, actions)
        _export_events(paths)
        _stage_tradelab_evidence(contract, paths)
        actions.append(_run_tradelab_lifecycle(contract, command="refresh-projection"))
        _project_if_present(contract, paths, actions)
    elif phase == "after-close":
        _observe_once(contract, paths, actions)
        _export_events(paths)
        actions.append(
            {
                "action": "export_events",
                "status": "succeeded",
                "path": str(paths.events_export),
            }
        )
        _stage_tradelab_evidence(contract, paths)
        actions.append(_run_tradelab_lifecycle(contract, command="finalize-run"))
        _project_if_present(contract, paths, actions)
    else:
        raise ValueError(f"unsupported coordinator phase: {phase}")
    body = {
        "schema": RECEIPT_SCHEMA,
        "status": "succeeded",
        "created_at": datetime.now(CENTRAL).isoformat(),
        "phase": phase,
        "campaign_id": contract["campaign_id"],
        "run_id": contract["run_id"],
        "contract_hash": contract["content_hash"],
        "campaign_config_hash": contract["campaign_config_hash"],
        "campaign_protocol_hash": contract["campaign_protocol_hash"],
        "campaign_freeze_receipt_hash": contract["campaign_freeze_receipt_hash"],
        "session_calendar_hash": contract["session_calendar_hash"],
        "run_root": str(paths.root),
        "actions": actions,
        "effects": {
            "broker": False,
            "orders": False,
            "authorization": False,
            "sheet_tab": "Chart_Scenarios_v1",
        },
    }
    receipt = {**body, "content_hash": canonical_sha256(body)}
    receipt_dir = paths.root / "coordinator"
    receipt_name = f"{phase}-{body['created_at'].replace(':', '')}.receipt.json"
    _write_atomic(receipt_dir / receipt_name, receipt)
    _write_atomic(receipt_dir / "latest.json", receipt)
    if phase in {"morning", "after-close"}:
        _write_atomic(receipt_dir / f"{phase}.complete.json", receipt)
    return receipt


def _run_no_plan_phase(
    contract: dict[str, Any], *, paths: Any, phase: str
) -> dict[str, Any]:
    marker = paths.root / "coordinator" / "no-plan.complete.json"
    if marker.is_file():
        prior = json.loads(marker.read_text(encoding="utf-8"))
        _validate_no_plan_coordinator_receipt(prior, contract=contract)
        return {
            "schema": RECEIPT_SCHEMA,
            "status": "skipped",
            "reason": "authenticated_no_plan",
            "outcome": "no_plan",
            "phase": phase,
            "campaign_id": contract["campaign_id"],
            "run_id": contract["run_id"],
            "contract_hash": contract["content_hash"],
            "prior_receipt_hash": prior["content_hash"],
            "effects": {
                "broker": False,
                "orders": False,
                "authorization": False,
                "sheet": False,
                "plan_install": False,
            },
        }
    if phase != "morning":
        raise ValueError("authenticated no-plan run has no morning preparation receipt")
    action = _run_tradelab_lifecycle(contract, command="prepare-run")
    lifecycle = action.pop("lifecycle_receipt", None)
    validated = _validate_tradelab_no_plan_receipt(lifecycle, contract=contract)
    body = {
        "schema": RECEIPT_SCHEMA,
        "status": "succeeded",
        "reason": "authenticated_no_plan",
        "outcome": "no_plan",
        "created_at": datetime.now(CENTRAL).isoformat(),
        "phase": "morning",
        "campaign_id": contract["campaign_id"],
        "run_id": contract["run_id"],
        "contract_hash": contract["content_hash"],
        "campaign_config_hash": contract["campaign_config_hash"],
        "campaign_protocol_hash": contract["campaign_protocol_hash"],
        "campaign_freeze_receipt_hash": contract["campaign_freeze_receipt_hash"],
        "session_calendar_hash": contract["session_calendar_hash"],
        "tradelab_preparation_receipt_hash": validated["content_hash"],
        "actions": [action],
        "effects": {
            "broker": False,
            "orders": False,
            "authorization": False,
            "sheet": False,
            "plan_install": False,
        },
    }
    receipt = {**body, "content_hash": canonical_sha256(body)}
    _write_atomic(marker, receipt)
    _write_atomic(paths.root / "coordinator" / "morning.complete.json", receipt)
    return receipt


def _observe_once(
    contract: dict[str, Any], paths: Any, actions: list[dict[str, Any]]
) -> None:
    plan = read_installed_plan(paths.plan)
    repository = ScenarioEventRepository(paths.database)
    candidate_ids = tuple(
        sorted({scenario.candidate_id for scenario in plan.scenarios})
    )
    completed_slot = _completed_cycle_slot(paths, plan=plan)
    latest_slot = repository.latest_observation_slot_ordinal(
        run_id=str(plan.run_manifest["run_id"]), candidate_ids=candidate_ids
    )
    pending_input = paths.cycle_inputs / f"slot-{latest_slot:04d}.cycle-input.json"
    if latest_slot > completed_slot:
        if latest_slot != completed_slot + 1 or not pending_input.is_file():
            raise ValueError("in-flight observation slot cannot be resumed exactly")
        cycle_input_path = pending_input
        cycle_input = json.loads(cycle_input_path.read_text(encoding="utf-8"))
        if cycle_input.get("observation_slot_ordinal") != latest_slot:
            raise ValueError("in-flight cycle input does not match durable slot")
        export = {
            "status": "skipped",
            "reason": "reuse_inflight_cycle_input",
        }
        slot = latest_slot
    else:
        slot = completed_slot + 1
        cycle_input_path = paths.cycle_inputs / f"slot-{slot:04d}.cycle-input.json"
        if cycle_input_path.is_file():
            export = {
                "status": "skipped",
                "reason": "reuse_failed_attempt_cycle_input",
            }
        else:
            export = _run_command(
                [
                    sys.executable,
                    "-m",
                    "bhiksha.tools.chart_scenario_live_export",
                    "--plan",
                    str(paths.plan),
                    "--db-path",
                    str(paths.database),
                    "--output",
                    str(cycle_input_path),
                    "--observation-slot",
                    str(slot),
                ],
                cwd=Path.cwd(),
                env=_sanitized_subprocess_env(role="schwab_market_data"),
            )
    cycle_input = json.loads(cycle_input_path.read_text(encoding="utf-8"))
    if int(cycle_input["observation_slot_ordinal"]) != slot:
        raise ValueError("exported cycle input used an unexpected observation slot")
    receipt_path = paths.cycle_receipts / f"slot-{slot:04d}.receipt.json"
    cycle = _run_command(
        [
            sys.executable,
            "-m",
            "bhiksha.chart_scenarios",
            "observe-cycle",
            "--plan",
            str(paths.plan),
            "--cycle-input",
            str(cycle_input_path),
            "--db-path",
            str(paths.database),
            "--receipt",
            str(receipt_path),
        ],
        cwd=Path.cwd(),
        env=_sanitized_subprocess_env(role="broker_inert"),
    )
    actions.extend(
        [
            {"action": "export_live_cycle", **export, "slot": slot},
            {
                "action": "observe_cycle",
                **cycle,
                "slot": slot,
                "receipt": str(receipt_path),
            },
        ]
    )


def _completed_cycle_slot(paths: Any, *, plan: ShadowPlan) -> int:
    return len(_validate_cycle_artifacts(paths, plan=plan, allow_pending_input=True))


def _project_if_present(
    contract: dict[str, Any], paths: Any, actions: list[dict[str, Any]]
) -> None:
    request = Path(contract["projection_request"])
    if not request.is_file():
        actions.append(
            {
                "action": "project_sheet",
                "status": "skipped",
                "reason": "projection_request_not_ready",
            }
        )
        return
    result = _run_command(
        [
            sys.executable,
            "-m",
            "bhiksha.tools.chart_scenario_sheet_project",
            "--request",
            str(request),
            "--receipt",
            str(paths.projection_receipt),
            "--plan",
            str(paths.plan),
        ],
        cwd=Path.cwd(),
        env=_sanitized_subprocess_env(role="google_sheet"),
    )
    actions.append(
        {
            "action": "project_sheet",
            **result,
            "receipt": str(paths.projection_receipt),
        }
    )


def _run_tradelab_lifecycle(
    contract: Mapping[str, Any], *, command: str
) -> dict[str, Any]:
    _verify_frozen_toolchain(contract)
    if command not in {"prepare-run", "refresh-projection", "finalize-run"}:
        raise ValueError("unsupported fixed TradeLab lifecycle command")
    cwd = Path(contract["tradelab_checkout"])
    env = _sanitized_subprocess_env(role="broker_inert")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(cwd), str(Path(contract["kernel_src"])), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    tradelab_runtime = _verify_toolchain_role(contract, role="tradelab")
    agent_broker_runtime = _verify_toolchain_role(contract, role="agent_broker")
    argv = [
        *tradelab_runtime["argv_prefix"],
        command,
        "--experiment-store",
        str(contract["tradelab_experiment_root"]),
        "--campaign-id",
        str(contract["campaign_id"]),
    ]
    if command == "prepare-run":
        argv.extend(
            [
                "--cartographer-export-dir",
                str(Path(str(contract["cartographer_receipt"])).parent),
                "--agent-broker",
                str(contract["agent_broker"]),
                "--agent-broker-runtime-record",
                str(agent_broker_runtime["record_path"]),
                "--spreadsheet-id",
                str(contract["spreadsheet_id"]),
            ]
        )
        if contract.get("birdclaw_export"):
            argv.extend(["--birdclaw-export", str(contract["birdclaw_export"])])
    else:
        argv.extend(
            [
                "--run-id",
                str(contract["run_id"]),
                "--spreadsheet-id",
                str(contract["spreadsheet_id"]),
            ]
        )
        if command == "finalize-run":
            argv.extend(["--bhiksha-root", str(Path.cwd().resolve())])
    completed = _execute_command(argv, cwd=cwd, env=env)
    # TradeLab may invoke Agent Broker while handling prepare-run.  Neither
    # runtime is accepted on return until both still match the frozen campaign
    # records.  This deliberately happens before stdout is parsed as a receipt.
    _verify_toolchain_role(contract, role="tradelab")
    _verify_toolchain_role(contract, role="agent_broker")
    execution = _completed_command_result(completed)
    lifecycle_receipt = execution.pop("_output", None)
    return {
        "action": f"tradelab:{command}",
        "command_hash": canonical_sha256(argv),
        **execution,
        "lifecycle_receipt": lifecycle_receipt,
    }


def _run_command(
    command: list[str], *, cwd: Path, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    completed = _execute_command(command, cwd=cwd, env=env)
    return _completed_command_result(completed)


def _completed_command_result(
    completed: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed rc={completed.returncode}: "
            + (completed.stderr or completed.stdout)[-2000:]
        )
    return {
        "status": "succeeded",
        "return_code": completed.returncode,
        "stdout_hash": canonical_sha256(completed.stdout),
        "stdout_tail": completed.stdout[-2000:],
        "_output": _last_json_object(completed.stdout),
    }


def _last_json_object(text: str) -> dict[str, Any] | None:
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _sanitized_subprocess_env(
    *, role: str, additions: Mapping[str, str] | None = None
) -> dict[str, str]:
    allowed = set(_COMMON_ENV_ALLOWLIST)
    if role == "schwab_market_data":
        allowed.update(_SCHWAB_MARKET_DATA_ENV)
    elif role == "google_sheet":
        allowed.update(_GOOGLE_SHEET_ENV)
    elif role != "broker_inert":
        raise ValueError(f"unsupported chart subprocess role: {role}")
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env["BHIKSHA_SANITIZED_SUBPROCESS"] = "1"
    if additions:
        env.update({str(key): str(value) for key, value in additions.items()})
    return env


def _execute_command(
    command: list[str], *, cwd: Path, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        cwd=cwd,
        env=dict(env or _sanitized_subprocess_env(role="broker_inert")),
        text=True,
        capture_output=True,
        timeout=float(
            os.getenv("BHIKSHA_CHART_SCENARIO_COMMAND_TIMEOUT_SECONDS", "600")
        ),
    )


def _export_events(paths: Any) -> None:
    repository = ScenarioEventRepository(paths.database)
    chain = repository.verify_event_chain()
    if not chain.valid:
        raise ValueError("run-scoped event chain failed verification")
    events = [
        {**event.model_dump(mode="json"), "event_hash": event.event_hash}
        for event in repository.events()
    ]
    if events and events[0].get("preceding_event_hash") is not None:
        raise ValueError("run-scoped event chain does not start at null predecessor")
    body = {
        "schema": "bhiksha.chart-scenario-events-export.v1",
        "event_count": len(events),
        "last_event_hash": chain.last_event_hash,
        "events": events,
    }
    _write_atomic(paths.events_export, {**body, "content_hash": canonical_sha256(body)})


def _validate_contract(
    value: Mapping[str, Any], *, contract_path: Path | None = None
) -> dict[str, Any]:
    expected = {
        "schema",
        "campaign_id",
        "run_id",
        "outcome",
        "target_session_date",
        "target_session_window",
        "target_session_window_hash",
        "cartographer_receipt",
        "birdclaw_export",
        "birdclaw_packet_hash",
        "birdclaw_output_hash",
        "narrative_source_failure",
        "campaign_config_hash",
        "campaign_protocol_hash",
        "campaign_freeze_receipt_hash",
        "session_calendar_hash",
        "toolchain",
        "toolchain_hash",
        "tradelab_checkout",
        "tradelab_experiment_root",
        "agent_broker",
        "agent_broker_checkout",
        "spreadsheet_id",
        "kernel_src",
        "plan_source",
        "projection_request",
        "content_hash",
    }
    if set(value) != expected or value.get("schema") != SCHEMA:
        raise ValueError("coordinator contract has unsupported or non-exact fields")
    if value.get("outcome") not in {"plan", "no_plan"}:
        raise ValueError("coordinator contract outcome must be plan or no_plan")
    computed = canonical_sha256(
        {key: item for key, item in value.items() if key != "content_hash"}
    )
    if str(value.get("content_hash", "")).removeprefix("sha256:") != computed:
        raise ValueError("coordinator contract content hash mismatch")
    if value.get("toolchain_hash") != canonical_sha256(value.get("toolchain")):
        raise ValueError("coordinator contract toolchain hash mismatch")
    if (
        re.fullmatch(
            r"[0-9a-f]{64}",
            str(value.get("campaign_config_hash", "")).removeprefix("sha256:"),
        )
        is None
    ):
        raise ValueError("coordinator contract campaign_config_hash is invalid")
    for field in (
        "campaign_protocol_hash",
        "campaign_freeze_receipt_hash",
        "session_calendar_hash",
    ):
        _normalized_hash(value.get(field))
    target_date = _parse_target_date(value.get("target_session_date"))
    if (
        contract_path is not None
        and contract_path.name != f"{target_date.isoformat()}.json"
    ):
        raise ValueError(
            "daily coordinator contract filename must equal target session date"
        )
    window = _validate_session_window(
        value.get("target_session_window"),
        target_date=target_date,
        expected_hash=value.get("target_session_window_hash"),
    )
    _validate_cartographer_receipt(value, window=window)
    _validate_birdclaw_export(value, window=window)
    _validate_tradelab_paths(value)
    return {
        **dict(value),
        "target_session_window": window,
        "content_hash": computed,
    }


def _phase(now: datetime, target_session_window: Mapping[str, Any]) -> str:
    current = now if now.tzinfo else now.replace(tzinfo=CENTRAL)
    start = datetime.fromisoformat(str(target_session_window["start_at"]))
    end = datetime.fromisoformat(str(target_session_window["end_at"]))
    if current < start:
        return "morning"
    if current <= end:
        return "intraday"
    return "after-close"


def _resolve_daily_contract(directory: Path, now: datetime) -> Path:
    local = now.astimezone(CENTRAL) if now.tzinfo else now.replace(tzinfo=CENTRAL)
    root = require_experiment_path(directory, role="daily contract directory")
    path = root / f"{local.date().isoformat()}.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"no daily chart-scenario contract for {local.date()}: {path}"
        )
    return path


def _before_prepare_cutoff(now: datetime) -> bool:
    local = now.astimezone(CENTRAL) if now.tzinfo else now.replace(tzinfo=CENTRAL)
    return local.hour * 60 + local.minute <= PREPARE_CUTOFF_MINUTES


def _verify_kernel_source() -> None:
    import mala_bhiksha_kernel

    verify_kernel_runtime_from_env(imported_module=mala_bhiksha_kernel)


def _validate_campaign_config(value: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "schema",
        "campaign_id",
        "birdclaw_checkout",
        "birdclaw_db",
        "market_cartographer_checkout",
        "tradelab_checkout",
        "tradelab_experiment_root",
        "agent_broker",
        "agent_broker_checkout",
        "spreadsheet_id",
        "kernel_src",
        "cartographer_provider",
        "cartographer_data_root",
        "symbols",
        "campaign_manifest_hash",
        "campaign_protocol_hash",
        "campaign_freeze_receipt_hash",
        "treatment_manifest_hash",
        "universe_hash",
        "session_calendar_hash",
        "session_calendar_id",
        "session_calendar_version",
        "toolchain",
        "starts_on",
        "checkpoint_after_sessions",
        "max_sessions",
        "ends_on",
        "content_hash",
    }
    if set(value) != expected or value.get("schema") != CAMPAIGN_CONFIG_SCHEMA:
        raise ValueError(
            "chart-scenario campaign config has unsupported or non-exact fields"
        )
    computed = canonical_sha256(
        {key: item for key, item in value.items() if key != "content_hash"}
    )
    if str(value.get("content_hash", "")).removeprefix("sha256:") != computed:
        raise ValueError("chart-scenario campaign config content hash mismatch")
    if value.get("cartographer_provider") not in {"mala", "fixture"}:
        raise ValueError("Cartographer provider must be mala or fixture")
    if value.get("cartographer_provider") == "mala" and not value.get(
        "cartographer_data_root"
    ):
        raise ValueError("mala Cartographer preparation requires data root")
    symbols = value.get("symbols")
    if (
        not isinstance(symbols, list)
        or not symbols
        or any(
            not isinstance(symbol, str)
            or not symbol
            or symbol != symbol.strip().upper()
            for symbol in symbols
        )
    ):
        raise ValueError("campaign symbols must be non-empty normalized tickers")
    require_experiment_path(
        str(value["tradelab_experiment_root"]), role="TradeLab experiment root"
    )
    if not str(value.get("agent_broker") or "").strip():
        raise ValueError("campaign agent_broker must be a fixed executable path")
    if value.get("spreadsheet_id") != SPREADSHEET_ID:
        raise ValueError("campaign spreadsheet_id must be the fixed experiment Sheet")
    configured_kernel_src = Path(os.getenv("BHIKSHA_KERNEL_SRC", "")).expanduser()
    campaign_kernel_src = Path(str(value.get("kernel_src") or "")).expanduser()
    if os.getenv("BHIKSHA_KERNEL_SRC") and (
        not configured_kernel_src.is_absolute()
        or not campaign_kernel_src.is_absolute()
        or campaign_kernel_src.resolve() != configured_kernel_src.resolve()
    ):
        raise ValueError("campaign kernel_src differs from frozen launchd runtime")
    birdclaw_db = Path(str(value.get("birdclaw_db") or "")).expanduser()
    if not birdclaw_db.is_absolute() or not birdclaw_db.is_file():
        raise ValueError("campaign birdclaw_db must be an existing absolute file")
    try:
        starts_on = date.fromisoformat(str(value.get("starts_on")))
        ends_on = date.fromisoformat(str(value.get("ends_on")))
    except ValueError as exc:
        raise ValueError("campaign boundaries must use YYYY-MM-DD") from exc
    if value.get("checkpoint_after_sessions") != 5:
        raise ValueError("campaign checkpoint_after_sessions must be exactly 5")
    if value.get("max_sessions") != 10:
        raise ValueError("campaign max_sessions must be exactly 10")
    campaign_root = (
        Path(str(value["tradelab_experiment_root"]))
        / "campaigns"
        / str(value["campaign_id"])
    )
    campaign = _read_content_addressed(
        campaign_root / "campaign.json", schema="tradelab.market_context_campaign.v2"
    )
    protocol = _read_content_addressed(
        campaign_root / "campaign-protocol.json",
        schema="tradelab.market_context_campaign_protocol.v1",
    )
    freeze = _read_content_addressed(
        campaign_root / "campaign-freeze-receipt.json",
        schema="tradelab.market_context_campaign_freeze_receipt.v1",
    )
    bindings = {
        "campaign_manifest_hash": campaign["content_hash"],
        "campaign_protocol_hash": protocol["content_hash"],
        "campaign_freeze_receipt_hash": freeze["content_hash"],
        "treatment_manifest_hash": protocol["treatment_manifest_hash"],
        "universe_hash": protocol["universe_hash"],
        "session_calendar_hash": protocol["session_calendar_hash"],
    }
    for field, actual in bindings.items():
        if _normalized_hash(value.get(field)) != _normalized_hash(actual):
            raise ValueError(f"campaign {field} does not match TradeLab freeze")
    if (
        campaign.get("campaign_id") != value["campaign_id"]
        or _normalized_hash(campaign.get("treatment_manifest_hash"))
        != _normalized_hash(protocol.get("treatment_manifest_hash"))
        or _normalized_hash(campaign.get("universe_hash"))
        != _normalized_hash(protocol.get("universe_hash"))
    ):
        raise ValueError("TradeLab campaign manifest identity is inconsistent")
    calendar = protocol.get("session_calendar")
    if not isinstance(calendar, Mapping):
        raise TypeError("TradeLab campaign protocol has no session calendar")
    calendar_body = {
        key: item for key, item in calendar.items() if key != "content_hash"
    }
    authorized = protocol.get("authorized_session_dates")
    if (
        calendar.get("schema") != "tradelab.market_context_session_calendar.v1"
        or calendar.get("calendar_id") != "XNYS"
        or calendar.get("timezone") != "America/New_York"
        or calendar.get("implementation") != "exchange_calendars"
        or calendar.get("calendar_version") != CALENDAR_VERSION
        or _normalized_hash(calendar.get("content_hash"))
        != canonical_sha256(calendar_body)
        or _normalized_hash(protocol.get("session_calendar_hash"))
        != _normalized_hash(calendar.get("content_hash"))
        or value.get("session_calendar_id") != calendar.get("calendar_id")
        or value.get("session_calendar_version") != calendar.get("calendar_version")
        or not isinstance(authorized, list)
        or authorized != calendar.get("session_dates")
        or calendar.get("session_count") != 10
        or calendar.get("starts_on") != starts_on.isoformat()
        or calendar.get("ends_on") != ends_on.isoformat()
        or calendar.get("ends_on_semantics") != "inclusive"
        or protocol.get("checkpoint_after_sessions") != 5
        or protocol.get("max_sessions") != 10
        or protocol.get("starts_on") != starts_on.isoformat()
        or protocol.get("ends_on") != ends_on.isoformat()
        or protocol.get("ends_on_semantics") != "inclusive"
    ):
        raise ValueError("TradeLab campaign session calendar bindings are invalid")
    sessions = tuple(date.fromisoformat(item) for item in authorized)
    if len(sessions) != 10 or sessions[0] != starts_on or sessions[-1] != ends_on:
        raise ValueError("campaign boundaries must be exactly 10 XNYS sessions")
    if sessions != xnys_session_dates(starts_on, ends_on):
        raise ValueError("TradeLab authorized sessions drift from pinned XNYS calendar")
    cross = (
        campaign.get("content_hash"),
        protocol.get("campaign_manifest_hash"),
        freeze.get("campaign_manifest_hash"),
    )
    if len({_normalized_hash(item) for item in cross}) != 1:
        raise ValueError("TradeLab campaign artifacts disagree on campaign manifest")
    for field in (
        "campaign_protocol_hash",
        "treatment_manifest_hash",
        "universe_hash",
        "session_calendar_hash",
    ):
        expected_value = (
            protocol["content_hash"]
            if field == "campaign_protocol_hash"
            else protocol[field]
        )
        if _normalized_hash(freeze.get(field)) != _normalized_hash(expected_value):
            raise ValueError(f"TradeLab freeze receipt disagrees on {field}")
    if (
        freeze.get("starts_on") != starts_on.isoformat()
        or freeze.get("ends_on") != ends_on.isoformat()
        or freeze.get("checkpoint_after_sessions") != 5
        or freeze.get("max_sessions") != 10
        or freeze.get("minimum_closed_trigger_count") != 10
    ):
        raise ValueError("TradeLab freeze receipt campaign boundaries are invalid")
    return {
        **dict(value),
        "content_hash": computed,
        "_authorized_session_dates": tuple(authorized),
    }


def _normalized_hash(value: Any) -> str:
    normalized = str(value or "").removeprefix("sha256:")
    if re.fullmatch(r"[0-9a-f]{64}", normalized) is None:
        raise ValueError("expected sha256 identity")
    return normalized


def _verify_frozen_toolchain(config: Mapping[str, Any]) -> None:
    toolchain = config.get("toolchain")
    configured_fields = {
        "birdclaw": "birdclaw_checkout",
        "market_cartographer": "market_cartographer_checkout",
        "tradelab": "tradelab_checkout",
        "agent_broker": "agent_broker_checkout",
    }
    roles = {
        role: Path(str(config[field]))
        for role, field in configured_fields.items()
        if field in config
    }
    if not isinstance(toolchain, Mapping) or set(toolchain) != set(configured_fields):
        raise ValueError("campaign toolchain must bind every invoked checkout")
    for role, configured_checkout in roles.items():
        _verify_toolchain_role(
            config,
            role=role,
            configured_checkout=configured_checkout,
        )


def _verify_toolchain_role(
    config: Mapping[str, Any],
    *,
    role: str,
    configured_checkout: Path | None = None,
) -> dict[str, Any]:
    """Revalidate the exact runtime record immediately before invocation."""

    toolchain = config.get("toolchain")
    record = toolchain.get(role) if isinstance(toolchain, Mapping) else None
    if not isinstance(record, Mapping) or set(record) != _RUNTIME_RECORD_FIELDS:
        raise ValueError(f"campaign toolchain runtime record is invalid: {role}")
    if record.get("schema") != _RUNTIME_RECORD_SCHEMA or record.get("role") != role:
        raise ValueError(f"campaign toolchain runtime schema is invalid: {role}")
    computed = canonical_sha256(
        {key: item for key, item in record.items() if key != "content_hash"}
    )
    if _normalized_hash(record.get("content_hash")) != computed:
        raise ValueError(f"campaign toolchain runtime hash drift: {role}")
    try:
        captured_at = datetime.fromisoformat(str(record["captured_at"]))
    except ValueError as exc:
        raise ValueError(f"campaign toolchain captured_at is invalid: {role}") from exc
    if captured_at.tzinfo is None or record.get("clean") is not True:
        raise ValueError(f"campaign toolchain runtime was not cleanly captured: {role}")

    requested_record = Path(str(record["record_path"])).expanduser()
    if requested_record.is_symlink() or any(
        parent.is_symlink() for parent in requested_record.parents
    ):
        raise ValueError(f"campaign toolchain runtime record path is invalid: {role}")
    record_path = require_experiment_path(
        requested_record, role=f"{role} runtime record"
    )
    if not record_path.is_file():
        raise ValueError(f"campaign toolchain runtime record path is invalid: {role}")
    persisted = json.loads(record_path.read_text(encoding="utf-8"))
    if persisted != dict(record):
        raise ValueError(f"campaign toolchain runtime record file drift: {role}")

    requested_checkout = Path(str(record["checkout"])).expanduser()
    if requested_checkout.is_symlink():
        raise ValueError(f"campaign toolchain checkout cannot be a symlink: {role}")
    checkout = requested_checkout.resolve()
    configured = configured_checkout or Path(str(config[f"{role}_checkout"]))
    if checkout != configured.expanduser().resolve() or not checkout.is_dir():
        raise ValueError(f"campaign toolchain checkout differs for {role}")
    status = _runtime_probe(
        ["git", "-C", str(checkout), "status", "--porcelain"], cwd=checkout
    )
    commit = _runtime_probe(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], cwd=checkout
    )
    if status.returncode != 0 or status.stdout.strip():
        raise ValueError(f"campaign toolchain checkout is not clean: {role}")
    if commit.returncode != 0 or commit.stdout.strip() != record["commit"]:
        raise ValueError(f"campaign toolchain commit drift: {role}")

    launcher = _verify_runtime_file(record, prefix="launcher", role=role)
    interpreter = _verify_runtime_file(record, prefix="interpreter", role=role)
    if not os.access(launcher, os.X_OK) or not os.access(interpreter, os.X_OK):
        raise ValueError(f"campaign toolchain runtime is not executable: {role}")
    version = _runtime_probe([str(interpreter), "--version"], cwd=checkout)
    observed_version = (version.stdout or version.stderr).strip()
    if version.returncode != 0 or observed_version != record["runtime_version"]:
        raise ValueError(f"campaign toolchain runtime version drift: {role}")

    entrypoint = _verified_checkout_file(
        record["entrypoint"], checkout=checkout, role=role, label="entrypoint"
    )
    if _file_sha256(entrypoint) != _normalized_hash(record["entrypoint_sha256"]):
        raise ValueError(f"campaign toolchain entrypoint drift: {role}")
    requested_import_root = Path(str(record["import_root"])).expanduser()
    import_root = requested_import_root.resolve()
    if (
        requested_import_root.is_symlink()
        or not import_root.is_dir()
        or not import_root.is_relative_to(checkout)
    ):
        raise ValueError(f"campaign toolchain import root escaped checkout: {role}")
    if _source_tree_sha256(import_root) != _normalized_hash(
        record["import_root_sha256"]
    ):
        raise ValueError(f"campaign toolchain import tree drift: {role}")

    module = _TOOL_MODULES[role]
    import_map = record.get("import_map")
    expected_import = {"path": str(entrypoint), "sha256": _file_sha256(entrypoint)}
    if not isinstance(import_map, Mapping) or dict(import_map) != {
        module: expected_import
    }:
        raise ValueError(f"campaign toolchain import map drift: {role}")
    if role != "birdclaw":
        origin = _resolve_module_origin(
            interpreter,
            module=module,
            role=role,
            config=config,
            cwd=(
                Path(str(config["tradelab_checkout"])).resolve()
                if role == "agent_broker"
                else checkout
            ),
        )
        if origin != entrypoint:
            raise ValueError(f"campaign toolchain module resolution drift: {role}")

    _verify_dependency_identity(
        record["dependency_identity"],
        checkout=checkout,
        import_root=import_root,
        role=role,
    )
    observed_environment = _capture_installed_environment_identity(
        interpreter,
        role=role,
        checkout=checkout,
    )
    if record["installed_environment_identity"] != observed_environment:
        raise ValueError(
            f"campaign toolchain installed environment drift: {role}"
        )
    prefix = record.get("argv_prefix")
    expected_prefix = {
        "birdclaw": [str(launcher), str(entrypoint)],
        "market_cartographer": [str(launcher), "-m", "market_cartographer.cli"],
        "tradelab": [str(launcher), "-m", "scripts.market_context"],
        "agent_broker": [str(launcher)],
    }[role]
    if prefix != expected_prefix:
        raise ValueError(f"campaign toolchain argv prefix drift: {role}")
    if role == "agent_broker":
        _verify_launcher_shebang(launcher, interpreter=interpreter)
        if (
            launcher.resolve()
            != Path(str(config["agent_broker"])).expanduser().resolve()
        ):
            raise ValueError("campaign Agent Broker launcher differs from executable")
    return dict(record)


def _runtime_probe(
    command: list[str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        cwd=cwd,
        text=True,
        capture_output=True,
        env=_sanitized_subprocess_env(role="broker_inert"),
    )


def _verify_runtime_file(record: Mapping[str, Any], *, prefix: str, role: str) -> Path:
    requested = Path(str(record[prefix])).expanduser()
    if not requested.is_absolute() or not requested.is_file():
        raise ValueError(f"campaign toolchain {prefix} is invalid: {role}")
    observed_link = os.readlink(requested) if requested.is_symlink() else None
    if observed_link != record[f"{prefix}_symlink_target"]:
        raise ValueError(f"campaign toolchain {prefix} symlink drift: {role}")
    resolved = requested.resolve()
    if str(resolved) != record[f"{prefix}_realpath"]:
        raise ValueError(f"campaign toolchain {prefix} realpath drift: {role}")
    digest = _file_sha256(requested)
    if digest != _normalized_hash(record[f"{prefix}_sha256"]):
        raise ValueError(f"campaign toolchain {prefix} drift: {role}")
    if prefix == "launcher" and digest != _normalized_hash(
        record["launcher_realpath_sha256"]
    ):
        raise ValueError(f"campaign toolchain launcher realpath drift: {role}")
    return requested


def _verified_checkout_file(
    value: Any, *, checkout: Path, role: str, label: str
) -> Path:
    requested = Path(str(value)).expanduser()
    resolved = requested.resolve()
    if (
        not requested.is_absolute()
        or requested.is_symlink()
        or not resolved.is_file()
        or not resolved.is_relative_to(checkout)
    ):
        raise ValueError(f"campaign toolchain {label} escaped checkout: {role}")
    return resolved


def _source_tree_sha256(root: Path) -> str:
    entries: list[dict[str, str]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise ValueError(
                f"campaign toolchain import tree contains symlink: {relative}"
            )
        if not path.is_file():
            continue
        if (
            "__pycache__" in relative.parts
            or path.suffix in {".pyc", ".pyo"}
            or path.name == ".DS_Store"
        ):
            continue
        entries.append({"path": relative.as_posix(), "sha256": _file_sha256(path)})
    if not entries:
        raise ValueError("campaign toolchain import tree is empty")
    return canonical_sha256(entries)


def _resolve_module_origin(
    interpreter: Path,
    *,
    module: str,
    role: str,
    config: Mapping[str, Any],
    cwd: Path,
) -> Path:
    env = _sanitized_subprocess_env(role="broker_inert")
    if role == "market_cartographer":
        env["PYTHONPATH"] = os.pathsep.join(
            [
                str(Path(str(config["market_cartographer_checkout"])) / "src"),
                str(Path(str(config["kernel_src"]))),
            ]
        )
    elif role == "tradelab":
        env["PYTHONPATH"] = os.pathsep.join(
            [
                str(Path(str(config["tradelab_checkout"]))),
                str(Path(str(config["kernel_src"]))),
            ]
        )
    script = (
        "import importlib.util; "
        f"spec=importlib.util.find_spec({module!r}); "
        "print(spec.origin if spec and spec.origin else '')"
    )
    completed = subprocess.run(
        [str(interpreter), "-c", script],
        check=False,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise ValueError(f"campaign toolchain module is unavailable: {role}")
    return Path(completed.stdout.strip()).resolve()


def _verify_dependency_identity(
    value: Any, *, checkout: Path, import_root: Path, role: str
) -> None:
    if not isinstance(value, Mapping) or set(value) != {"mode", "path", "sha256"}:
        raise ValueError(f"campaign toolchain dependency identity is invalid: {role}")
    if value["mode"] == "source_tree_only":
        if value["path"] is not None or _normalized_hash(
            value["sha256"]
        ) != _source_tree_sha256(import_root):
            raise ValueError(f"campaign toolchain source dependency drift: {role}")
        return
    if value["mode"] != "lockfile":
        raise ValueError(f"campaign toolchain dependency mode is invalid: {role}")
    lock = Path(str(value["path"])).expanduser()
    if (
        not lock.is_absolute()
        or lock.is_symlink()
        or not lock.is_file()
        or not lock.resolve().is_relative_to(checkout)
    ):
        raise ValueError(f"campaign toolchain dependency lock is invalid: {role}")
    if _file_sha256(lock) != _normalized_hash(value["sha256"]):
        raise ValueError(f"campaign toolchain dependency lock drift: {role}")


def _capture_installed_environment_identity(
    interpreter: Path, *, role: str, checkout: Path
) -> dict[str, Any]:
    """Hash the effective isolated dependency tree, excluding mutable caches.

    A lockfile states intent; it does not authenticate the packages the frozen
    interpreter can actually import.  Python tool roles therefore require a
    checkout-local virtual environment with system site packages disabled.
    Birdclaw's Node runtime binds checkout-local ``node_modules`` and rejects
    ambient ancestor module trees.
    """

    if role == "birdclaw":
        roots: list[dict[str, str]] = []
        local_modules = checkout / "node_modules"
        for parent in checkout.parents:
            ambient = parent / "node_modules"
            if ambient.is_dir():
                raise ValueError(
                    "campaign Birdclaw runtime has ambient ancestor node_modules"
                )
        if local_modules.exists():
            roots.append(
                {
                    "path": str(local_modules.resolve()),
                    "sha256": _runtime_tree_sha256(local_modules),
                }
            )
        body: dict[str, Any] = {
            "schema": "bhiksha.chart-scenario-installed-environment.v1",
            "mode": "isolated_node_environment",
            "environment_root": str(checkout),
            "site_packages": roots,
            "pyvenv_cfg_sha256": None,
        }
        return {**body, "content_hash": canonical_sha256(body)}

    environment_root = interpreter.parent.parent.resolve()
    if not environment_root.is_relative_to(checkout):
        raise ValueError(
            f"campaign Python environment must be isolated under checkout: {role}"
        )
    pyvenv_cfg = environment_root / "pyvenv.cfg"
    if not pyvenv_cfg.is_file() or pyvenv_cfg.is_symlink():
        raise ValueError(f"campaign Python environment is not a real venv: {role}")
    config_text = pyvenv_cfg.read_text(encoding="utf-8")
    settings = {
        key.strip().lower(): value.strip().lower()
        for line in config_text.splitlines()
        if "=" in line
        for key, value in [line.split("=", 1)]
    }
    if settings.get("include-system-site-packages") != "false":
        raise ValueError(
            f"campaign Python environment exposes system site-packages: {role}"
        )
    roots = sorted(
        {
            path.resolve()
            for lib in (environment_root / "lib", environment_root / "lib64")
            if lib.is_dir()
            for path in lib.glob("python*/site-packages")
            if path.is_dir()
        },
        key=lambda item: item.as_posix(),
    )
    if not roots:
        raise ValueError(f"campaign Python environment has no site-packages: {role}")
    site_packages = [
        {"path": str(path), "sha256": _runtime_tree_sha256(path)} for path in roots
    ]
    body = {
        "schema": "bhiksha.chart-scenario-installed-environment.v1",
        "mode": "isolated_python_environment",
        "environment_root": str(environment_root),
        "site_packages": site_packages,
        "pyvenv_cfg_sha256": _file_sha256(pyvenv_cfg),
    }
    return {**body, "content_hash": canonical_sha256(body)}


def _runtime_tree_sha256(root: Path) -> str:
    requested = root.expanduser()
    if requested.is_symlink() or not requested.is_dir():
        raise ValueError(f"installed environment root is not a real directory: {root}")
    entries: list[dict[str, str]] = []
    for path in sorted(requested.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(requested)
        if path.is_symlink():
            raise ValueError(
                f"installed environment contains symlink: {relative.as_posix()}"
            )
        if not path.is_file():
            continue
        if (
            "__pycache__" in relative.parts
            or path.suffix in {".pyc", ".pyo"}
            or path.name == ".DS_Store"
        ):
            continue
        entries.append(
            {"path": relative.as_posix(), "sha256": _file_sha256(path)}
        )
    return canonical_sha256(entries)


def _verify_launcher_shebang(launcher: Path, *, interpreter: Path) -> None:
    first = launcher.read_text(encoding="utf-8").splitlines()[0]
    if not first.startswith("#!"):
        raise ValueError("campaign Agent Broker launcher has no shebang")
    tokens = shlex.split(first[2:].strip())
    if (
        len(tokens) != 1
        or Path(tokens[0]).expanduser().resolve() != interpreter.resolve()
    ):
        raise ValueError("campaign Agent Broker launcher shebang drift")


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_content_addressed(path: Path, *, schema: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != schema:
        raise ValueError(f"unsupported TradeLab campaign artifact: {path.name}")
    computed = canonical_sha256(
        {key: item for key, item in value.items() if key != "content_hash"}
    )
    if _normalized_hash(value.get("content_hash")) != computed:
        raise ValueError(f"TradeLab campaign artifact hash mismatch: {path.name}")
    return value


def _campaign_window_preflight(
    config: Mapping[str, Any], *, now: datetime, artifact_root: Path
) -> dict[str, Any] | None:
    """Reject non-campaign clock ticks before any source or lifecycle invocation."""

    local = now.astimezone(CENTRAL) if now.tzinfo else now.replace(tzinfo=CENTRAL)
    target = local.date()
    starts_on = date.fromisoformat(str(config["starts_on"]))
    ends_on = date.fromisoformat(str(config["ends_on"]))
    authorized = tuple(
        date.fromisoformat(item) for item in config["_authorized_session_dates"]
    )
    ordinal = authorized.index(target) + 1 if target in authorized else None
    if starts_on <= target <= ends_on and ordinal is not None and ordinal <= 10:
        return None
    if target < starts_on:
        detail = "before_starts_on"
    elif target > ends_on:
        detail = "after_ends_on"
    elif ordinal is None:
        detail = "non_xnys_session"
    else:
        detail = "max_sessions_elapsed"
    body = {
        "schema": CAMPAIGN_WINDOW_SCHEMA,
        "status": "skipped",
        "reason": "outside_campaign_window",
        "detail": detail,
        "campaign_id": config["campaign_id"],
        "campaign_config_hash": config["content_hash"],
        "campaign_protocol_hash": config["campaign_protocol_hash"],
        "campaign_freeze_receipt_hash": config["campaign_freeze_receipt_hash"],
        "treatment_manifest_hash": config["treatment_manifest_hash"],
        "universe_hash": config["universe_hash"],
        "session_calendar_hash": config["session_calendar_hash"],
        "session_calendar_id": config["session_calendar_id"],
        "session_calendar_version": config["session_calendar_version"],
        "target_date": target.isoformat(),
        "starts_on": config["starts_on"],
        "checkpoint_after_sessions": 5,
        "max_sessions": 10,
        "ends_on": config["ends_on"],
        "session_ordinal": ordinal,
        "effects": {
            "birdclaw": False,
            "cartographer": False,
            "broker": False,
            "orders": False,
            "sheet": False,
        },
    }
    receipt = {**body, "content_hash": canonical_sha256(body)}
    path = require_experiment_path(
        artifact_root / "campaign-window" / f"{target.isoformat()}.json",
        role="campaign window receipt",
    )
    if path.exists():
        prior = json.loads(path.read_text(encoding="utf-8"))
        if prior != receipt:
            raise ValueError("campaign window receipt conflicts with fixed campaign")
    else:
        _write_atomic(path, receipt)
    return receipt


def _prepare_daily_contract(
    config: Mapping[str, Any], *, contract_dir: Path, now: datetime
) -> Path:
    _verify_frozen_toolchain(config)
    local = now.astimezone(CENTRAL) if now.tzinfo else now.replace(tzinfo=CENTRAL)
    target_date = local.date().isoformat()
    preparation_root = require_experiment_path(
        Path(contract_dir).parent / "preparation" / target_date,
        role="daily preparation root",
    )
    attempt = preparation_root / f"attempt-{local.strftime('%H%M%S')}"
    cartographer_output = attempt / "cartographer"
    cartographer_checkout = Path(str(config["market_cartographer_checkout"])).resolve()
    cartographer_runtime = _verify_toolchain_role(config, role="market_cartographer")
    command = [
        *cartographer_runtime["argv_prefix"],
        "market-context-export",
        "--provider",
        str(config["cartographer_provider"]),
        "--symbols",
        ",".join(config["symbols"]),
        "--as-of",
        local.isoformat(),
        "--campaign-id",
        str(config["campaign_id"]),
        "--output",
        str(cartographer_output),
    ]
    if config.get("cartographer_data_root"):
        command.extend(["--data-root", str(config["cartographer_data_root"])])
    env = _sanitized_subprocess_env(role="broker_inert")
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(cartographer_checkout / "src"),
            str(Path(str(config["kernel_src"]))),
            env.get("PYTHONPATH", ""),
        ]
    ).rstrip(os.pathsep)
    completed = _execute_command(command, cwd=cartographer_checkout, env=env)
    _verify_toolchain_role(config, role="market_cartographer")
    _completed_command_result(completed)
    cartographer_receipt = cartographer_output / "receipt.json"
    receipt = json.loads(cartographer_receipt.read_text(encoding="utf-8"))
    if receipt.get("target_session_date") != target_date:
        _record_missed_preparation(contract_dir, now, reason="wrong_target_session")
        raise ValueError(
            "Cartographer preparation did not target today's still-unopened session"
        )
    run_id = str(receipt.get("run_id") or "")
    if not run_id:
        raise ValueError("Cartographer preparation receipt is missing run_id")
    if receipt.get("status") not in {"succeeded", "no_plan"}:
        raise ValueError("Cartographer preparation receipt status is unsupported")
    outcome = "no_plan" if receipt["status"] == "no_plan" else "plan"
    if outcome == "no_plan":
        _validate_cartographer_no_plan_export(
            receipt,
            root=cartographer_output,
            campaign_id=str(config["campaign_id"]),
        )
        birdclaw_export = {"path": None, "packet_hash": None, "output_hash": None}
        narrative_failure = None
    else:
        try:
            birdclaw_export = _export_birdclaw_context(
                config, attempt=attempt, as_of=local
            )
            narrative_failure = None
        except Exception as exc:  # noqa: BLE001 - observational sidecar.
            failure_body = {
                "schema": "bhiksha.chart-scenario-narrative-source-failure.v1",
                "status": "unavailable_non_blocking",
                "as_of": local.isoformat(),
                "error_type": type(exc).__name__,
                "error": "Birdclaw narrative source unavailable",
                "selection_influence": False,
            }
            narrative_failure = {
                **failure_body,
                "content_hash": canonical_sha256(failure_body),
            }
            _write_atomic(attempt / "birdclaw-failure.json", narrative_failure)
            birdclaw_export = {"path": None, "packet_hash": None, "output_hash": None}
    experiment_root = require_experiment_path(
        str(config["tradelab_experiment_root"]), role="TradeLab experiment root"
    )
    tradelab_run = (
        experiment_root / "campaigns" / str(config["campaign_id"]) / "runs" / run_id
    )
    body = {
        "schema": SCHEMA,
        "campaign_id": config["campaign_id"],
        "run_id": run_id,
        "outcome": outcome,
        "target_session_date": target_date,
        "target_session_window": receipt["target_session_window"],
        "target_session_window_hash": receipt["target_session_window_hash"],
        "cartographer_receipt": str(cartographer_receipt),
        "birdclaw_export": (
            str(birdclaw_export["path"]) if birdclaw_export["path"] else None
        ),
        "birdclaw_packet_hash": birdclaw_export["packet_hash"],
        "birdclaw_output_hash": birdclaw_export["output_hash"],
        "narrative_source_failure": narrative_failure,
        "campaign_config_hash": config["content_hash"],
        "campaign_protocol_hash": config["campaign_protocol_hash"],
        "campaign_freeze_receipt_hash": config["campaign_freeze_receipt_hash"],
        "session_calendar_hash": config["session_calendar_hash"],
        "toolchain": config["toolchain"],
        "toolchain_hash": canonical_sha256(config["toolchain"]),
        "tradelab_checkout": config["tradelab_checkout"],
        "tradelab_experiment_root": str(experiment_root),
        "agent_broker": config["agent_broker"],
        "agent_broker_checkout": config["agent_broker_checkout"],
        "spreadsheet_id": config["spreadsheet_id"],
        "kernel_src": config["kernel_src"],
        "plan_source": (
            None
            if outcome == "no_plan"
            else str(tradelab_run / "outputs" / "shadow-plan.json")
        ),
        "projection_request": (
            None
            if outcome == "no_plan"
            else str(tradelab_run / "outputs" / "sheet-upsert-request.json")
        ),
    }
    contract = {**body, "content_hash": canonical_sha256(body)}
    target = require_experiment_path(
        Path(contract_dir) / f"{target_date}.json", role="daily contract"
    )
    _validate_contract(contract, contract_path=target)
    _write_atomic(target, contract)
    return target


def _export_birdclaw_context(
    config: Mapping[str, Any], *, attempt: Path, as_of: datetime
) -> dict[str, Any]:
    checkout = Path(str(config["birdclaw_checkout"])).resolve()
    runtime = _verify_toolchain_role(config, role="birdclaw")
    completed = _execute_command(
        [
            *runtime["argv_prefix"],
            "export",
            "temporal-market-context",
            "--as-of",
            as_of.isoformat(),
            "--json",
        ],
        cwd=checkout,
        env=_sanitized_subprocess_env(
            role="broker_inert", additions={"BIRDCLAW_DB": str(config["birdclaw_db"])}
        ),
    )
    _verify_toolchain_role(config, role="birdclaw")
    if completed.returncode != 0:
        raise RuntimeError(
            "Birdclaw temporal export failed: "
            + (completed.stderr or completed.stdout)[-2000:]
        )
    status = json.loads(completed.stdout)
    if (
        status.get("schema") != "birdclaw.temporal_market_context_export.v1"
        or status.get("packet_schema") != "birdclaw.temporal_market_context_packet.v1"
        or status.get("dry_run") is not False
        or not status.get("output")
    ):
        raise ValueError("Birdclaw temporal export status is not canonical")
    source = (checkout / str(status["output"])).resolve()
    if not source.is_relative_to(checkout):
        raise ValueError("Birdclaw temporal export escaped its checkout")
    payload = json.loads(source.read_text(encoding="utf-8"))
    packet_hash = canonical_sha256(payload.get("packet"))
    output_body = {
        "schema": payload.get("schema"),
        "packet": payload.get("packet"),
        "packet_hash": payload.get("packet_hash"),
    }
    output_hash = canonical_sha256(output_body)
    if (
        payload.get("schema") != "birdclaw.temporal_market_context_export.v1"
        or payload.get("packet", {}).get("schema")
        != "birdclaw.temporal_market_context_packet.v1"
        or payload.get("packet_hash") != packet_hash
        or payload.get("output_hash") != output_hash
        or status.get("packet_hash") != packet_hash
        or status.get("output_hash") != output_hash
    ):
        raise ValueError("Birdclaw temporal export hashes are invalid")
    target = attempt / "birdclaw-temporal-export.json"
    _write_atomic(target, payload)
    return {
        "path": target.resolve(),
        "packet_hash": packet_hash,
        "output_hash": output_hash,
    }


def _record_missed_preparation(
    contract_dir: Path, now: datetime, *, reason: str = "pre_open_cutoff_elapsed"
) -> bool:
    local = now.astimezone(CENTRAL) if now.tzinfo else now.replace(tzinfo=CENTRAL)
    target = require_experiment_path(
        Path(contract_dir).parent / "missed" / f"{local.date().isoformat()}.json",
        role="missed preparation receipt",
    )
    if target.exists():
        return False
    body = {
        "schema": "bhiksha.chart-scenario-missed-preparation.v1",
        "status": "missed_non_comparable",
        "target_session_date": local.date().isoformat(),
        "recorded_at": local.isoformat(),
        "reason": reason,
        "effects": {"broker": False, "orders": False, "authorization": False},
    }
    _write_atomic(target, {**body, "content_hash": canonical_sha256(body)})
    return True


def _require_preopen_completion(
    contract: Mapping[str, Any], paths: Any, *, now: datetime | None = None
) -> None:
    completed_at = now or datetime.now(UTC)
    session_start = datetime.fromisoformat(
        contract["target_session_window"]["start_at"]
    )
    if completed_at < session_start:
        return
    body = {
        "schema": "bhiksha.chart-scenario-late-preparation.v1",
        "status": "late_non_comparable",
        "campaign_id": contract["campaign_id"],
        "run_id": contract["run_id"],
        "campaign_config_hash": contract["campaign_config_hash"],
        "contract_hash": contract["content_hash"],
        "session_start": contract["target_session_window"]["start_at"],
        "completed_at": completed_at.isoformat(),
        "installed": False,
        "effects": {"broker": False, "orders": False, "authorization": False},
    }
    _write_atomic(
        paths.root / "coordinator" / "late-preparation.json",
        {**body, "content_hash": canonical_sha256(body)},
    )
    raise RuntimeError("daily preparation completed after authenticated session open")


def _parse_target_date(value: Any) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError("target_session_date must use YYYY-MM-DD") from exc


def _validate_session_window(
    value: Any, *, target_date: date, expected_hash: Any
) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {
        "start_at",
        "end_at",
        "market_timezone",
    }:
        raise ValueError("target_session_window must have exact v1 fields")
    window = {key: str(item) for key, item in value.items()}
    try:
        start = datetime.fromisoformat(window["start_at"])
        end = datetime.fromisoformat(window["end_at"])
        market_tz = ZoneInfo(window["market_timezone"])
    except (ValueError, KeyError) as exc:
        raise ValueError(
            "target_session_window timestamps/timezone are invalid"
        ) from exc
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError("target_session_window must be aware and increasing")
    if start.astimezone(market_tz).date() != target_date:
        raise ValueError(
            "target_session_window start does not match target session date"
        )
    computed = canonical_sha256(window)
    if str(expected_hash or "").removeprefix("sha256:") != computed:
        raise ValueError("target_session_window hash mismatch")
    return window


def _validate_cartographer_receipt(
    contract: Mapping[str, Any], *, window: Mapping[str, str]
) -> None:
    receipt = json.loads(
        Path(str(contract["cartographer_receipt"])).read_text(encoding="utf-8")
    )
    if receipt.get("schema") != "market_cartographer.market_context_receipt.v2":
        raise ValueError("daily contract requires Cartographer receipt v2")
    receipt_hash = canonical_sha256(
        {key: item for key, item in receipt.items() if key != "receipt_hash"}
    )
    if str(receipt.get("receipt_hash", "")).removeprefix("sha256:") != receipt_hash:
        raise ValueError("Cartographer receipt hash mismatch")
    expected_status = "no_plan" if contract["outcome"] == "no_plan" else "succeeded"
    if (
        receipt.get("status") != expected_status
        or receipt.get("run_id") != contract["run_id"]
        or receipt.get("target_session_date") != contract["target_session_date"]
        or receipt.get("target_session_window") != dict(window)
        or str(receipt.get("target_session_window_hash", "")).removeprefix("sha256:")
        != str(contract["target_session_window_hash"]).removeprefix("sha256:")
    ):
        raise ValueError(
            "daily contract does not match authenticated Cartographer session"
        )
    if expected_status == "no_plan":
        _validate_cartographer_no_plan_export(
            receipt,
            root=Path(str(contract["cartographer_receipt"])).parent,
            campaign_id=str(contract["campaign_id"]),
        )


def _validate_cartographer_no_plan_export(
    receipt: Mapping[str, Any], *, root: Path, campaign_id: str
) -> None:
    expected_receipt_fields = {
        "schema",
        "status",
        "run_id",
        "export_id",
        "export_hash",
        "target_session_date",
        "target_session_window",
        "target_session_window_hash",
        "data_mode",
        "candidate_pool_hash",
        "arm_a_selection_hash",
        "materialized_scenario_count",
        "artifacts",
        "effects",
        "receipt_hash",
    }
    false_effects = {
        "broker": False,
        "orders": False,
        "auth": False,
        "schedule": False,
        "external_send": False,
    }
    body = {key: item for key, item in receipt.items() if key != "receipt_hash"}
    if (
        set(receipt) != expected_receipt_fields
        or receipt.get("status") != "no_plan"
        or receipt.get("candidate_pool_hash") is not None
        or receipt.get("arm_a_selection_hash") is not None
        or receipt.get("materialized_scenario_count") != 0
        or receipt.get("effects") != false_effects
        or receipt.get("receipt_hash") != canonical_sha256(body)
    ):
        raise ValueError("Cartographer no-plan receipt is invalid")
    export_root = root.expanduser().resolve()
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list):
        raise TypeError("Cartographer no-plan artifact inventory is invalid")
    expected_paths: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, Mapping) or set(artifact) != {
            "path",
            "content_hash",
        }:
            raise ValueError("Cartographer no-plan artifact record is invalid")
        relative = Path(str(artifact["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Cartographer no-plan artifact path escaped export")
        requested_path = export_root / relative
        path = requested_path.resolve()
        if (
            not path.is_relative_to(export_root)
            or requested_path.is_symlink()
            or any(
                parent.is_symlink()
                for parent in requested_path.parents
                if parent.is_relative_to(export_root)
            )
            or not path.is_file()
        ):
            raise ValueError("Cartographer no-plan artifact is unavailable")
        payload = json.loads(path.read_text(encoding="utf-8"))
        observed_hash = (
            payload.get("export_hash")
            if relative.as_posix() == "manifest.json"
            else canonical_sha256(payload)
        )
        if artifact["content_hash"] != observed_hash:
            raise ValueError("Cartographer no-plan artifact hash mismatch")
        expected_paths.add(relative.as_posix())
    members = list(export_root.rglob("*"))
    if any(path.is_symlink() for path in members):
        raise ValueError("Cartographer no-plan export contains a symlink")
    actual_paths = {
        path.relative_to(export_root).as_posix()
        for path in members
        if path.is_file() and path.name != "receipt.json"
    }
    if actual_paths != expected_paths or "manifest.json" not in expected_paths:
        raise ValueError("Cartographer no-plan artifact inventory is not exact")
    manifest = json.loads((export_root / "manifest.json").read_text(encoding="utf-8"))
    manifest_body = {
        key: item
        for key, item in manifest.items()
        if key not in {"export_id", "export_hash"}
    }
    manifest_hash = canonical_sha256(manifest_body)
    if (
        manifest.get("schema") != "market_cartographer.market_context_no_plan.v1"
        or manifest.get("status") != "no_plan"
        or manifest.get("reason") != "all_symbols_quarantined"
        or manifest.get("campaign_id") != campaign_id
        or manifest.get("run_id") != receipt["run_id"]
        or manifest.get("target_session_date") != receipt["target_session_date"]
        or manifest.get("target_session_window") != receipt["target_session_window"]
        or manifest.get("target_session_window_hash")
        != receipt["target_session_window_hash"]
        or manifest.get("effects") != false_effects
        or manifest.get("export_hash") != manifest_hash
        or manifest.get("export_hash") != receipt["export_hash"]
        or manifest.get("export_id") != f"no-plan:{manifest_hash[:16]}"
        or manifest.get("export_id") != receipt["export_id"]
    ):
        raise ValueError("Cartographer no-plan manifest identity is invalid")


def _validate_tradelab_no_plan_receipt(
    value: Any, *, contract: Mapping[str, Any]
) -> dict[str, Any]:
    expected = {
        "schema",
        "status",
        "reason",
        "campaign_id",
        "run_id",
        "run_root",
        "run_manifest_hash",
        "target_session_date",
        "target_session_window",
        "target_session_window_hash",
        "cartographer_receipt_hash",
        "cartographer_export_hash",
        "chart_input_verification_hash",
        "shadow_plan_hash",
        "projection_receipt_hash",
        "effects",
        "content_hash",
    }
    false_effects = {
        "sheet_write": False,
        "broker": False,
        "orders": False,
        "auth_mutation": False,
        "live_plan": False,
        "schedule": False,
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("TradeLab no-plan preparation receipt fields are invalid")
    body = {key: item for key, item in value.items() if key != "content_hash"}
    cartographer = json.loads(
        Path(str(contract["cartographer_receipt"])).read_text(encoding="utf-8")
    )
    run_root = (
        Path(str(contract["tradelab_experiment_root"])).resolve()
        / "campaigns"
        / str(contract["campaign_id"])
        / "runs"
        / str(contract["run_id"])
    )
    if (
        value.get("schema") != "tradelab.market_context_daily_preparation_receipt.v1"
        or value.get("status") != "no_plan"
        or value.get("reason") != "all_symbols_quarantined"
        or value.get("campaign_id") != contract["campaign_id"]
        or value.get("run_id") != contract["run_id"]
        or Path(str(value.get("run_root"))).resolve() != run_root
        or value.get("target_session_date") != contract["target_session_date"]
        or value.get("target_session_window") != contract["target_session_window"]
        or value.get("target_session_window_hash")
        != contract["target_session_window_hash"]
        or str(value.get("cartographer_receipt_hash", "")).removeprefix("sha256:")
        != str(cartographer["receipt_hash"]).removeprefix("sha256:")
        or str(value.get("cartographer_export_hash", "")).removeprefix("sha256:")
        != str(cartographer["export_hash"]).removeprefix("sha256:")
        or re.fullmatch(
            r"(?:sha256:)?[0-9a-f]{64}",
            str(value.get("chart_input_verification_hash", "")),
        )
        is None
        or re.fullmatch(
            r"(?:sha256:)?[0-9a-f]{64}", str(value.get("run_manifest_hash", ""))
        )
        is None
        or value.get("shadow_plan_hash") is not None
        or value.get("projection_receipt_hash") is not None
        or value.get("effects") != false_effects
        or str(value.get("content_hash", "")).removeprefix("sha256:")
        != canonical_sha256(body)
    ):
        raise ValueError("TradeLab no-plan preparation receipt identity is invalid")
    persisted = run_root / "outputs" / "preparation-receipt.json"
    if not persisted.is_file() or json.loads(
        persisted.read_text(encoding="utf-8")
    ) != dict(value):
        raise ValueError(
            "TradeLab no-plan preparation receipt was not persisted exactly"
        )
    return dict(value)


def _validate_no_plan_coordinator_receipt(
    value: Mapping[str, Any], *, contract: Mapping[str, Any]
) -> None:
    body = {key: item for key, item in value.items() if key != "content_hash"}
    if (
        value.get("schema") != RECEIPT_SCHEMA
        or value.get("status") != "succeeded"
        or value.get("reason") != "authenticated_no_plan"
        or value.get("outcome") != "no_plan"
        or value.get("campaign_id") != contract["campaign_id"]
        or value.get("run_id") != contract["run_id"]
        or value.get("contract_hash") != contract["content_hash"]
        or value.get("content_hash") != canonical_sha256(body)
        or value.get("effects")
        != {
            "broker": False,
            "orders": False,
            "authorization": False,
            "sheet": False,
            "plan_install": False,
        }
    ):
        raise ValueError("existing no-plan coordinator receipt conflicts with contract")


def _validate_birdclaw_export(
    contract: Mapping[str, Any], *, window: Mapping[str, str]
) -> None:
    if contract["outcome"] == "no_plan":
        if any(
            contract.get(field) is not None
            for field in (
                "birdclaw_export",
                "birdclaw_packet_hash",
                "birdclaw_output_hash",
                "narrative_source_failure",
            )
        ):
            raise ValueError("no-plan contract cannot run or bind narrative evidence")
        return
    if contract.get("birdclaw_export") is None:
        failure = contract.get("narrative_source_failure")
        if (
            contract.get("birdclaw_packet_hash") is not None
            or contract.get("birdclaw_output_hash") is not None
            or not isinstance(failure, Mapping)
            or failure.get("schema")
            != "bhiksha.chart-scenario-narrative-source-failure.v1"
            or failure.get("selection_influence") is not False
            or failure.get("content_hash")
            != canonical_sha256(
                {key: item for key, item in failure.items() if key != "content_hash"}
            )
        ):
            raise ValueError("daily contract narrative degradation receipt is invalid")
        return
    if contract.get("narrative_source_failure") is not None:
        raise ValueError("daily contract cannot have Birdclaw success and failure")
    payload = json.loads(
        Path(str(contract["birdclaw_export"])).read_text(encoding="utf-8")
    )
    packet = payload.get("packet")
    packet_hash = canonical_sha256(packet)
    output_hash = canonical_sha256(
        {
            "schema": payload.get("schema"),
            "packet": packet,
            "packet_hash": payload.get("packet_hash"),
        }
    )
    try:
        cutoff = datetime.fromisoformat(str(packet["as_of"]))
        session_start = datetime.fromisoformat(window["start_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Birdclaw temporal export cutoff is invalid") from exc
    if (
        payload.get("schema") != "birdclaw.temporal_market_context_export.v1"
        or not isinstance(packet, Mapping)
        or packet.get("schema") != "birdclaw.temporal_market_context_packet.v1"
        or payload.get("packet_hash") != packet_hash
        or payload.get("output_hash") != output_hash
        or contract["birdclaw_packet_hash"] != packet_hash
        or contract["birdclaw_output_hash"] != output_hash
        or cutoff >= session_start
    ):
        raise ValueError("daily contract Birdclaw temporal export is invalid")


def _validate_tradelab_paths(contract: Mapping[str, Any]) -> None:
    root = require_experiment_path(
        str(contract["tradelab_experiment_root"]), role="TradeLab experiment root"
    )
    run_root = (
        root
        / "campaigns"
        / str(contract["campaign_id"])
        / "runs"
        / str(contract["run_id"])
    )
    expected = {
        "plan_source": run_root / "outputs" / "shadow-plan.json",
        "projection_request": run_root / "outputs" / "sheet-upsert-request.json",
    }
    if contract["outcome"] == "no_plan":
        if (
            contract.get("plan_source") is not None
            or contract.get("projection_request") is not None
        ):
            raise ValueError("no-plan contract cannot bind plan or Sheet outputs")
        return
    for field, path in expected.items():
        if Path(str(contract[field])).expanduser().resolve() != path.resolve():
            raise ValueError(f"daily contract {field} is not the fixed run-scoped path")


def _stage_tradelab_evidence(contract: Mapping[str, Any], paths: Any) -> None:
    _validate_staging_source_paths(paths)
    plan = read_installed_plan(paths.plan)
    if (
        plan.run_manifest.get("campaign_id") != contract["campaign_id"]
        or plan.run_manifest.get("run_id") != contract["run_id"]
    ):
        raise ValueError("installed plan differs from staging contract")
    install_receipt = _validate_install_receipt(paths, plan=plan)
    cycles = _validate_cycle_artifacts(paths, plan=plan)
    events = _validate_events_export(paths.events_export)
    covered_event_hashes = [
        event_hash
        for _cycle, receipt, _input_path, _receipt_path in cycles
        for evidence in receipt["durable_slot_evidence"]
        for event_hash in evidence["event_hashes"]
    ]
    _validate_staging_event_partition(
        events["events"], covered_event_hashes=covered_event_hashes, plan=plan
    )

    root = require_experiment_path(
        str(contract["tradelab_experiment_root"]), role="TradeLab experiment root"
    )
    run_root = (
        root
        / "campaigns"
        / str(contract["campaign_id"])
        / "runs"
        / str(contract["run_id"])
    )
    group_body = {
        "schema": "bhiksha.chart-scenario-staging-group.v1",
        "plan_hash": plan.plan_hash,
        "install_receipt_hash": install_receipt["receipt_hash"],
        "cycle_input_hashes": [cycle["content_hash"] for cycle, *_ in cycles],
        "cycle_receipt_hashes": [receipt["receipt_hash"] for _, receipt, *_ in cycles],
        "events_export_hash": events["content_hash"],
    }
    group = {**group_body, "content_hash": canonical_sha256(group_body)}
    generations = require_experiment_path(
        run_root / ".bhiksha-staging", role="TradeLab staging generations"
    )
    if generations.parent != run_root.resolve():
        raise ValueError("TradeLab staging generations escaped the exact run root")
    generations.mkdir(parents=True, exist_ok=True)
    final_generation = generations / group["content_hash"]
    expected_generation: dict[str, Mapping[str, Any]] = {
        "install-receipt.json": install_receipt,
        "events.json": events,
        "group-manifest.json": group,
    }
    for ordinal, (cycle, receipt, _input, _receipt) in enumerate(cycles, 1):
        expected_generation[f"cycle-inputs/cycle-{ordinal:04d}.json"] = cycle
        expected_generation[f"cycle-receipts/cycle-{ordinal:04d}.receipt.json"] = (
            receipt
        )
    if final_generation.is_symlink():
        raise ValueError("TradeLab staging generation cannot be a symlink")
    if final_generation.exists():
        _validate_existing_staging_generation(
            final_generation, expected=expected_generation
        )
    else:
        temporary = Path(tempfile.mkdtemp(prefix=".generation-", dir=generations))
        try:
            for relative, payload in expected_generation.items():
                _write_atomic(temporary / relative, payload)
            os.replace(temporary, final_generation)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        _validate_existing_staging_generation(
            final_generation, expected=expected_generation
        )
    staging = run_root / "bhiksha"
    if staging.exists() and not staging.is_symlink():
        raise ValueError("TradeLab staging target must be an atomic generation link")
    link = run_root / f".bhiksha-link-{group['content_hash'][:16]}"
    try:
        if link.exists() or link.is_symlink():
            if not link.is_symlink():
                raise ValueError("TradeLab staging temporary link path is occupied")
            link.unlink()
        os.symlink(os.path.relpath(final_generation, run_root), link)
        os.replace(link, staging)
    finally:
        link.unlink(missing_ok=True)


def _validate_staging_source_paths(paths: Any) -> None:
    root = require_experiment_path(paths.root, role="Bhiksha staging source run")
    expected = {
        "plan": root / "active_shadow_plan.json",
        "install_receipt": root / "install.receipt.json",
        "cycle_inputs": root / "cycle-inputs",
        "cycle_receipts": root / "cycles",
        "events_export": root / "events.json",
    }
    for field, exact in expected.items():
        supplied = Path(getattr(paths, field)).expanduser().resolve()
        if supplied != exact.resolve():
            raise ValueError(
                f"Bhiksha staging {field} is not the exact run artifact path"
            )


def _validate_existing_staging_generation(
    generation: Path, *, expected: Mapping[str, Mapping[str, Any]]
) -> None:
    if generation.is_symlink() or not generation.is_dir():
        raise ValueError("TradeLab staging generation is not an immutable directory")
    members = list(generation.rglob("*"))
    if any(member.is_symlink() for member in members):
        raise ValueError("TradeLab staging generation contains a symlink")
    files = {
        member.relative_to(generation).as_posix(): member
        for member in members
        if member.is_file()
    }
    expected_directories = {
        str(Path(relative).parent)
        for relative in expected
        if str(Path(relative).parent) != "."
    }
    directories = {
        member.relative_to(generation).as_posix()
        for member in members
        if member.is_dir()
    }
    if set(files) != set(expected) or directories != expected_directories:
        raise ValueError("TradeLab staging generation members are not exact")
    for relative, payload in expected.items():
        if json.loads(files[relative].read_text(encoding="utf-8")) != dict(payload):
            raise ValueError("TradeLab staging generation content drift")


def _validate_install_receipt(paths: Any, *, plan: ShadowPlan) -> dict[str, Any]:
    receipt = json.loads(paths.install_receipt.read_text(encoding="utf-8"))
    expected_fields = {
        "receipt_schema_version",
        "receipt_id",
        "status",
        "created_at",
        "artifact_path",
        "receipt_path",
        "input_sha256",
        "plan_id",
        "plan_hash",
        "run_manifest_hash",
        "treatment_manifest_hash",
        "cartographer_receipt_hash",
        "cartographer_export_hash",
        "option_selection_policy_hash",
        "arm_b_selector_receipt_hash",
        "target_session_date",
        "target_session_window_hash",
        "scenario_count",
        "scenario_hashes",
        "identities",
        "component_manifest_hash",
        "candidate_pool_hash",
        "broker_effect_count",
        "receipt_hash",
    }
    if set(receipt) != expected_fields:
        raise ValueError("install receipt must declare exact fields")
    body = {key: item for key, item in receipt.items() if key != "receipt_hash"}
    expected_identities = [
        {
            "program_id": scenario.program_id,
            "experiment_family_id": scenario.experiment_family_id,
            "experiment_version": scenario.experiment_version,
            "campaign_id": scenario.campaign_id,
            "run_id": scenario.run_id,
            "arm_id": scenario.arm_id.value,
            "scenario_id": scenario.scenario_id,
            "candidate_id": scenario.candidate_id,
            "symbol": scenario.symbol,
            "direction": scenario.direction.value,
            "thesis_class": scenario.thesis_class.value,
            "scenario_hash": scenario.scenario_hash,
            "candidate_pool_hash": scenario.candidate_pool_hash,
            "selection_packet_hash": scenario.selection_packet_hash,
            "component_manifest_hash": scenario.component_manifest_hash,
            "chart_evidence_hashes": [
                item.evidence_hash for item in scenario.chart_evidence_refs
            ],
            "exit_policy_hash": scenario.exit_policy_hash,
        }
        for scenario in plan.scenarios
    ]
    if (
        receipt["receipt_schema_version"] != INSTALL_RECEIPT_SCHEMA_VERSION
        or receipt["status"] != "installed"
        or receipt["receipt_id"] != "install-" + plan.plan_hash[:32]
        or Path(receipt["artifact_path"]).resolve() != paths.plan.resolve()
        or Path(receipt["receipt_path"]).resolve() != paths.install_receipt.resolve()
        or re.fullmatch(r"[0-9a-f]{64}", str(receipt["input_sha256"])) is None
        or receipt["plan_id"] != plan.plan_id
        or receipt["plan_hash"] != plan.plan_hash
        or str(receipt["run_manifest_hash"]).removeprefix("sha256:")
        != plan.run_manifest_hash.removeprefix("sha256:")
        or str(receipt["treatment_manifest_hash"]).removeprefix("sha256:")
        != plan.treatment_manifest_hash.removeprefix("sha256:")
        or receipt["cartographer_receipt_hash"] != plan.cartographer_receipt_hash
        or receipt["cartographer_export_hash"] != plan.cartographer_export_hash
        or receipt["option_selection_policy_hash"]
        != plan.option_selection_policy.content_hash
        or receipt["arm_b_selector_receipt_hash"] != plan.arm_b_selector_receipt_hash
        or receipt["target_session_date"] != plan.target_session_date
        or receipt["target_session_window_hash"] != plan.target_session_window_hash
        or receipt["scenario_count"] != len(plan.scenarios)
        or receipt["scenario_hashes"]
        != [scenario.scenario_hash for scenario in plan.scenarios]
        or receipt["identities"] != expected_identities
        or receipt["component_manifest_hash"] != plan.component_manifest_hash
        or receipt["candidate_pool_hash"] != plan.candidate_pool.pool_hash
        or receipt["broker_effect_count"] != 0
        or receipt["receipt_hash"] != canonical_sha256(body)
    ):
        raise ValueError("install receipt differs from installed plan")
    return receipt


def _validate_staging_event_partition(
    events: list[dict[str, Any]],
    *,
    covered_event_hashes: list[str],
    plan: ShadowPlan,
) -> None:
    """Partition the global chain into install lifecycle and slot evidence."""

    covered = Counter(covered_event_hashes)
    exported_non_install = Counter(
        event["event_hash"] for event in events if event["event_type"] != "installed"
    )
    if covered != exported_non_install or any(count != 1 for count in covered.values()):
        raise ValueError(
            "non-installed events must be covered exactly once by durable slot evidence"
        )
    installed = [event for event in events if event["event_type"] == "installed"]
    scenario_by_id = {scenario.scenario_id: scenario for scenario in plan.scenarios}
    if len(installed) != len(scenario_by_id):
        raise ValueError("event export must contain one installed event per scenario")
    seen_scenarios: set[str] = set()
    first_index_by_scenario: dict[str, int] = {}
    for index, event in enumerate(events):
        first_index_by_scenario.setdefault(str(event["scenario_id"]), index)
    for event in installed:
        scenario = scenario_by_id.get(str(event["scenario_id"]))
        if scenario is None or scenario.scenario_id in seen_scenarios:
            raise ValueError("installed event scenario coverage is invalid")
        seen_scenarios.add(scenario.scenario_id)
        if (
            first_index_by_scenario[scenario.scenario_id] != events.index(event)
            or event["program_id"] != scenario.program_id
            or event["experiment_family_id"] != scenario.experiment_family_id
            or event["experiment_version"] != scenario.experiment_version
            or event["campaign_id"] != scenario.campaign_id
            or event["run_id"] != scenario.run_id
            or event["arm_id"] != scenario.arm_id.value
            or event["scenario_hash"] != scenario.scenario_hash
            or event["implementation_hash"] != scenario.component_manifest_hash
            or event["event_time"]
            != scenario.observation_window.start_at.isoformat().replace("+00:00", "Z")
            or event["market_observation_id"]
            != "install-" + scenario.scenario_hash[:24]
            or event["broker_effect_count"] != 0
            or event["authorization_mode"] != "shadow"
            or event["source_type"] != "chart_scenario_experiment"
            or event["details"].get("status") != "installed"
            or event["details"].get("reason") != "validated_shadow_plan"
            or event["details"].get("plan_hash") != plan.plan_hash
            or str(event["details"].get("run_manifest_hash")).removeprefix("sha256:")
            != plan.run_manifest_hash.removeprefix("sha256:")
            or str(event["details"].get("treatment_manifest_hash")).removeprefix(
                "sha256:"
            )
            != plan.treatment_manifest_hash.removeprefix("sha256:")
        ):
            raise ValueError("installed event differs from installed plan")


def _validate_cycle_artifacts(
    paths: Any, *, plan: ShadowPlan, allow_pending_input: bool = False
) -> list[tuple[dict[str, Any], dict[str, Any], Path, Path]]:
    input_paths = (
        sorted(paths.cycle_inputs.glob("slot-*")) if paths.cycle_inputs.exists() else []
    )
    receipt_paths = (
        sorted(paths.cycle_receipts.glob("slot-*.receipt.json"))
        if paths.cycle_receipts.exists()
        else []
    )
    pending = len(input_paths) == len(receipt_paths) + 1
    if len(input_paths) != len(receipt_paths) and not (allow_pending_input and pending):
        raise ValueError("cycle input/receipt cardinality mismatch")
    validated = []
    for ordinal, (input_path, receipt_path) in enumerate(
        zip(input_paths, receipt_paths, strict=True), 1
    ):
        if input_path.name != f"slot-{ordinal:04d}.cycle-input.json":
            raise ValueError("cycle input filenames are not exact and contiguous")
        if receipt_path.name != f"slot-{ordinal:04d}.receipt.json":
            raise ValueError("cycle receipt filenames are not exact and contiguous")
        raw_cycle = json.loads(input_path.read_text(encoding="utf-8"))
        cycle = validate_cycle_input(raw_cycle, plan=plan)
        receipt = _validate_cycle_receipt(
            json.loads(receipt_path.read_text(encoding="utf-8")),
            cycle=cycle,
            plan=plan,
            input_path=input_path,
            run_root=paths.root,
        )
        if cycle["observation_slot_ordinal"] != ordinal:
            raise ValueError("cycle input ordinal is not contiguous")
        validated.append((raw_cycle, receipt, input_path, receipt_path))
    if pending:
        pending_path = input_paths[-1]
        expected = len(receipt_paths) + 1
        if pending_path.name != f"slot-{expected:04d}.cycle-input.json":
            raise ValueError("pending cycle input filename is not exact")
        pending_cycle = validate_cycle_input(
            json.loads(pending_path.read_text(encoding="utf-8")), plan=plan
        )
        if pending_cycle["observation_slot_ordinal"] != expected:
            raise ValueError("pending cycle input ordinal is not contiguous")
    return validated


def _validate_cycle_receipt(
    receipt: Mapping[str, Any],
    *,
    cycle: Mapping[str, Any],
    plan: ShadowPlan,
    input_path: Path,
    run_root: Path,
) -> dict[str, Any]:
    expected_fields = {
        "schema_version",
        "status",
        "created_at",
        "plan_hash",
        "run_manifest_hash",
        "treatment_manifest_hash",
        "cycle_input_hash",
        "cycle_input_artifact_path",
        "cycle_input_artifact_hash",
        "observation_slot_ordinal",
        "evaluated_at",
        "scenario_count",
        "paired_fact_proof_count",
        "paired_fact_proofs",
        "durable_slot_evidence",
        "proof_required_candidate_ids",
        "terminal_carryforwards",
        "candidate_diagnostics",
        "results",
        "errors",
        "broker_effect_count",
        "auth",
        "effects",
        "receipt_hash",
    }
    if set(receipt) != expected_fields:
        raise ValueError("cycle receipt must declare exact fields")
    body = {key: item for key, item in receipt.items() if key != "receipt_hash"}
    false_effects = {
        "broker": False,
        "orders": False,
        "auth_mutation": False,
        "schedule": False,
        "external_send": False,
    }
    expected_auth = {
        "read": plan.option_selection_policy.provider_id == "schwab",
        "mutation": False,
        "token_refresh": False,
        "token_persist": False,
    }
    if (
        receipt["schema_version"] != CYCLE_RECEIPT_SCHEMA
        or receipt["status"] != "succeeded"
        or receipt["errors"] != []
        or receipt["created_at"] != cycle["evaluated_at"]
        or receipt["evaluated_at"] != cycle["evaluated_at"]
        or receipt["plan_hash"] != plan.plan_hash
        or str(receipt["run_manifest_hash"]).removeprefix("sha256:")
        != plan.run_manifest_hash.removeprefix("sha256:")
        or str(receipt["treatment_manifest_hash"]).removeprefix("sha256:")
        != plan.treatment_manifest_hash.removeprefix("sha256:")
        or receipt["cycle_input_hash"] != cycle["content_hash"]
        or Path(receipt["cycle_input_artifact_path"]).resolve() != input_path.resolve()
        or input_path.parent.resolve() != (run_root / "cycle-inputs").resolve()
        or not input_path.resolve().is_relative_to(run_root.resolve())
        or receipt["cycle_input_artifact_hash"] != canonical_sha256(cycle)
        or receipt["observation_slot_ordinal"] != cycle["observation_slot_ordinal"]
        or receipt["scenario_count"] != len(plan.scenarios)
        or receipt["broker_effect_count"] != 0
        or receipt["auth"] != expected_auth
        or receipt["effects"] != false_effects
        or receipt["receipt_hash"] != canonical_sha256(body)
    ):
        raise ValueError("cycle receipt identity or effects are invalid")
    scenario_by_id = {scenario.scenario_id: scenario for scenario in plan.scenarios}
    results = receipt["results"]
    if not isinstance(results, list) or {
        item.get("scenario_id") for item in results if isinstance(item, Mapping)
    } != set(scenario_by_id):
        raise ValueError("cycle receipt results do not exactly cover plan scenarios")
    durable = receipt["durable_slot_evidence"]
    if not isinstance(durable, list):
        raise TypeError("cycle durable evidence must be an array")
    events_by_scenario: dict[str, list[dict[str, Any]]] = {}
    for evidence in durable:
        evidence_body = {
            key: item for key, item in evidence.items() if key != "content_hash"
        }
        if (
            set(evidence)
            != {
                "schema",
                "campaign_id",
                "run_id",
                "candidate_id",
                "slot_ordinal",
                "slot_id",
                "facts_hash",
                "paired",
                "paired_proof_hash",
                "scenario_ids",
                "event_count",
                "event_hashes",
                "events",
                "content_hash",
            }
            or evidence["schema"] != "bhiksha.chart-scenario-durable-slot-evidence.v1"
            or evidence["campaign_id"] != plan.run_manifest["campaign_id"]
            or evidence["run_id"] != plan.run_manifest["run_id"]
            or evidence["slot_ordinal"] != cycle["observation_slot_ordinal"]
            or evidence["candidate_id"] not in cycle["candidates"]
            or evidence["slot_id"]
            != canonical_observation_slot_id(
                run_manifest_hash=plan.run_manifest_hash,
                ordinal=cycle["observation_slot_ordinal"],
            )
            or evidence["paired"] is not True
            or evidence["event_count"] != len(evidence["events"])
            or evidence["event_hashes"]
            != [event["event_hash"] for event in evidence["events"]]
            or evidence["content_hash"] != canonical_sha256(evidence_body)
        ):
            raise ValueError("cycle durable slot evidence is invalid")
        expected_scenario_ids = sorted(
            scenario.scenario_id
            for scenario in plan.scenarios
            if scenario.candidate_id == evidence["candidate_id"]
        )
        if evidence["scenario_ids"] != expected_scenario_ids:
            raise ValueError("durable evidence scenario identities are invalid")
        for event in evidence["events"]:
            event_body = dict(event)
            claimed_event_hash = event_body.pop("event_hash", None)
            sealed_event = ScenarioShadowEvent.model_validate(event_body)
            if (
                claimed_event_hash != sealed_event.event_hash
                or sealed_event.scenario_id not in evidence["scenario_ids"]
                or sealed_event.market_observation_id != evidence["slot_id"]
            ):
                raise ValueError("durable event identity differs from slot evidence")
            events_by_scenario.setdefault(sealed_event.scenario_id, []).append(event)
    for result in results:
        if (
            set(result)
            != {
                "scenario_id",
                "status",
                "terminal",
                "new_event_count",
                "events",
                "broker_effect_count",
                "error",
            }
            or result["broker_effect_count"] != 0
            or result["error"] is not None
            or result["events"] != events_by_scenario.get(result["scenario_id"], [])
            or result["new_event_count"] != len(result["events"])
        ):
            raise ValueError("cycle scenario result differs from durable evidence")
    proofs = receipt["paired_fact_proofs"]
    if receipt["paired_fact_proof_count"] != len(proofs):
        raise ValueError("cycle paired proof count mismatch")
    for proof in proofs:
        proof_body = {key: item for key, item in proof.items() if key != "proof_hash"}
        if (
            proof.get("paired") is not True
            or proof.get("slot_ordinal") != cycle["observation_slot_ordinal"]
            or proof.get("plan_hash") != plan.plan_hash
            or proof.get("candidate_id") not in cycle["candidates"]
            or proof.get("run_id") != plan.run_manifest["run_id"]
            or proof.get("treatment_manifest_hash")
            != plan.treatment_manifest_hash.removeprefix("sha256:")
            or proof.get("proof_hash") != canonical_sha256(proof_body)
        ):
            raise ValueError("cycle paired fact proof is invalid")
    if set(receipt["candidate_diagnostics"]) != set(cycle["candidates"]):
        raise ValueError("cycle diagnostics do not exactly cover candidates")
    required = receipt["proof_required_candidate_ids"]
    carried = [item.get("candidate_id") for item in receipt["terminal_carryforwards"]]
    if (
        not isinstance(required, list)
        or len(required) != len(set(required))
        or set(required) != {proof["candidate_id"] for proof in proofs}
        or set(required) != {evidence["candidate_id"] for evidence in durable}
        or set(required).intersection(carried)
        or set(required).union(carried) != set(cycle["candidates"])
    ):
        raise ValueError("cycle proof/carryforward candidate coverage is invalid")
    proof_by_candidate = {proof["candidate_id"]: proof for proof in proofs}
    if any(
        evidence["facts_hash"]
        != proof_by_candidate[evidence["candidate_id"]]["facts_hash"]
        or evidence["paired_proof_hash"]
        != proof_by_candidate[evidence["candidate_id"]]["proof_hash"]
        for evidence in durable
    ):
        raise ValueError("durable evidence differs from paired fact proof")
    return dict(receipt)


def _validate_events_export(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if set(value) != {
        "schema",
        "event_count",
        "last_event_hash",
        "events",
        "content_hash",
    }:
        raise ValueError("event export must declare exact fields")
    body = {key: item for key, item in value.items() if key != "content_hash"}
    previous = None
    for raw in value["events"]:
        if not isinstance(raw, Mapping):
            raise TypeError("event export entries must be objects")
        event_body = dict(raw)
        claimed_event_hash = event_body.pop("event_hash", None)
        event = ScenarioShadowEvent.model_validate(event_body)
        if claimed_event_hash != event.event_hash:
            raise ValueError("event export event_hash is invalid")
        if event.preceding_event_hash != previous:
            raise ValueError("event export predecessor chain is invalid")
        previous = event.event_hash
    if (
        value["schema"] != "bhiksha.chart-scenario-events-export.v1"
        or value["event_count"] != len(value["events"])
        or value["last_event_hash"] != previous
        or value["content_hash"] != canonical_sha256(body)
    ):
        raise ValueError("event export identity/hash is invalid")
    return value


def _install_or_verify_plan(contract: Mapping[str, Any], paths: Any) -> dict[str, Any]:
    source_payload = json.loads(
        Path(contract["plan_source"]).read_text(encoding="utf-8")
    )
    source_plan = validate_bundle(source_payload)
    if (
        source_plan.run_manifest.get("campaign_id") != contract["campaign_id"]
        or source_plan.run_manifest.get("run_id") != contract["run_id"]
        or source_plan.target_session_date != contract["target_session_date"]
        or source_plan.cartographer_receipt.get("target_session_window")
        != contract["target_session_window"]
    ):
        raise ValueError("shadow plan identity/session does not match daily contract")
    if paths.plan.exists() or paths.install_receipt.exists():
        if not paths.plan.is_file() or not paths.install_receipt.is_file():
            raise ValueError("partial prior shadow-plan installation requires review")
        installed = read_installed_plan(paths.plan)
        receipt = json.loads(paths.install_receipt.read_text(encoding="utf-8"))
        receipt_hash = canonical_sha256(
            {key: item for key, item in receipt.items() if key != "receipt_hash"}
        )
        if (
            installed.plan_hash != source_plan.plan_hash
            or receipt.get("status") != "installed"
            or receipt.get("plan_hash") != source_plan.plan_hash
            or receipt.get("receipt_hash") != receipt_hash
        ):
            raise ValueError("existing run is not an exact idempotent plan replay")
        return {
            "action": "install_shadow_plan",
            "status": "skipped",
            "reason": "exact_idempotent_replay",
            "receipt_hash": receipt_hash,
        }
    install = install_shadow_plan(
        source_payload,
        output_path=paths.plan,
        receipt_path=paths.install_receipt,
    )
    return {
        "action": "install_shadow_plan",
        "status": "succeeded",
        "receipt_hash": install["receipt_hash"],
    }


def _completed_phase_receipt(
    paths: Any, contract: Mapping[str, Any], *, phase: str
) -> dict[str, Any] | None:
    path = paths.root / "coordinator" / f"{phase}.complete.json"
    if not path.exists():
        return None
    receipt = json.loads(path.read_text(encoding="utf-8"))
    content_hash = canonical_sha256(
        {key: item for key, item in receipt.items() if key != "content_hash"}
    )
    if (
        receipt.get("content_hash") != content_hash
        or receipt.get("status") != "succeeded"
        or receipt.get("phase") != phase
        or receipt.get("campaign_id") != contract["campaign_id"]
        or receipt.get("run_id") != contract["run_id"]
        or receipt.get("contract_hash") != contract["content_hash"]
    ):
        raise ValueError(f"existing {phase} completion receipt conflicts with contract")
    return receipt


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    target = require_experiment_path(path, role="coordinator receipt")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


if __name__ == "__main__":
    raise SystemExit(main())
