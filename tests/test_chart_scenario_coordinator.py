from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from mala_bhiksha_kernel import canonical_sha256

import bhiksha.tools.chart_scenario_coordinator as coordinator
from bhiksha.chart_scenarios.cycle import run_observation_cycle
from bhiksha.chart_scenarios.repository import ScenarioEventRepository
from bhiksha.chart_scenarios.validation import install_shadow_plan
from bhiksha.ops.chart_scenario_sheet import SPREADSHEET_ID
from bhiksha.tools.chart_kernel_runtime import (
    RUNTIME_HASH_ENV,
    RUNTIME_RECORD_ENV,
    capture_kernel_runtime,
    write_runtime_record,
)
from bhiksha.tools.chart_scenario_campaign_config import (
    build_campaign_config,
    capture_runtime_record,
)
from bhiksha.tools.chart_scenario_coordinator import (
    _before_prepare_cutoff,
    _campaign_window_preflight,
    _completed_cycle_slot,
    _export_events,
    _phase,
    _prepare_daily_contract,
    _require_preopen_completion,
    _resolve_daily_contract,
    _run_phase,
    _run_tradelab_lifecycle,
    _sanitized_subprocess_env,
    _stage_tradelab_evidence,
    _validate_campaign_config,
    _validate_contract,
    _validate_existing_staging_generation,
    _validate_staging_event_partition,
    _verify_frozen_toolchain,
    _verify_kernel_source,
)
from tests.test_chart_scenarios import _bundle_payload, _cycle_input, _plan


def _toolchain(tmp_path: Path) -> dict[str, dict[str, str]]:
    roles = {
        "birdclaw": tmp_path / "birdclaw",
        "market_cartographer": tmp_path / "market-cartographer",
        "tradelab": tmp_path / "tradelab",
        "agent_broker": tmp_path / "agent-broker",
    }
    return {
        role: {
            "checkout": str(checkout),
            "commit": "a" * 40,
            "entrypoint": str(checkout / "entrypoint"),
            "entrypoint_sha256": "b" * 64,
        }
        for role, checkout in roles.items()
    }


def _window(*, end_at: str = "2026-08-04T16:00:00-04:00") -> dict[str, str]:
    return {
        "start_at": "2026-08-04T09:30:00-04:00",
        "end_at": end_at,
        "market_timezone": "America/New_York",
    }


def _contract(tmp_path: Path) -> dict[str, object]:
    window = _window()
    packet = {
        "schema": "birdclaw.temporal_market_context_packet.v1",
        "as_of": "2026-08-04T12:45:00.000Z",
        "cutoff": "2026-08-04T12:45:00.000Z",
        "evidence": [],
    }
    birdclaw_body = {
        "schema": "birdclaw.temporal_market_context_export.v1",
        "packet": packet,
        "packet_hash": canonical_sha256(packet),
    }
    birdclaw = {**birdclaw_body, "output_hash": canonical_sha256(birdclaw_body)}
    birdclaw_path = tmp_path / "birdclaw.json"
    birdclaw_path.write_text(json.dumps(birdclaw), encoding="utf-8")
    receipt_body = {
        "schema": "market_cartographer.market_context_receipt.v2",
        "status": "succeeded",
        "run_id": "run-2026-08-04",
        "target_session_date": "2026-08-04",
        "target_session_window": window,
        "target_session_window_hash": canonical_sha256(window),
    }
    receipt = {**receipt_body, "receipt_hash": canonical_sha256(receipt_body)}
    receipt_path = tmp_path / "cartographer-receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    experiment_root = tmp_path / "artifacts" / "chart_scenarios" / "tradelab"
    run_root = experiment_root / "campaigns" / "campaign-1" / "runs" / "run-2026-08-04"
    toolchain = _toolchain(tmp_path)
    body: dict[str, object] = {
        "schema": "bhiksha.chart-scenario-coordinator-contract.v1",
        "campaign_id": "campaign-1",
        "run_id": "run-2026-08-04",
        "outcome": "plan",
        "target_session_date": "2026-08-04",
        "target_session_window": window,
        "target_session_window_hash": canonical_sha256(window),
        "cartographer_receipt": str(receipt_path),
        "birdclaw_export": str(birdclaw_path),
        "birdclaw_packet_hash": birdclaw["packet_hash"],
        "birdclaw_output_hash": birdclaw["output_hash"],
        "narrative_source_failure": None,
        "campaign_config_hash": "c" * 64,
        "campaign_protocol_hash": "d" * 64,
        "campaign_freeze_receipt_hash": "e" * 64,
        "session_calendar_hash": "f" * 64,
        "toolchain": toolchain,
        "toolchain_hash": canonical_sha256(toolchain),
        "tradelab_checkout": "/tmp/tradelab",
        "tradelab_experiment_root": str(experiment_root),
        "agent_broker": "/Users/sunny/code/agent-broker/agent-broker",
        "agent_broker_checkout": str(tmp_path / "agent-broker"),
        "spreadsheet_id": SPREADSHEET_ID,
        "kernel_src": "/tmp/kernel/src",
        "plan_source": str(run_root / "outputs" / "shadow-plan.json"),
        "projection_request": str(run_root / "outputs" / "sheet-upsert-request.json"),
    }
    return {**body, "content_hash": canonical_sha256(body)}


def _campaign_artifacts(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "artifacts" / "chart_scenarios" / "tradelab"
    campaign_root = root / "campaigns" / "campaign-1"
    campaign_root.mkdir(parents=True)
    treatment_hash = "sha256:" + "1" * 64
    universe_hash = "sha256:" + "2" * 64
    campaign_body = {
        "schema": "tradelab.market_context_campaign.v2",
        "campaign_id": "campaign-1",
        "treatment_manifest_hash": treatment_hash,
        "universe_hash": universe_hash,
    }
    campaign = {
        **campaign_body,
        "content_hash": "sha256:" + canonical_sha256(campaign_body),
    }
    sessions = [
        "2026-08-04",
        "2026-08-05",
        "2026-08-06",
        "2026-08-07",
        "2026-08-10",
        "2026-08-11",
        "2026-08-12",
        "2026-08-13",
        "2026-08-14",
        "2026-08-17",
    ]
    calendar_body = {
        "schema": "tradelab.market_context_session_calendar.v1",
        "calendar_id": "XNYS",
        "timezone": "America/New_York",
        "implementation": "exchange_calendars",
        "calendar_version": "exchange_calendars-4.13.2-XNYS",
        "starts_on": sessions[0],
        "ends_on": sessions[-1],
        "ends_on_semantics": "inclusive",
        "session_count": 10,
        "session_dates": sessions,
    }
    calendar = {
        **calendar_body,
        "content_hash": "sha256:" + canonical_sha256(calendar_body),
    }
    protocol_body = {
        "schema": "tradelab.market_context_campaign_protocol.v1",
        "campaign_manifest_hash": campaign["content_hash"],
        "treatment_manifest_hash": treatment_hash,
        "universe_hash": universe_hash,
        "starts_on": sessions[0],
        "ends_on": sessions[-1],
        "ends_on_semantics": "inclusive",
        "session_calendar": calendar,
        "session_calendar_hash": calendar["content_hash"],
        "authorized_session_dates": sessions,
        "checkpoint_after_sessions": 5,
        "max_sessions": 10,
    }
    protocol = {
        **protocol_body,
        "content_hash": "sha256:" + canonical_sha256(protocol_body),
    }
    freeze_body = {
        "schema": "tradelab.market_context_campaign_freeze_receipt.v1",
        "campaign_manifest_hash": campaign["content_hash"],
        "campaign_protocol_hash": protocol["content_hash"],
        "treatment_manifest_hash": treatment_hash,
        "component_commits": {},
        "frozen_behavior_hashes": {},
        "universe_hash": universe_hash,
        "session_calendar_hash": calendar["content_hash"],
        "starts_on": sessions[0],
        "ends_on": sessions[-1],
        "checkpoint_after_sessions": 5,
        "max_sessions": 10,
        "minimum_closed_trigger_count": 10,
        "effects": {"broker": False, "orders": False},
    }
    freeze = {
        **freeze_body,
        "content_hash": "sha256:" + canonical_sha256(freeze_body),
    }
    for name, payload in (
        ("campaign.json", campaign),
        ("campaign-protocol.json", protocol),
        ("campaign-freeze-receipt.json", freeze),
    ):
        (campaign_root / name).write_text(json.dumps(payload), encoding="utf-8")
    return root, {
        "campaign_manifest_hash": campaign["content_hash"],
        "campaign_protocol_hash": protocol["content_hash"],
        "campaign_freeze_receipt_hash": freeze["content_hash"],
        "treatment_manifest_hash": treatment_hash,
        "universe_hash": universe_hash,
        "session_calendar_hash": calendar["content_hash"],
    }


def test_coordinator_contract_is_content_addressed_and_session_phases_are_explicit(
    tmp_path: Path,
) -> None:
    contract = _validate_contract(_contract(tmp_path))

    assert contract["run_id"] == "run-2026-08-04"
    window = contract["target_session_window"]
    assert (
        _phase(datetime.fromisoformat("2026-08-04T07:45:00-05:00"), window) == "morning"
    )
    assert (
        _phase(datetime.fromisoformat("2026-08-04T10:00:00-05:00"), window)
        == "intraday"
    )
    assert (
        _phase(datetime.fromisoformat("2026-08-04T15:15:00-05:00"), window)
        == "after-close"
    )


def test_phase_uses_authenticated_early_close_end_instead_of_fixed_clock(
    tmp_path: Path,
) -> None:
    value = _contract(tmp_path)
    window = _window(end_at="2026-08-04T13:00:00-04:00")
    value["target_session_window"] = window
    value["target_session_window_hash"] = canonical_sha256(window)
    receipt_path = Path(str(value["cartographer_receipt"]))
    receipt_body = {
        "schema": "market_cartographer.market_context_receipt.v2",
        "status": "succeeded",
        "run_id": value["run_id"],
        "target_session_date": value["target_session_date"],
        "target_session_window": window,
        "target_session_window_hash": canonical_sha256(window),
    }
    receipt_path.write_text(
        json.dumps({**receipt_body, "receipt_hash": canonical_sha256(receipt_body)}),
        encoding="utf-8",
    )
    value["content_hash"] = canonical_sha256(
        {key: item for key, item in value.items() if key != "content_hash"}
    )
    contract = _validate_contract(value)

    assert (
        _phase(
            datetime.fromisoformat("2026-08-04T12:05:00-05:00"),
            contract["target_session_window"],
        )
        == "after-close"
    )


def test_daily_contract_resolution_rotates_run_without_scheduler_edits(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "artifacts" / "chart_scenarios" / "daily-contracts"
    directory.mkdir(parents=True)
    first = directory / "2026-08-04.json"
    second = directory / "2026-08-05.json"
    first.write_text("{}", encoding="utf-8")
    second.write_text("{}", encoding="utf-8")

    assert (
        _resolve_daily_contract(
            directory, datetime.fromisoformat("2026-08-04T07:45:00-05:00")
        )
        == first
    )
    assert (
        _resolve_daily_contract(
            directory, datetime.fromisoformat("2026-08-05T07:45:00-05:00")
        )
        == second
    )


def test_preparation_cutoff_allows_bounded_retries_only_before_open() -> None:
    assert _before_prepare_cutoff(datetime.fromisoformat("2026-08-04T08:15:00-05:00"))
    assert not _before_prepare_cutoff(
        datetime.fromisoformat("2026-08-04T08:16:00-05:00")
    )


def test_preopen_completion_gate_writes_non_comparable_receipt(tmp_path: Path) -> None:
    contract = _validate_contract(_contract(tmp_path))
    run_root = tmp_path / "artifacts/chart_scenarios/run-late"
    with pytest.raises(RuntimeError, match="after authenticated session open"):
        _require_preopen_completion(
            contract,
            SimpleNamespace(root=run_root),
            now=datetime.fromisoformat("2026-08-04T09:30:00-04:00"),
        )
    receipt = json.loads(
        (run_root / "coordinator/late-preparation.json").read_text(encoding="utf-8")
    )
    assert receipt["status"] == "late_non_comparable"
    assert receipt["installed"] is False
    assert not any(receipt["effects"].values())


def test_narrative_source_failure_is_valid_non_blocking_contract(
    tmp_path: Path,
) -> None:
    value = _contract(tmp_path)
    failure_body = {
        "schema": "bhiksha.chart-scenario-narrative-source-failure.v1",
        "status": "unavailable_non_blocking",
        "as_of": "2026-08-04T07:45:00-05:00",
        "error_type": "RuntimeError",
        "error": "source unavailable",
        "selection_influence": False,
    }
    value["birdclaw_export"] = None
    value["birdclaw_packet_hash"] = None
    value["birdclaw_output_hash"] = None
    value["narrative_source_failure"] = {
        **failure_body,
        "content_hash": canonical_sha256(failure_body),
    }
    value["content_hash"] = canonical_sha256(
        {key: item for key, item in value.items() if key != "content_hash"}
    )
    contract = _validate_contract(value)
    assert contract["narrative_source_failure"]["selection_influence"] is False


def test_kernel_readback_must_resolve_under_reviewed_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mala_bhiksha_kernel

    kernel_src = Path(str(mala_bhiksha_kernel.__file__)).resolve().parent.parent
    runtime = capture_kernel_runtime(kernel_src)
    record = write_runtime_record(tmp_path / "kernel-runtime.json", runtime)
    monkeypatch.setenv("BHIKSHA_KERNEL_SRC", str(kernel_src))
    monkeypatch.setenv(RUNTIME_RECORD_ENV, str(record))
    monkeypatch.setenv(RUNTIME_HASH_ENV, runtime["content_hash"])
    _verify_kernel_source()

    monkeypatch.setenv("BHIKSHA_KERNEL_SRC", "/tmp/not-the-loaded-kernel")
    with pytest.raises(ValueError, match="configured kernel src"):
        _verify_kernel_source()


def test_subprocess_environments_are_role_scoped_and_strip_money_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = {
        "PUBLIC_API_SECRET": "public-secret",
        "PUBLIC_API_KEY": "public-key",
        "SCHWAB_APP_SECRET": "schwab-secret",
        "SCHWAB_APP_KEY": "schwab-key",
        "SCHWAB_TOKEN_FILE": "/secure/schwab-token.json",
        "GOOGLE_API_CREDENTIALS_PATH": "/secure/google.json",
    }
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)

    inert = _sanitized_subprocess_env(role="broker_inert")
    market_data = _sanitized_subprocess_env(role="schwab_market_data")
    sheet = _sanitized_subprocess_env(role="google_sheet")
    assert not set(secrets).intersection(inert)
    assert market_data["SCHWAB_TOKEN_FILE"] == secrets["SCHWAB_TOKEN_FILE"]
    assert "SCHWAB_APP_SECRET" not in market_data
    assert "SCHWAB_APP_KEY" not in market_data
    assert "PUBLIC_API_SECRET" not in market_data
    assert (
        sheet["GOOGLE_API_CREDENTIALS_PATH"] == secrets["GOOGLE_API_CREDENTIALS_PATH"]
    )
    assert "SCHWAB_TOKEN_FILE" not in sheet


def test_frozen_toolchain_rejects_dirty_hash_drift_and_symlink_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkouts = {
        "birdclaw": tmp_path / "birdclaw",
        "market_cartographer": tmp_path / "market-cartographer",
        "tradelab": tmp_path / "tradelab",
        "agent_broker": tmp_path / "agent-broker",
    }
    toolchain: dict[str, dict[str, object]] = {}
    runtime_records = tmp_path / "artifacts/chart_scenarios/runtime-records"
    runtime_records.mkdir(parents=True)
    for role, checkout in checkouts.items():
        (checkout / ".gitignore").parent.mkdir(parents=True, exist_ok=True)
        (checkout / ".gitignore").write_text(
            "/.venv/\n/escaped-entrypoint\n", encoding="utf-8"
        )
        import_root = checkout / "src"
        import_root.mkdir()
        entrypoint = import_root / "entrypoint.py"
        entrypoint.write_text(f"{role}\n", encoding="utf-8")
        interpreter = checkout / ".venv/bin/runtime"
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text(
            "#!/bin/sh\n[ \"${1:-}\" = --version ] && echo 'Fixture 1.0'\n",
            encoding="utf-8",
        )
        interpreter.chmod(0o755)
        launcher = interpreter
        if role == "agent_broker":
            launcher = checkout / ".venv/bin/agent-broker"
            launcher.write_text(f"#!{interpreter}\n", encoding="utf-8")
            launcher.chmod(0o755)
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ],
            check=True,
        )
        commit = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        module = coordinator._TOOL_MODULES[role]
        prefix = {
            "birdclaw": [str(launcher), str(entrypoint)],
            "market_cartographer": [
                str(launcher),
                "-m",
                "market_cartographer.cli",
            ],
            "tradelab": [str(launcher), "-m", "scripts.market_context"],
            "agent_broker": [str(launcher)],
        }[role]
        record_path = runtime_records / f"{role}.json"
        body: dict[str, object] = {
            "schema": coordinator._RUNTIME_RECORD_SCHEMA,
            "role": role,
            "checkout": str(checkout),
            "commit": commit,
            "clean": True,
            "launcher": str(launcher),
            "launcher_sha256": hashlib.sha256(launcher.read_bytes()).hexdigest(),
            "launcher_realpath": str(launcher.resolve()),
            "launcher_realpath_sha256": hashlib.sha256(
                launcher.resolve().read_bytes()
            ).hexdigest(),
            "launcher_symlink_target": None,
            "interpreter": str(interpreter),
            "interpreter_realpath": str(interpreter.resolve()),
            "interpreter_sha256": hashlib.sha256(interpreter.read_bytes()).hexdigest(),
            "interpreter_symlink_target": None,
            "runtime_version": "Fixture 1.0",
            "entrypoint": str(entrypoint),
            "entrypoint_sha256": hashlib.sha256(entrypoint.read_bytes()).hexdigest(),
            "import_root": str(import_root),
            "import_root_sha256": coordinator._source_tree_sha256(import_root),
            "import_map": {
                module: {
                    "path": str(entrypoint.resolve()),
                    "sha256": hashlib.sha256(entrypoint.read_bytes()).hexdigest(),
                }
            },
            "dependency_identity": {
                "mode": "source_tree_only",
                "path": None,
                "sha256": coordinator._source_tree_sha256(import_root),
            },
            "installed_environment_identity": {
                "schema": "fixture.installed-environment.v1",
                "role": role,
            },
            "argv_prefix": prefix,
            "captured_at": "2026-08-04T12:00:00Z",
            "record_path": str(record_path),
        }
        record = {**body, "content_hash": canonical_sha256(body)}
        record_path.write_text(json.dumps(record), encoding="utf-8")
        toolchain[role] = record
    config = {
        "birdclaw_checkout": str(checkouts["birdclaw"]),
        "market_cartographer_checkout": str(checkouts["market_cartographer"]),
        "tradelab_checkout": str(checkouts["tradelab"]),
        "agent_broker_checkout": str(checkouts["agent_broker"]),
        "kernel_src": str(tmp_path / "kernel/src"),
        "agent_broker": toolchain["agent_broker"]["launcher"],
        "toolchain": toolchain,
    }
    monkeypatch.setattr(
        coordinator,
        "_resolve_module_origin",
        lambda interpreter, *, module, role, config, cwd: Path(
            toolchain[role]["entrypoint"]
        ).resolve(),
    )
    monkeypatch.setattr(
        coordinator,
        "_capture_installed_environment_identity",
        lambda interpreter, *, role, checkout: toolchain[role][
            "installed_environment_identity"
        ],
    )
    _verify_frozen_toolchain(config)

    birdclaw_entrypoint = Path(toolchain["birdclaw"]["entrypoint"])
    birdclaw_entrypoint.write_text("dirty\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not clean"):
        _verify_frozen_toolchain(config)
    birdclaw_entrypoint.write_text("birdclaw\n", encoding="utf-8")

    toolchain["birdclaw"]["entrypoint_sha256"] = "f" * 64
    _rewrite_runtime_record(toolchain["birdclaw"])
    with pytest.raises(ValueError, match="entrypoint drift"):
        _verify_frozen_toolchain(config)
    toolchain["birdclaw"]["entrypoint_sha256"] = hashlib.sha256(
        birdclaw_entrypoint.read_bytes()
    ).hexdigest()
    _rewrite_runtime_record(toolchain["birdclaw"])

    outside = tmp_path / "outside-entrypoint"
    outside.write_text("outside\n", encoding="utf-8")
    symlink = checkouts["birdclaw"] / "escaped-entrypoint"
    symlink.symlink_to(outside)
    toolchain["birdclaw"]["entrypoint"] = str(symlink)
    _rewrite_runtime_record(toolchain["birdclaw"])
    with pytest.raises(ValueError, match="escaped checkout"):
        _verify_frozen_toolchain(config)

    toolchain["birdclaw"]["entrypoint"] = str(birdclaw_entrypoint)
    _rewrite_runtime_record(toolchain["birdclaw"])
    ignored_runtime = Path(toolchain["market_cartographer"]["interpreter"])
    ignored_runtime.write_text(
        "#!/bin/sh\n[ \"${1:-}\" = --version ] && echo 'Fixture 2.0'\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="(launcher|interpreter) drift"):
        _verify_frozen_toolchain(config)


def _rewrite_runtime_record(record: dict[str, object]) -> None:
    body = {key: value for key, value in record.items() if key != "content_hash"}
    record["content_hash"] = canonical_sha256(body)
    Path(str(record["record_path"])).write_text(json.dumps(record), encoding="utf-8")


@pytest.mark.parametrize("cartographer_status", ["succeeded", "no_plan"])
def test_campaign_config_autonomously_emits_authenticated_daily_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cartographer_status: str
) -> None:
    cartographer = tmp_path / "market-cartographer"
    python = cartographer / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    birdclaw = tmp_path / "birdclaw"
    birdclaw.mkdir()
    (birdclaw / "birdclawctl").write_text("", encoding="utf-8")
    birdclaw_db = tmp_path / "production-birdclaw.sqlite"
    birdclaw_db.write_bytes(b"sqlite-fixture")
    experiment_root, frozen = _campaign_artifacts(tmp_path)
    agent_broker_checkout = tmp_path / "agent-broker"
    agent_broker = agent_broker_checkout / ".venv" / "bin" / "agent-broker"
    toolchain = _toolchain(tmp_path)
    toolchain["agent_broker"]["entrypoint"] = str(agent_broker)
    body: dict[str, object] = {
        "schema": "bhiksha.chart-scenario-campaign-config.v1",
        "campaign_id": "campaign-1",
        "birdclaw_checkout": str(birdclaw),
        "birdclaw_db": str(birdclaw_db),
        "market_cartographer_checkout": str(cartographer),
        "tradelab_checkout": str(tmp_path / "tradelab"),
        "tradelab_experiment_root": str(experiment_root),
        "agent_broker": str(agent_broker),
        "agent_broker_checkout": str(agent_broker_checkout),
        "spreadsheet_id": SPREADSHEET_ID,
        "kernel_src": str(tmp_path / "kernel" / "src"),
        "cartographer_provider": "mala",
        "cartographer_data_root": str(tmp_path / "bars"),
        "symbols": ["IWM", "SPY"],
        **frozen,
        "session_calendar_id": "XNYS",
        "session_calendar_version": "exchange_calendars-4.13.2-XNYS",
        "toolchain": toolchain,
        "starts_on": "2026-08-04",
        "checkpoint_after_sessions": 5,
        "max_sessions": 10,
        "ends_on": "2026-08-17",
    }
    config_payload = {**body, "content_hash": canonical_sha256(body)}
    config = _validate_campaign_config(config_payload)
    assert (
        _campaign_window_preflight(
            config,
            now=datetime.fromisoformat("2026-08-04T07:45:00-05:00"),
            artifact_root=tmp_path / "artifacts" / "chart_scenarios",
        )
        is None
    )
    outside = _campaign_window_preflight(
        config,
        now=datetime.fromisoformat("2026-08-03T07:45:00-05:00"),
        artifact_root=tmp_path / "artifacts" / "chart_scenarios",
    )
    assert outside is not None
    assert outside["reason"] == "outside_campaign_window"
    assert outside["campaign_protocol_hash"] == config["campaign_protocol_hash"]
    assert outside["session_calendar_hash"] == config["session_calendar_hash"]
    assert not any(outside["effects"].values())
    window = _window()

    birdclaw_envs: list[dict[str, str]] = []

    def fake_execute(command, *, cwd, env=None):
        del command
        birdclaw_envs.append(dict(env or {}))
        packet = {
            "schema": "birdclaw.temporal_market_context_packet.v1",
            "as_of": "2026-08-04T12:45:00.000Z",
            "cutoff": "2026-08-04T12:45:00.000Z",
            "evidence": [],
        }
        output_body = {
            "schema": "birdclaw.temporal_market_context_export.v1",
            "packet": packet,
            "packet_hash": canonical_sha256(packet),
        }
        output = {**output_body, "output_hash": canonical_sha256(output_body)}
        relative = "birdclaw-home/public/x/market-context/fixture.json"
        target = cwd / relative
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(output), encoding="utf-8")
        status = {
            "schema": "birdclaw.temporal_market_context_export.v1",
            "packet_schema": "birdclaw.temporal_market_context_packet.v1",
            "packet_hash": output["packet_hash"],
            "output_hash": output["output_hash"],
            "dry_run": False,
            "output": relative,
        }
        return coordinator.subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(status), stderr=""
        )

    def fake_run(command, *, cwd, env=None):
        del cwd, env
        output = Path(command[command.index("--output") + 1])
        output.mkdir(parents=True)
        if cartographer_status == "no_plan":
            effects = {
                "broker": False,
                "orders": False,
                "auth": False,
                "schedule": False,
                "external_send": False,
            }
            manifest_body = {
                "schema": "market_cartographer.market_context_no_plan.v1",
                "program_id": "morning-market-scenario-selection-shadow.v1",
                "experiment_family_id": "market-context-shadow.v1",
                "experiment_version": "v1",
                "campaign_id": "campaign-1",
                "run_id": "run-auto-2026-08-04",
                "status": "no_plan",
                "reason": "all_symbols_quarantined",
                "target_session_date": "2026-08-04",
                "target_session_window": window,
                "target_session_window_hash": canonical_sha256(window),
                "effects": effects,
            }
            export_hash = canonical_sha256(manifest_body)
            manifest = {
                **manifest_body,
                "export_id": f"no-plan:{export_hash[:16]}",
                "export_hash": export_hash,
            }
            payloads = {
                "manifest.json": manifest,
                "freshness-evidence.json": {"schema": "fixture.freshness.v1"},
                "normalized-inputs/index.json": {"schema": "fixture.index.v1"},
                "universe-manifest.json": {"schema": "fixture.universe.v1"},
            }
            for relative, payload in payloads.items():
                target = output / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(payload), encoding="utf-8")
            artifacts = [
                {
                    "path": relative,
                    "content_hash": (
                        export_hash
                        if relative == "manifest.json"
                        else canonical_sha256(payload)
                    ),
                }
                for relative, payload in sorted(payloads.items())
            ]
            receipt_body = {
                "schema": "market_cartographer.market_context_receipt.v2",
                "status": "no_plan",
                "run_id": "run-auto-2026-08-04",
                "export_id": manifest["export_id"],
                "export_hash": export_hash,
                "target_session_date": "2026-08-04",
                "target_session_window": window,
                "target_session_window_hash": canonical_sha256(window),
                "data_mode": "fixture",
                "candidate_pool_hash": None,
                "arm_a_selection_hash": None,
                "materialized_scenario_count": 0,
                "artifacts": artifacts,
                "effects": effects,
            }
            (output / "receipt.json").write_text(
                json.dumps(
                    {
                        **receipt_body,
                        "receipt_hash": canonical_sha256(receipt_body),
                    }
                ),
                encoding="utf-8",
            )
            return {"status": "succeeded"}
        receipt_body = {
            "schema": "market_cartographer.market_context_receipt.v2",
            "status": "succeeded",
            "run_id": "run-auto-2026-08-04",
            "target_session_date": "2026-08-04",
            "target_session_window": window,
            "target_session_window_hash": canonical_sha256(window),
        }
        (output / "receipt.json").write_text(
            json.dumps(
                {**receipt_body, "receipt_hash": canonical_sha256(receipt_body)}
            ),
            encoding="utf-8",
        )
        return {"status": "succeeded"}

    def fake_subprocess(command, *, cwd, env=None):
        if "market-context-export" in command:
            fake_run(command, cwd=cwd, env=env)
            return coordinator.subprocess.CompletedProcess(
                args=command, returncode=0, stdout="{}", stderr=""
            )
        return fake_execute(command, cwd=cwd, env=env)

    monkeypatch.setattr(coordinator, "_execute_command", fake_subprocess)
    monkeypatch.setattr(
        coordinator, "_verify_frozen_toolchain", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        coordinator,
        "_verify_toolchain_role",
        lambda config, *, role, configured_checkout=None: {
            "argv_prefix": (
                ["bird-runtime", "bird-entrypoint"]
                if role == "birdclaw"
                else ["cartographer-python", "-m", "market_cartographer.cli"]
            )
        },
    )
    contract_dir = tmp_path / "artifacts" / "chart_scenarios" / "daily-contracts"

    path = _prepare_daily_contract(
        config,
        contract_dir=contract_dir,
        now=datetime.fromisoformat("2026-08-04T07:45:00-05:00"),
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert path.name == "2026-08-04.json"
    assert payload["campaign_id"] == "campaign-1"
    assert payload["run_id"] == "run-auto-2026-08-04"
    assert payload["target_session_window"] == window
    assert payload["campaign_config_hash"] == config["content_hash"]
    if cartographer_status == "no_plan":
        assert payload["outcome"] == "no_plan"
        assert payload["birdclaw_packet_hash"] is None
        assert payload["plan_source"] is None
        assert payload["projection_request"] is None
        assert birdclaw_envs == []
    else:
        assert payload["outcome"] == "plan"
        assert payload["birdclaw_packet_hash"]
        assert birdclaw_envs[0]["BIRDCLAW_DB"] == str(birdclaw_db)
    assert str(birdclaw_db) not in json.dumps(payload)

    protocol_path = (
        experiment_root / "campaigns" / "campaign-1" / "campaign-protocol.json"
    )
    tampered = json.loads(protocol_path.read_text(encoding="utf-8"))
    tampered["max_sessions"] = 9
    tampered["content_hash"] = "sha256:" + canonical_sha256(
        {key: value for key, value in tampered.items() if key != "content_hash"}
    )
    protocol_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="campaign_protocol_hash"):
        _validate_campaign_config(config_payload)


def test_campaign_config_builder_binds_canonical_freeze_without_hand_authored_hashes(
    tmp_path: Path,
) -> None:
    experiment_root, frozen = _campaign_artifacts(tmp_path)
    birdclaw_db = tmp_path / "birdclaw.sqlite"
    birdclaw_db.write_bytes(b"fixture")
    output = tmp_path / "artifacts/chart_scenarios/campaign-config.json"

    payload = build_campaign_config(
        experiment_root=experiment_root,
        campaign_id="campaign-1",
        birdclaw_checkout=tmp_path / "birdclaw",
        birdclaw_db=birdclaw_db,
        market_cartographer_checkout=tmp_path / "market-cartographer",
        tradelab_checkout=tmp_path / "tradelab",
        agent_broker_checkout=tmp_path / "agent-broker",
        agent_broker=tmp_path / "agent-broker/.venv/bin/agent-broker",
        kernel_src=tmp_path / "kernel/src",
        cartographer_provider="fixture",
        cartographer_data_root=None,
        symbols=["SPY", "IWM", "SPY"],
        toolchain=_toolchain(tmp_path),
        output=output,
    )

    assert json.loads(output.read_text(encoding="utf-8")) == payload
    assert payload["symbols"] == ["IWM", "SPY"]
    assert payload["campaign_freeze_receipt_hash"] == frozen[
        "campaign_freeze_receipt_hash"
    ]
    assert payload["checkpoint_after_sessions"] == 5
    assert payload["max_sessions"] == 10
    assert payload["content_hash"] == canonical_sha256(
        {key: value for key, value in payload.items() if key != "content_hash"}
    )

    with pytest.raises(ValueError, match="must be under artifacts/chart_scenarios"):
        build_campaign_config(
            experiment_root=experiment_root,
            campaign_id="campaign-1",
            birdclaw_checkout=tmp_path / "birdclaw",
            birdclaw_db=birdclaw_db,
            market_cartographer_checkout=tmp_path / "market-cartographer",
            tradelab_checkout=tmp_path / "tradelab",
            agent_broker_checkout=tmp_path / "agent-broker",
            agent_broker=tmp_path / "agent-broker/.venv/bin/agent-broker",
            kernel_src=tmp_path / "kernel/src",
            cartographer_provider="fixture",
            cartographer_data_root=None,
            symbols=["SPY"],
            toolchain=_toolchain(tmp_path),
            output=tmp_path / "campaign-config.json",
        )


def test_agent_broker_runtime_capture_uses_checkout_package_layout(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "agent-broker"
    package = checkout / "agent_broker"
    package.mkdir(parents=True)
    (package / "cli.py").write_text("def main(): pass\n", encoding="utf-8")
    (checkout / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.name=Bhiksha Tests",
            "-c",
            "user.email=tests@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    environment = checkout / ".venv"
    subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
    interpreter = environment / "bin" / "python"
    launcher = environment / "bin" / "agent-broker"
    launcher.symlink_to(interpreter)
    record_path = (
        tmp_path
        / "artifacts"
        / "chart_scenarios"
        / "runtime"
        / "agent-broker.json"
    )

    payload = capture_runtime_record(
        role="agent_broker",
        checkout=checkout,
        launcher=launcher,
        interpreter=interpreter,
        record_path=record_path,
        captured_at="2026-08-10T01:10:00+00:00",
    )

    assert payload["entrypoint"] == str(package / "cli.py")
    assert payload["import_root"] == str(package)
    assert json.loads(record_path.read_text(encoding="utf-8")) == payload


def test_campaign_config_builder_rejects_stale_freeze_decision_field(
    tmp_path: Path,
) -> None:
    experiment_root, _frozen = _campaign_artifacts(tmp_path)
    campaign_root = experiment_root / "campaigns" / "campaign-1"
    freeze_path = campaign_root / "campaign-freeze-receipt.json"
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    freeze.pop("minimum_closed_trigger_count")
    freeze["minimum_closed_sessions_for_decision"] = 10
    freeze["content_hash"] = "sha256:" + canonical_sha256(
        {key: value for key, value in freeze.items() if key != "content_hash"}
    )
    freeze_path.write_text(json.dumps(freeze), encoding="utf-8")
    birdclaw_db = tmp_path / "birdclaw.sqlite"
    birdclaw_db.write_bytes(b"fixture")

    with pytest.raises(ValueError, match="campaign boundaries are invalid"):
        build_campaign_config(
            experiment_root=experiment_root,
            campaign_id="campaign-1",
            birdclaw_checkout=tmp_path / "birdclaw",
            birdclaw_db=birdclaw_db,
            market_cartographer_checkout=tmp_path / "market-cartographer",
            tradelab_checkout=tmp_path / "tradelab",
            agent_broker_checkout=tmp_path / "agent-broker",
            agent_broker=tmp_path / "agent-broker/.venv/bin/agent-broker",
            kernel_src=tmp_path / "kernel/src",
            cartographer_provider="fixture",
            cartographer_data_root=None,
            symbols=["SPY"],
            toolchain=_toolchain(tmp_path),
            output=tmp_path / "artifacts/chart_scenarios/campaign-config.json",
        )


def test_coordinator_rejects_generic_commands_and_non_run_scoped_outputs(
    tmp_path: Path,
) -> None:
    value = _contract(tmp_path)
    value["morning_commands"] = [["bhiksha.tools.launchd_job", "live-start"]]
    value["content_hash"] = canonical_sha256(
        {key: item for key, item in value.items() if key != "content_hash"}
    )
    with pytest.raises(ValueError, match="non-exact fields"):
        _validate_contract(value)

    value = _contract(tmp_path)
    value["plan_source"] = "/tmp/artifacts/playbook/active_plan.json"
    value["content_hash"] = canonical_sha256(
        {key: item for key, item in value.items() if key != "content_hash"}
    )
    with pytest.raises(ValueError, match="fixed run-scoped path"):
        _validate_contract(value)


def test_fixed_tradelab_lifecycle_has_no_caller_authored_output_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = _validate_contract(_contract(tmp_path))
    captured: list[list[str]] = []

    def fake_run(command, *, cwd, env=None):
        del cwd, env
        captured.append(command)
        return {"status": "succeeded"}

    monkeypatch.setattr(
        coordinator,
        "_execute_command",
        lambda command, *, cwd, env=None: (
            captured.append(command)
            or coordinator.subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=json.dumps({"status": "succeeded"}),
                stderr="",
            )
        ),
    )
    monkeypatch.setattr(
        coordinator, "_verify_frozen_toolchain", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        coordinator,
        "_verify_toolchain_role",
        lambda config, *, role, configured_checkout=None: {
            "argv_prefix": ["python", "-m", "scripts.market_context"],
            "record_path": str(
                tmp_path / "artifacts/chart_scenarios/agent-broker.json"
            ),
        },
    )

    _run_tradelab_lifecycle(contract, command="prepare-run")
    _run_tradelab_lifecycle(contract, command="refresh-projection")
    _run_tradelab_lifecycle(contract, command="finalize-run")

    assert [command[3] for command in captured] == [
        "prepare-run",
        "refresh-projection",
        "finalize-run",
    ]
    assert all("--output" not in command for command in captured)
    assert all("active_plan.json" not in command for command in captured)
    assert "--birdclaw-export" in captured[0]
    assert "--bhiksha-root" in captured[2]
    assert captured[2][captured[2].index("--bhiksha-root") + 1] == str(
        Path.cwd().resolve()
    )


def test_authenticated_no_plan_stops_before_plan_sheet_and_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = _contract(tmp_path)
    window = value["target_session_window"]
    false_cart_effects = {
        "broker": False,
        "orders": False,
        "auth": False,
        "schedule": False,
        "external_send": False,
    }
    export_root = tmp_path / "cartographer-no-plan"
    export_root.mkdir()
    supporting = {
        "freshness-evidence.json": {"schema": "fixture.freshness.v1"},
        "normalized-inputs/index.json": {"schema": "fixture.index.v1"},
        "universe-manifest.json": {"schema": "fixture.universe.v1"},
    }
    manifest_body = {
        "schema": "market_cartographer.market_context_no_plan.v1",
        "program_id": "morning-market-scenario-selection-shadow.v1",
        "experiment_family_id": "market-context-shadow.v1",
        "experiment_version": "v1",
        "campaign_id": value["campaign_id"],
        "run_id": value["run_id"],
        "status": "no_plan",
        "reason": "all_symbols_quarantined",
        "target_session_date": value["target_session_date"],
        "target_session_window": window,
        "target_session_window_hash": value["target_session_window_hash"],
        "effects": false_cart_effects,
    }
    export_hash = canonical_sha256(manifest_body)
    manifest = {
        **manifest_body,
        "export_id": f"no-plan:{export_hash[:16]}",
        "export_hash": export_hash,
    }
    payloads = {"manifest.json": manifest, **supporting}
    for relative, payload in payloads.items():
        path = export_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    artifacts = [
        {
            "path": relative,
            "content_hash": (
                export_hash
                if relative == "manifest.json"
                else canonical_sha256(payload)
            ),
        }
        for relative, payload in sorted(payloads.items())
    ]
    cart_body = {
        "schema": "market_cartographer.market_context_receipt.v2",
        "status": "no_plan",
        "run_id": value["run_id"],
        "export_id": manifest["export_id"],
        "export_hash": export_hash,
        "target_session_date": value["target_session_date"],
        "target_session_window": window,
        "target_session_window_hash": value["target_session_window_hash"],
        "data_mode": "fixture",
        "candidate_pool_hash": None,
        "arm_a_selection_hash": None,
        "materialized_scenario_count": 0,
        "artifacts": artifacts,
        "effects": false_cart_effects,
    }
    cart_receipt = {**cart_body, "receipt_hash": canonical_sha256(cart_body)}
    cart_receipt_path = export_root / "receipt.json"
    cart_receipt_path.write_text(json.dumps(cart_receipt), encoding="utf-8")
    value.update(
        {
            "outcome": "no_plan",
            "cartographer_receipt": str(cart_receipt_path),
            "birdclaw_export": None,
            "birdclaw_packet_hash": None,
            "birdclaw_output_hash": None,
            "narrative_source_failure": None,
            "plan_source": None,
            "projection_request": None,
        }
    )
    value["content_hash"] = canonical_sha256(
        {key: item for key, item in value.items() if key != "content_hash"}
    )
    contract = _validate_contract(value)
    run_root = (
        Path(str(contract["tradelab_experiment_root"]))
        / "campaigns"
        / str(contract["campaign_id"])
        / "runs"
        / str(contract["run_id"])
    )
    preparation_body = {
        "schema": "tradelab.market_context_daily_preparation_receipt.v1",
        "status": "no_plan",
        "reason": "all_symbols_quarantined",
        "campaign_id": contract["campaign_id"],
        "run_id": contract["run_id"],
        "run_root": str(run_root),
        "run_manifest_hash": "sha256:" + "1" * 64,
        "target_session_date": contract["target_session_date"],
        "target_session_window": contract["target_session_window"],
        "target_session_window_hash": contract["target_session_window_hash"],
        "cartographer_receipt_hash": cart_receipt["receipt_hash"],
        "cartographer_export_hash": export_hash,
        "chart_input_verification_hash": "sha256:" + "2" * 64,
        "shadow_plan_hash": None,
        "projection_receipt_hash": None,
        "effects": {
            "sheet_write": False,
            "broker": False,
            "orders": False,
            "auth_mutation": False,
            "live_plan": False,
            "schedule": False,
        },
    }
    preparation = {
        **preparation_body,
        "content_hash": "sha256:" + canonical_sha256(preparation_body),
    }
    persisted = run_root / "outputs" / "preparation-receipt.json"
    persisted.parent.mkdir(parents=True)
    persisted.write_text(json.dumps(preparation), encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        coordinator,
        "_run_tradelab_lifecycle",
        lambda *_args, **_kwargs: {
            "action": "tradelab:prepare-run",
            "status": "succeeded",
            "lifecycle_receipt": preparation,
        },
    )
    for forbidden in (
        "_install_or_verify_plan",
        "_observe_once",
        "_stage_tradelab_evidence",
        "_project_if_present",
        "_export_birdclaw_context",
    ):
        monkeypatch.setattr(
            coordinator,
            forbidden,
            lambda *_args, _name=forbidden, **_kwargs: (_ for _ in ()).throw(
                AssertionError(f"no-plan invoked {_name}")
            ),
        )

    morning = _run_phase(contract, phase="morning")
    intraday = _run_phase(contract, phase="intraday")
    after_close = _run_phase(contract, phase="after-close")

    assert morning["status"] == "succeeded"
    assert morning["outcome"] == "no_plan"
    assert intraday["status"] == "skipped"
    assert after_close["status"] == "skipped"
    assert intraday["reason"] == after_close["reason"] == "authenticated_no_plan"
    assert not (
        tmp_path
        / "artifacts/chart_scenarios/runs/campaign-1/run-2026-08-04/active_shadow_plan.json"
    ).exists()


def test_bhiksha_stages_exact_contiguous_cycle_receipts_for_tradelab(
    tmp_path: Path,
) -> None:
    value = _contract(tmp_path)
    value["run_id"] = "run-1"
    receipt_path = Path(str(value["cartographer_receipt"]))
    cartographer = json.loads(receipt_path.read_text(encoding="utf-8"))
    cartographer["run_id"] = "run-1"
    cartographer["receipt_hash"] = canonical_sha256(
        {key: item for key, item in cartographer.items() if key != "receipt_hash"}
    )
    receipt_path.write_text(json.dumps(cartographer), encoding="utf-8")
    run_root = (
        Path(str(value["tradelab_experiment_root"])) / "campaigns/campaign-1/runs/run-1"
    )
    value["plan_source"] = str(run_root / "outputs/shadow-plan.json")
    value["projection_request"] = str(run_root / "outputs/sheet-upsert-request.json")
    value["content_hash"] = canonical_sha256(
        {key: item for key, item in value.items() if key != "content_hash"}
    )
    contract = _validate_contract(value)
    source = tmp_path / "artifacts" / "chart_scenarios" / "source-run"
    cycles = source / "cycles"
    inputs = source / "cycle-inputs"
    cycles.mkdir(parents=True)
    inputs.mkdir(parents=True)
    plan_path = source / "active_shadow_plan.json"
    install = source / "install.receipt.json"
    events = source / "events.json"
    install_shadow_plan(_bundle_payload(), output_path=plan_path, receipt_path=install)
    database = source / "events.sqlite3"
    cycle_path = inputs / "slot-0001.cycle-input.json"
    cycle = _cycle_input(_plan())
    cycle_path.write_text(json.dumps(cycle), encoding="utf-8")
    run_observation_cycle(
        _plan(),
        cycle,
        repository=ScenarioEventRepository(database),
        receipt_path=cycles / "slot-0001.receipt.json",
        cycle_input_path=cycle_path,
    )
    paths = SimpleNamespace(
        root=source,
        plan=plan_path,
        install_receipt=install,
        cycle_inputs=inputs,
        cycle_receipts=cycles,
        events_export=events,
        database=database,
    )
    _export_events(paths)

    _stage_tradelab_evidence(contract, paths)

    staged = (
        Path(contract["tradelab_experiment_root"])
        / "campaigns"
        / "campaign-1"
        / "runs"
        / "run-1"
        / "bhiksha"
    )
    assert sorted(path.name for path in (staged / "cycle-receipts").iterdir()) == [
        "cycle-0001.receipt.json",
    ]
    assert (staged / "install-receipt.json").is_file()
    assert (staged / "events.json").is_file()
    assert sorted(path.name for path in (staged / "cycle-inputs").iterdir()) == [
        "cycle-0001.json",
    ]
    assert staged.is_symlink()

    exported = json.loads(events.read_text(encoding="utf-8"))["events"]
    cycle_receipt = json.loads(
        (cycles / "slot-0001.receipt.json").read_text(encoding="utf-8")
    )
    covered = [
        event_hash
        for evidence in cycle_receipt["durable_slot_evidence"]
        for event_hash in evidence["event_hashes"]
    ]
    with pytest.raises(ValueError, match="covered exactly once"):
        _validate_staging_event_partition(
            exported, covered_event_hashes=covered[:-1], plan=_plan()
        )
    with pytest.raises(ValueError, match="covered exactly once"):
        _validate_staging_event_partition(
            exported, covered_event_hashes=[*covered, covered[0]], plan=_plan()
        )
    with pytest.raises(ValueError, match="covered exactly once"):
        _validate_staging_event_partition(
            exported, covered_event_hashes=[*covered, "f" * 64], plan=_plan()
        )
    omitted_install = next(
        event for event in exported if event["event_type"] == "installed"
    )
    with pytest.raises(ValueError, match="one installed event"):
        _validate_staging_event_partition(
            [event for event in exported if event is not omitted_install],
            covered_event_hashes=covered,
            plan=_plan(),
        )
    wrong_type = json.loads(json.dumps(exported))
    next(event for event in wrong_type if event["event_type"] == "installed")[
        "event_type"
    ] = "watching"
    with pytest.raises(ValueError, match="covered exactly once"):
        _validate_staging_event_partition(
            wrong_type, covered_event_hashes=covered, plan=_plan()
        )

    (staged / "events.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="content drift"):
        _stage_tradelab_evidence(contract, paths)

    outside_generation = tmp_path / "outside-generation"
    outside_generation.mkdir()
    escaped_generation = tmp_path / "artifacts/chart_scenarios/escaped-generation"
    escaped_generation.symlink_to(outside_generation)
    with pytest.raises(ValueError, match="not an immutable directory"):
        _validate_existing_staging_generation(escaped_generation, expected={})


def test_failed_cycle_receipt_never_advances_completed_slot(tmp_path: Path) -> None:
    source = tmp_path / "source"
    cycles = source / "cycles"
    inputs = source / "cycle-inputs"
    cycles.mkdir(parents=True)
    inputs.mkdir()
    receipt_body = {
        "schema_version": "bhiksha.chart-scenario-cycle-receipt.v4",
        "status": "failed",
        "observation_slot_ordinal": 1,
    }
    (cycles / "slot-0001.receipt.json").write_text(
        json.dumps({**receipt_body, "receipt_hash": canonical_sha256(receipt_body)}),
        encoding="utf-8",
    )
    (inputs / "slot-0001.cycle-input.json").write_text("{}", encoding="utf-8")
    paths = SimpleNamespace(root=source, cycle_inputs=inputs, cycle_receipts=cycles)

    with pytest.raises(ValueError):
        _completed_cycle_slot(paths, plan=_plan())


def test_daily_contract_must_match_authenticated_cartographer_window(
    tmp_path: Path,
) -> None:
    value = _contract(tmp_path)
    value["target_session_window"] = _window(end_at="2026-08-04T13:00:00-04:00")
    value["target_session_window_hash"] = canonical_sha256(
        value["target_session_window"]
    )
    value["content_hash"] = canonical_sha256(
        {key: item for key, item in value.items() if key != "content_hash"}
    )

    with pytest.raises(ValueError, match="authenticated Cartographer session"):
        _validate_contract(value)
