from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from mala_bhiksha_kernel import canonical_sha256

import bhiksha.tools.chart_scenario_coordinator as coordinator
from bhiksha.tools.chart_scenario_coordinator import (
    _before_prepare_cutoff,
    _phase,
    _prepare_daily_contract,
    _require_preopen_completion,
    _resolve_daily_contract,
    _run_tradelab_lifecycle,
    _stage_tradelab_evidence,
    _validate_campaign_config,
    _validate_contract,
    _verify_kernel_source,
)


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
    body: dict[str, object] = {
        "schema": "bhiksha.chart-scenario-coordinator-contract.v1",
        "campaign_id": "campaign-1",
        "run_id": "run-2026-08-04",
        "target_session_date": "2026-08-04",
        "target_session_window": window,
        "target_session_window_hash": canonical_sha256(window),
        "cartographer_receipt": str(receipt_path),
        "birdclaw_export": str(birdclaw_path),
        "birdclaw_packet_hash": birdclaw["packet_hash"],
        "birdclaw_output_hash": birdclaw["output_hash"],
        "narrative_source_failure": None,
        "campaign_config_hash": "c" * 64,
        "tradelab_checkout": "/tmp/tradelab",
        "tradelab_experiment_root": str(experiment_root),
        "agent_broker": "/Users/sunny/code/agent-broker/agent-broker",
        "spreadsheet_id": "sheet-1",
        "kernel_src": "/tmp/kernel/src",
        "plan_source": str(run_root / "outputs" / "shadow-plan.json"),
        "projection_request": str(
            run_root / "outputs" / "sheet-upsert-request.json"
        ),
    }
    return {**body, "content_hash": canonical_sha256(body)}


def test_coordinator_contract_is_content_addressed_and_session_phases_are_explicit(
    tmp_path: Path,
) -> None:
    contract = _validate_contract(_contract(tmp_path))

    assert contract["run_id"] == "run-2026-08-04"
    window = contract["target_session_window"]
    assert _phase(datetime.fromisoformat("2026-08-04T07:45:00-05:00"), window) == "morning"
    assert _phase(datetime.fromisoformat("2026-08-04T10:00:00-05:00"), window) == "intraday"
    assert _phase(datetime.fromisoformat("2026-08-04T15:15:00-05:00"), window) == "after-close"


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

    assert _resolve_daily_contract(
        directory, datetime.fromisoformat("2026-08-04T07:45:00-05:00")
    ) == first
    assert _resolve_daily_contract(
        directory, datetime.fromisoformat("2026-08-05T07:45:00-05:00")
    ) == second


def test_preparation_cutoff_allows_bounded_retries_only_before_open() -> None:
    assert _before_prepare_cutoff(
        datetime.fromisoformat("2026-08-04T08:15:00-05:00")
    )
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mala_bhiksha_kernel

    kernel_src = Path(str(mala_bhiksha_kernel.__file__)).resolve().parent.parent
    monkeypatch.setenv("BHIKSHA_KERNEL_SRC", str(kernel_src))
    _verify_kernel_source()

    monkeypatch.setenv("BHIKSHA_KERNEL_SRC", "/tmp/not-the-loaded-kernel")
    with pytest.raises(ValueError, match="not from reviewed"):
        _verify_kernel_source()


def test_campaign_config_autonomously_emits_authenticated_daily_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    body: dict[str, object] = {
        "schema": "bhiksha.chart-scenario-campaign-config.v1",
        "campaign_id": "campaign-1",
        "birdclaw_checkout": str(birdclaw),
        "birdclaw_db": str(birdclaw_db),
        "market_cartographer_checkout": str(cartographer),
        "tradelab_checkout": str(tmp_path / "tradelab"),
        "tradelab_experiment_root": str(
            tmp_path / "artifacts" / "chart_scenarios" / "tradelab"
        ),
        "agent_broker": "/Users/sunny/code/agent-broker/agent-broker",
        "spreadsheet_id": "sheet-1",
        "kernel_src": str(tmp_path / "kernel" / "src"),
        "cartographer_provider": "mala",
        "cartographer_data_root": str(tmp_path / "bars"),
        "symbols": ["IWM", "SPY"],
    }
    config = _validate_campaign_config(
        {**body, "content_hash": canonical_sha256(body)}
    )
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

    monkeypatch.setattr(coordinator, "_run_command", fake_run)
    monkeypatch.setattr(coordinator, "_execute_command", fake_execute)
    contract_dir = (
        tmp_path / "artifacts" / "chart_scenarios" / "daily-contracts"
    )

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
    assert payload["birdclaw_packet_hash"]
    assert birdclaw_envs[0]["BIRDCLAW_DB"] == str(birdclaw_db)
    assert str(birdclaw_db) not in json.dumps(payload)


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

    monkeypatch.setattr(coordinator, "_run_command", fake_run)

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


def test_bhiksha_stages_exact_contiguous_cycle_receipts_for_tradelab(
    tmp_path: Path,
) -> None:
    contract = _validate_contract(_contract(tmp_path))
    source = tmp_path / "artifacts" / "chart_scenarios" / "source-run"
    cycles = source / "cycles"
    inputs = source / "cycle-inputs"
    cycles.mkdir(parents=True)
    inputs.mkdir(parents=True)
    install = source / "install.json"
    events = source / "events.json"
    install.write_text(json.dumps({"status": "installed"}), encoding="utf-8")
    events.write_text(json.dumps({"events": []}), encoding="utf-8")
    for slot in (1, 2):
        (cycles / f"slot-{slot:04d}.receipt.json").write_text(
            json.dumps({"slot": slot}), encoding="utf-8"
        )
        (inputs / f"slot-{slot:04d}.json").write_text(
            json.dumps({"slot": slot}), encoding="utf-8"
        )
    paths = SimpleNamespace(
        install_receipt=install,
        cycle_inputs=inputs,
        cycle_receipts=cycles,
        events_export=events,
    )

    _stage_tradelab_evidence(contract, paths)

    staged = (
        Path(contract["tradelab_experiment_root"])
        / "campaigns"
        / "campaign-1"
        / "runs"
        / "run-2026-08-04"
        / "bhiksha"
    )
    assert sorted(path.name for path in (staged / "cycle-receipts").iterdir()) == [
        "cycle-0001.receipt.json",
        "cycle-0002.receipt.json",
    ]
    assert (staged / "install-receipt.json").is_file()
    assert (staged / "events.json").is_file()
    assert sorted(path.name for path in (staged / "cycle-inputs").iterdir()) == [
        "cycle-0001.json",
        "cycle-0002.json",
    ]


def test_daily_contract_must_match_authenticated_cartographer_window(
    tmp_path: Path,
) -> None:
    value = _contract(tmp_path)
    value["target_session_window"] = _window(
        end_at="2026-08-04T13:00:00-04:00"
    )
    value["target_session_window_hash"] = canonical_sha256(
        value["target_session_window"]
    )
    value["content_hash"] = canonical_sha256(
        {key: item for key, item in value.items() if key != "content_hash"}
    )

    with pytest.raises(ValueError, match="authenticated Cartographer session"):
        _validate_contract(value)
