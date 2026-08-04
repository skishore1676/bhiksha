"""Bhiksha-owned lifecycle coordinator for the chart-scenario experiment."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mala_bhiksha_kernel import canonical_sha256

from bhiksha.chart_scenarios.paths import require_experiment_path, run_artifact_paths
from bhiksha.chart_scenarios.repository import ScenarioEventRepository
from bhiksha.chart_scenarios.validation import (
    install_shadow_plan,
    read_installed_plan,
    validate_bundle,
)
from bhiksha.config.environment import load_dotenv

SCHEMA = "bhiksha.chart-scenario-coordinator-contract.v1"
CAMPAIGN_CONFIG_SCHEMA = "bhiksha.chart-scenario-campaign-config.v1"
RECEIPT_SCHEMA = "bhiksha.chart-scenario-coordinator-receipt.v1"
CENTRAL = ZoneInfo("America/Chicago")
PREPARE_CUTOFF_MINUTES = 8 * 60 + 15
_CYCLE_RECEIPT_RE = re.compile(r"slot-(\d{4})\.receipt\.json")


def main(argv: list[str] | None = None) -> int:
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
        return _main_locked(args, now)


def _main_locked(args: argparse.Namespace, now: datetime) -> int:
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
            _validate_campaign_config(
                json.loads(Path(args.campaign_config).read_text(encoding="utf-8"))
            ),
            contract_dir=Path(args.contract_dir),
            now=now,
        )
    contract = _validate_contract(
        json.loads(contract_path.read_text(encoding="utf-8")),
        contract_path=contract_path,
    )
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


def _observe_once(
    contract: dict[str, Any], paths: Any, actions: list[dict[str, Any]]
) -> None:
    plan = read_installed_plan(paths.plan)
    repository = ScenarioEventRepository(paths.database)
    candidate_ids = tuple(
        sorted({scenario.candidate_id for scenario in plan.scenarios})
    )
    completed_slot = _completed_cycle_slot(paths.cycle_receipts)
    latest_slot = repository.latest_observation_slot_ordinal(
        run_id=str(plan.run_manifest["run_id"]), candidate_ids=candidate_ids
    )
    pending_input = paths.cycle_inputs / f"slot-{latest_slot:04d}.json"
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
        cycle_input_path = paths.cycle_inputs / f"slot-{slot:04d}.json"
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


def _completed_cycle_slot(receipt_dir: Path) -> int:
    if not receipt_dir.exists():
        return 0
    receipts = sorted(receipt_dir.glob("slot-*.receipt.json"))
    for expected, path in enumerate(receipts, start=1):
        if path.name != f"slot-{expected:04d}.receipt.json":
            raise ValueError("cycle receipt filenames are not exact and contiguous")
        receipt = json.loads(path.read_text(encoding="utf-8"))
        receipt_hash = canonical_sha256(
            {key: item for key, item in receipt.items() if key != "receipt_hash"}
        )
        if (
            receipt.get("schema_version")
            != "bhiksha.chart-scenario-cycle-receipt.v2"
            or receipt.get("observation_slot_ordinal") != expected
            or receipt.get("receipt_hash") != receipt_hash
        ):
            raise ValueError("cycle receipt chain contains an invalid receipt")
    return len(receipts)


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
        ],
        cwd=Path.cwd(),
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
    if command not in {"prepare-run", "refresh-projection", "finalize-run"}:
        raise ValueError("unsupported fixed TradeLab lifecycle command")
    cwd = Path(contract["tradelab_checkout"])
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(cwd), str(Path(contract["kernel_src"])), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    argv = [
        sys.executable,
        "-m",
        "scripts.market_context",
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
    return {
        "action": f"tradelab:{command}",
        "command_hash": canonical_sha256(argv),
        **_run_command(argv, cwd=cwd, env=env),
    }


def _run_command(
    command: list[str], *, cwd: Path, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    completed = _execute_command(command, cwd=cwd, env=env)
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
    }


def _execute_command(
    command: list[str], *, cwd: Path, env: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - callers construct fixed module argv
        command,
        check=False,
        cwd=cwd,
        env=dict(env) if env is not None else None,
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
    events = [event.model_dump(mode="json") for event in repository.events()]
    if events and events[0].get("previous_event_hash") is not None:
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
        "target_session_date",
        "target_session_window",
        "target_session_window_hash",
        "cartographer_receipt",
        "birdclaw_export",
        "birdclaw_packet_hash",
        "birdclaw_output_hash",
        "narrative_source_failure",
        "campaign_config_hash",
        "tradelab_checkout",
        "tradelab_experiment_root",
        "agent_broker",
        "spreadsheet_id",
        "kernel_src",
        "plan_source",
        "projection_request",
        "content_hash",
    }
    if set(value) != expected or value.get("schema") != SCHEMA:
        raise ValueError("coordinator contract has unsupported or non-exact fields")
    computed = canonical_sha256(
        {key: item for key, item in value.items() if key != "content_hash"}
    )
    if str(value.get("content_hash", "")).removeprefix("sha256:") != computed:
        raise ValueError("coordinator contract content hash mismatch")
    if re.fullmatch(
        r"[0-9a-f]{64}",
        str(value.get("campaign_config_hash", "")).removeprefix("sha256:"),
    ) is None:
        raise ValueError("coordinator contract campaign_config_hash is invalid")
    target_date = _parse_target_date(value.get("target_session_date"))
    if contract_path is not None and contract_path.name != f"{target_date.isoformat()}.json":
        raise ValueError("daily coordinator contract filename must equal target session date")
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
        raise FileNotFoundError(f"no daily chart-scenario contract for {local.date()}: {path}")
    return path


def _before_prepare_cutoff(now: datetime) -> bool:
    local = now.astimezone(CENTRAL) if now.tzinfo else now.replace(tzinfo=CENTRAL)
    return local.hour * 60 + local.minute <= PREPARE_CUTOFF_MINUTES


def _verify_kernel_source() -> None:
    import mala_bhiksha_kernel

    configured = os.getenv("BHIKSHA_KERNEL_SRC")
    if not configured:
        raise ValueError("BHIKSHA_KERNEL_SRC is required for chart-scenario scheduling")
    expected = Path(configured).expanduser().resolve()
    actual = Path(str(mala_bhiksha_kernel.__file__)).resolve()
    if not actual.is_relative_to(expected):
        raise ValueError(
            f"loaded kernel is not from reviewed BHIKSHA_KERNEL_SRC: {actual}"
        )


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
        "spreadsheet_id",
        "kernel_src",
        "cartographer_provider",
        "cartographer_data_root",
        "symbols",
        "content_hash",
    }
    if set(value) != expected or value.get("schema") != CAMPAIGN_CONFIG_SCHEMA:
        raise ValueError("chart-scenario campaign config has unsupported or non-exact fields")
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
    if not str(value.get("spreadsheet_id") or "").strip():
        raise ValueError("campaign spreadsheet_id is required")
    birdclaw_db = Path(str(value.get("birdclaw_db") or "")).expanduser()
    if not birdclaw_db.is_absolute() or not birdclaw_db.is_file():
        raise ValueError("campaign birdclaw_db must be an existing absolute file")
    return {**dict(value), "content_hash": computed}


def _prepare_daily_contract(
    config: Mapping[str, Any], *, contract_dir: Path, now: datetime
) -> Path:
    local = now.astimezone(CENTRAL) if now.tzinfo else now.replace(tzinfo=CENTRAL)
    target_date = local.date().isoformat()
    preparation_root = require_experiment_path(
        Path(contract_dir).parent / "preparation" / target_date,
        role="daily preparation root",
    )
    attempt = preparation_root / f"attempt-{local.strftime('%H%M%S')}"
    try:
        birdclaw_export = _export_birdclaw_context(config, attempt=attempt, as_of=local)
        narrative_failure = None
    except Exception as exc:  # noqa: BLE001 - narrative is an observational sidecar.
        failure_body = {
            "schema": "bhiksha.chart-scenario-narrative-source-failure.v1",
            "status": "unavailable_non_blocking",
            "as_of": local.isoformat(),
            "error_type": type(exc).__name__,
            # Do not project local checkout/database paths into narrative data.
            "error": "Birdclaw narrative source unavailable",
            "selection_influence": False,
        }
        narrative_failure = {
            **failure_body,
            "content_hash": canonical_sha256(failure_body),
        }
        _write_atomic(attempt / "birdclaw-failure.json", narrative_failure)
        birdclaw_export = {"path": None, "packet_hash": None, "output_hash": None}
    cartographer_output = attempt / "cartographer"
    cartographer_checkout = Path(str(config["market_cartographer_checkout"])).resolve()
    cartographer_python = cartographer_checkout / ".venv" / "bin" / "python"
    if not cartographer_python.is_file():
        raise FileNotFoundError(
            f"Cartographer virtualenv interpreter is unavailable: {cartographer_python}"
        )
    command = [
        str(cartographer_python),
        "-m",
        "market_cartographer.cli",
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
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(cartographer_checkout / "src"),
            str(Path(str(config["kernel_src"]))),
            env.get("PYTHONPATH", ""),
        ]
    ).rstrip(os.pathsep)
    _run_command(command, cwd=cartographer_checkout, env=env)
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
    experiment_root = require_experiment_path(
        str(config["tradelab_experiment_root"]), role="TradeLab experiment root"
    )
    tradelab_run = experiment_root / "campaigns" / str(config["campaign_id"]) / "runs" / run_id
    body = {
        "schema": SCHEMA,
        "campaign_id": config["campaign_id"],
        "run_id": run_id,
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
        "tradelab_checkout": config["tradelab_checkout"],
        "tradelab_experiment_root": str(experiment_root),
        "agent_broker": config["agent_broker"],
        "spreadsheet_id": config["spreadsheet_id"],
        "kernel_src": config["kernel_src"],
        "plan_source": str(tradelab_run / "outputs" / "shadow-plan.json"),
        "projection_request": str(
            tradelab_run / "outputs" / "sheet-upsert-request.json"
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
    executable = checkout / "birdclawctl"
    if not executable.is_file():
        raise FileNotFoundError(f"Birdclaw entrypoint is unavailable: {executable}")
    completed = _execute_command(
        [
            str(executable),
            "export",
            "temporal-market-context",
            "--as-of",
            as_of.isoformat(),
            "--json",
        ],
        cwd=checkout,
        env={**os.environ, "BIRDCLAW_DB": str(config["birdclaw_db"])},
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Birdclaw temporal export failed: "
            + (completed.stderr or completed.stdout)[-2000:]
        )
    status = json.loads(completed.stdout)
    if (
        status.get("schema") != "birdclaw.temporal_market_context_export.v1"
        or status.get("packet_schema")
        != "birdclaw.temporal_market_context_packet.v1"
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
    return {"path": target.resolve(), "packet_hash": packet_hash, "output_hash": output_hash}


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
    session_start = datetime.fromisoformat(contract["target_session_window"]["start_at"])
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
        raise ValueError("target_session_window timestamps/timezone are invalid") from exc
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError("target_session_window must be aware and increasing")
    if start.astimezone(market_tz).date() != target_date:
        raise ValueError("target_session_window start does not match target session date")
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
    if (
        receipt.get("status") != "succeeded"
        or receipt.get("run_id") != contract["run_id"]
        or receipt.get("target_session_date") != contract["target_session_date"]
        or receipt.get("target_session_window") != dict(window)
        or str(receipt.get("target_session_window_hash", "")).removeprefix("sha256:")
        != str(contract["target_session_window_hash"]).removeprefix("sha256:")
    ):
        raise ValueError("daily contract does not match authenticated Cartographer session")


def _validate_birdclaw_export(
    contract: Mapping[str, Any], *, window: Mapping[str, str]
) -> None:
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
        cutoff = datetime.fromisoformat(str(packet["as_of"]).replace("Z", "+00:00"))
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
    run_root = root / "campaigns" / str(contract["campaign_id"]) / "runs" / str(
        contract["run_id"]
    )
    expected = {
        "plan_source": run_root / "outputs" / "shadow-plan.json",
        "projection_request": run_root / "outputs" / "sheet-upsert-request.json",
    }
    for field, path in expected.items():
        if Path(str(contract[field])).expanduser().resolve() != path.resolve():
            raise ValueError(f"daily contract {field} is not the fixed run-scoped path")


def _stage_tradelab_evidence(contract: Mapping[str, Any], paths: Any) -> None:
    root = require_experiment_path(
        str(contract["tradelab_experiment_root"]), role="TradeLab experiment root"
    )
    staging = (
        root
        / "campaigns"
        / str(contract["campaign_id"])
        / "runs"
        / str(contract["run_id"])
        / "bhiksha"
    )
    _write_atomic(
        staging / "install-receipt.json",
        json.loads(paths.install_receipt.read_text(encoding="utf-8")),
    )
    source_receipts = sorted(paths.cycle_receipts.glob("*.receipt.json"))
    expected_names: set[str] = set()
    for expected_slot, source in enumerate(source_receipts, start=1):
        match = _CYCLE_RECEIPT_RE.fullmatch(source.name)
        if match is None or int(match.group(1)) != expected_slot:
            raise ValueError("Bhiksha cycle receipts are not exact and contiguous")
        name = f"cycle-{expected_slot:04d}.receipt.json"
        expected_names.add(name)
        _write_atomic(
            staging / "cycle-receipts" / name,
            json.loads(source.read_text(encoding="utf-8")),
        )
    staged_dir = staging / "cycle-receipts"
    if staged_dir.exists() and {
        item.name for item in staged_dir.iterdir() if item.is_file()
    } != expected_names:
        raise ValueError("TradeLab staging contains non-exact cycle receipts")
    source_inputs = sorted(paths.cycle_inputs.glob("slot-*.json"))
    if len(source_inputs) != len(source_receipts):
        raise ValueError("cycle input/receipt cardinality mismatch")
    expected_input_names: set[str] = set()
    for expected_slot, source in enumerate(source_inputs, start=1):
        if source.name != f"slot-{expected_slot:04d}.json":
            raise ValueError("Bhiksha cycle inputs are not exact and contiguous")
        name = f"cycle-{expected_slot:04d}.json"
        expected_input_names.add(name)
        _write_atomic(
            staging / "cycle-inputs" / name,
            json.loads(source.read_text(encoding="utf-8")),
        )
    staged_inputs = staging / "cycle-inputs"
    if staged_inputs.exists() and {
        item.name for item in staged_inputs.iterdir() if item.is_file()
    } != expected_input_names:
        raise ValueError("TradeLab staging contains non-exact cycle inputs")
    _write_atomic(
        staging / "events.json",
        json.loads(paths.events_export.read_text(encoding="utf-8")),
    )


def _install_or_verify_plan(contract: Mapping[str, Any], paths: Any) -> dict[str, Any]:
    source_payload = json.loads(Path(contract["plan_source"]).read_text(encoding="utf-8"))
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
