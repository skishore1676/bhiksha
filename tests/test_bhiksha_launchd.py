import hashlib
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from bhiksha.ops.launchd_registry import active_launchd_jobs, registered_launchd_jobs


def test_bhiksha_launchd_installer_owns_non_openclaw_labels() -> None:
    script = Path("scripts/launchd/install_bhiksha_launchd.sh").read_text(
        encoding="utf-8"
    )

    assert "com.bhiksha.live-start" in script
    assert "com.bhiksha.live-watchdog" in script
    assert "com.bhiksha.reconciliation-supervisor" in script
    assert "com.bhiksha.live-stop" in script
    assert "com.bhiksha.schwab-guard" in script
    assert "com.bhiksha.session-report" in script
    assert "ai.openclaw.bhiksha" not in script


def test_chart_scenario_schedule_is_registered_but_install_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("BHIKSHA_INSTALL_CHART_SCENARIO_SHADOW_ENABLED", raising=False)
    assert "chart-scenario-shadow" not in {
        job.runner_job for job in active_launchd_jobs()
    }
    registered = {job.runner_job: job for job in registered_launchd_jobs()}
    chart = registered["chart-scenario-shadow"]
    assert chart.install_opt_in_env == ("BHIKSHA_INSTALL_CHART_SCENARIO_SHADOW_ENABLED")
    times = {(item["Hour"], item["Minute"]) for item in chart.schedule}
    assert {
        (7, 45),
        (7, 55),
        (8, 5),
        (8, 15),
        (8, 30),
        (15, 0),
        (15, 15),
    } <= times

    monkeypatch.setenv("BHIKSHA_INSTALL_CHART_SCENARIO_SHADOW_ENABLED", "true")
    assert "chart-scenario-shadow" in {job.runner_job for job in active_launchd_jobs()}


def test_exit_edge_launchd_enable_is_explicit_persistent_for_start_and_watchdog() -> (
    None
):
    script = Path("scripts/launchd/install_bhiksha_launchd.sh").read_text(
        encoding="utf-8"
    )
    assert "BHIKSHA_INSTALL_EXIT_EDGE_LIVE_SHADOW_ENABLED" in script
    assert '"com.bhiksha.live-start", "com.bhiksha.live-watchdog"' in script
    assert 'environment["BHIKSHA_EXIT_EDGE_LIVE_SHADOW_ENABLED"] = "true"' in script
    assert "exit_edge_live_shadow.enabled" in script


def test_installer_persists_stable_plan_id_only_for_live_restart_jobs(
    tmp_path,
) -> None:
    repo = Path.cwd().resolve()
    launchd_dir = tmp_path / "LaunchAgents"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    launchctl = fake_bin / "launchctl"
    launchctl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    launchctl.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "BHIKSHA_REPO_ROOT": str(repo),
        "BHIKSHA_LAUNCHD_DIR": str(launchd_dir),
        "BHIKSHA_LAUNCHD_LOG_DIR": str(tmp_path / "logs"),
        "BHIKSHA_RUNTIME_FLAG_DIR": str(tmp_path / "flags"),
        "BHIKSHA_CHART_SCENARIO_ARTIFACT_ROOT": str(
            tmp_path / "artifacts/chart_scenarios"
        ),
        "BHIKSHA_INSTALL_EXIT_EDGE_LIVE_SHADOW_ENABLED": "true",
        "BHIKSHA_ACTIVE_PLAN_ID": ("active_plan_2026-07-27_exit_engine_v2_iwm_canary"),
    }

    subprocess.run(
        ["bash", "scripts/launchd/install_bhiksha_launchd.sh", "install"],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    for path in launchd_dir.glob("*.plist"):
        payload = plistlib.loads(path.read_bytes())
        if payload["Label"] in {
            "com.bhiksha.live-start",
            "com.bhiksha.live-watchdog",
        }:
            assert payload["EnvironmentVariables"] == {
                "BHIKSHA_ACTIVE_PLAN_ID": (
                    "active_plan_2026-07-27_exit_engine_v2_iwm_canary"
                ),
                "BHIKSHA_EXIT_EDGE_LIVE_SHADOW_ENABLED": "true",
            }
        else:
            assert "EnvironmentVariables" not in payload


def test_chart_scenario_install_pins_kernel_and_existing_env_file(tmp_path) -> None:
    repo = Path.cwd().resolve()
    launchd_dir = tmp_path / "LaunchAgents"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    launchctl = fake_bin / "launchctl"
    launchctl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    launchctl.chmod(0o755)
    kernel_src = tmp_path / "kernel" / "src"
    (kernel_src / "mala_bhiksha_kernel").mkdir(parents=True)
    env_file = tmp_path / "production.env"
    env_file.write_text("SCHWAB_CLIENT_ID=not-read-by-test\n", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "BHIKSHA_REPO_ROOT": str(repo),
        "BHIKSHA_LAUNCHD_DIR": str(launchd_dir),
        "BHIKSHA_LAUNCHD_LOG_DIR": str(
            tmp_path / "artifacts/chart_scenarios/launchd/logs"
        ),
        "BHIKSHA_RUNTIME_FLAG_DIR": str(tmp_path / "artifacts/chart_scenarios/launchd"),
        "BHIKSHA_CHART_SCENARIO_ARTIFACT_ROOT": str(
            tmp_path / "artifacts/chart_scenarios"
        ),
        "BHIKSHA_INSTALL_CHART_SCENARIO_SHADOW_ENABLED": "true",
        "BHIKSHA_KERNEL_SRC": str(kernel_src),
        "BHIKSHA_PYTHON": sys.executable,
        "BHIKSHA_ENV_FILE": str(env_file),
    }

    subprocess.run(
        [
            "bash",
            "scripts/launchd/install_bhiksha_launchd.sh",
            "install-chart-scenario-shadow",
        ],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    payload = plistlib.loads(
        (launchd_dir / "com.bhiksha.chart-scenario-shadow.plist").read_bytes()
    )
    python_realpath = Path(sys.executable).resolve()
    version_result = subprocess.run(
        [sys.executable, "--version"],
        check=True,
        text=True,
        capture_output=True,
    )
    python_version = (version_result.stdout or version_result.stderr).strip()
    runner_path = repo / "scripts/launchd/run_bhiksha_job.sh"
    repo_commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    assert payload["EnvironmentVariables"] == {
        "BHIKSHA_CHART_SCENARIO_ARTIFACT_ROOT": str(
            tmp_path / "artifacts/chart_scenarios"
        ),
        "BHIKSHA_CHART_PYTHON_REALPATH": str(python_realpath),
        "BHIKSHA_CHART_PYTHON_SHA256": hashlib.sha256(
            python_realpath.read_bytes()
        ).hexdigest(),
        "BHIKSHA_CHART_PYTHON_VERSION": python_version,
        "BHIKSHA_CHART_REPO_COMMIT": repo_commit,
        "BHIKSHA_CHART_RUNNER_SHA256": hashlib.sha256(
            runner_path.read_bytes()
        ).hexdigest(),
        "BHIKSHA_ENV_FILE": str(env_file),
        "BHIKSHA_KERNEL_SRC": str(kernel_src),
        "BHIKSHA_PYTHON": sys.executable,
    }
    runner = Path("scripts/launchd/run_bhiksha_job.sh").read_text(encoding="utf-8")
    assert "BHIKSHA_KERNEL_SRC" in runner
    assert "chart-scenario-shadow requires an absolute BHIKSHA_PYTHON" in runner


def test_scoped_chart_install_does_not_rewrite_or_reload_live_jobs(tmp_path) -> None:
    repo = Path.cwd().resolve()
    launchd_dir = tmp_path / "LaunchAgents"
    launchd_dir.mkdir()
    live_plist = launchd_dir / "com.bhiksha.live-start.plist"
    sentinel = b"production-live-plist-sentinel"
    live_plist.write_bytes(sentinel)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    launch_log = tmp_path / "launchctl.log"
    launchctl = fake_bin / "launchctl"
    launchctl.write_text(
        f'#!/usr/bin/env bash\necho "$*" >> {launch_log}\n',
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    kernel_src = tmp_path / "kernel" / "src"
    (kernel_src / "mala_bhiksha_kernel").mkdir(parents=True)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "BHIKSHA_REPO_ROOT": str(repo),
        "BHIKSHA_LAUNCHD_DIR": str(launchd_dir),
        "BHIKSHA_LAUNCHD_LOG_DIR": str(
            tmp_path / "artifacts/chart_scenarios/launchd/logs"
        ),
        "BHIKSHA_RUNTIME_FLAG_DIR": str(tmp_path / "artifacts/chart_scenarios/launchd"),
        "BHIKSHA_CHART_SCENARIO_ARTIFACT_ROOT": str(
            tmp_path / "artifacts/chart_scenarios"
        ),
        "BHIKSHA_INSTALL_CHART_SCENARIO_SHADOW_ENABLED": "true",
        "BHIKSHA_KERNEL_SRC": str(kernel_src),
        "BHIKSHA_PYTHON": sys.executable,
    }

    subprocess.run(
        [
            "bash",
            "scripts/launchd/install_bhiksha_launchd.sh",
            "install-chart-scenario-shadow",
        ],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert live_plist.read_bytes() == sentinel
    calls = launch_log.read_text(encoding="utf-8")
    assert "com.bhiksha.chart-scenario-shadow" in calls
    assert "com.bhiksha.live-start" not in calls

    marker = (
        tmp_path / "artifacts/chart_scenarios/launchd/chart_scenario_shadow.enabled"
    )
    assert marker.exists()
    subprocess.run(
        [
            "bash",
            "scripts/launchd/install_bhiksha_launchd.sh",
            "uninstall-chart-scenario-shadow",
        ],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert not marker.exists()
    assert live_plist.read_bytes() == sentinel


@pytest.mark.parametrize("escaped", ["launchd", "logs", "marker"])
def test_scoped_chart_install_rejects_symlinked_effect_paths(
    tmp_path, escaped: str
) -> None:
    repo = Path.cwd().resolve()
    chart_root = tmp_path / "artifacts/chart_scenarios"
    chart_launchd = chart_root / "launchd"
    chart_launchd.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    launchd_dir = tmp_path / "LaunchAgents"
    logs = chart_launchd / "logs"
    marker_dir = chart_launchd
    if escaped == "launchd":
        launchd_dir.symlink_to(outside)
    else:
        launchd_dir.mkdir()
    if escaped == "logs":
        logs.symlink_to(outside)
    else:
        logs.mkdir()
    if escaped == "marker":
        marker_dir = chart_root / "marker-link"
        marker_dir.symlink_to(outside)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    launchctl = fake_bin / "launchctl"
    launchctl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    launchctl.chmod(0o755)
    path_python = fake_bin / "python3"
    path_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path_python.chmod(0o755)
    kernel_src = tmp_path / "kernel/src"
    (kernel_src / "mala_bhiksha_kernel").mkdir(parents=True)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "BHIKSHA_REPO_ROOT": str(repo),
        "BHIKSHA_LAUNCHD_DIR": str(launchd_dir),
        "BHIKSHA_LAUNCHD_LOG_DIR": str(logs),
        "BHIKSHA_RUNTIME_FLAG_DIR": str(marker_dir),
        "BHIKSHA_CHART_SCENARIO_ARTIFACT_ROOT": str(chart_root),
        "BHIKSHA_INSTALL_CHART_SCENARIO_SHADOW_ENABLED": "true",
        "BHIKSHA_KERNEL_SRC": str(kernel_src),
        "BHIKSHA_PYTHON": sys.executable,
    }
    completed = subprocess.run(
        [
            "bash",
            "scripts/launchd/install_bhiksha_launchd.sh",
            "install-chart-scenario-shadow",
        ],
        cwd=repo,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    assert not (outside / "com.bhiksha.chart-scenario-shadow.plist").exists()
    assert not (outside / "chart_scenario_shadow.enabled").exists()


def test_chart_runner_uses_only_installed_chart_marker(tmp_path) -> None:
    repo = tmp_path / "isolated-bhiksha"
    repo.mkdir()
    package = repo / "src/bhiksha/tools"
    package.mkdir(parents=True)
    (repo / "src/bhiksha/__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "launchd_job.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "Path(os.environ['CAPTURE']).write_text("
        "os.environ['BHIKSHA_CHART_SCENARIO_SHADOW_ENABLED'])\n",
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text("/artifacts/\n", encoding="utf-8")
    chart_root = repo / "artifacts/chart_scenarios"
    chart_launchd = chart_root / "launchd"
    chart_launchd.mkdir(parents=True)
    playbook_flags = repo / "artifacts/playbook/runtime_flags"
    playbook_flags.mkdir(parents=True)
    capture = tmp_path / "capture.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python3 = fake_bin / "python3"
    fake_python3.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    fake_python3.chmod(0o755)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
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
    repo_commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    python = Path(sys.executable)
    python_realpath = python.resolve()
    version = subprocess.run(
        [str(python), "--version"],
        check=True,
        text=True,
        capture_output=True,
    )
    runner = Path("scripts/launchd/run_bhiksha_job.sh").resolve()
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "BHIKSHA_REPO_ROOT": str(repo),
        "BHIKSHA_CHART_SCENARIO_ARTIFACT_ROOT": str(chart_root),
        "BHIKSHA_PYTHON": str(python),
        "BHIKSHA_CHART_PYTHON_REALPATH": str(python_realpath),
        "BHIKSHA_CHART_PYTHON_SHA256": hashlib.sha256(
            python_realpath.read_bytes()
        ).hexdigest(),
        "BHIKSHA_CHART_PYTHON_VERSION": (version.stdout or version.stderr).strip(),
        "BHIKSHA_CHART_RUNNER_SHA256": hashlib.sha256(runner.read_bytes()).hexdigest(),
        "BHIKSHA_CHART_REPO_COMMIT": repo_commit,
        "CAPTURE": str(capture),
    }
    marker = chart_launchd / "chart_scenario_shadow.enabled"
    marker.write_text("", encoding="utf-8")
    subprocess.run(
        ["bash", "scripts/launchd/run_bhiksha_job.sh", "chart-scenario-shadow"],
        cwd=Path.cwd(),
        env=env,
        check=True,
    )
    assert capture.read_text(encoding="utf-8") == "true"

    marker.unlink()
    (playbook_flags / "chart_scenario_shadow.enabled").write_text("", encoding="utf-8")
    subprocess.run(
        ["bash", "scripts/launchd/run_bhiksha_job.sh", "chart-scenario-shadow"],
        cwd=Path.cwd(),
        env=env,
        check=True,
    )
    assert capture.read_text(encoding="utf-8") == "false"


def test_generic_install_omits_plan_id_and_blank_explicit_value_fails(
    tmp_path,
) -> None:
    repo = Path.cwd().resolve()
    launchd_dir = tmp_path / "LaunchAgents"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    launchctl = fake_bin / "launchctl"
    launchctl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    launchctl.chmod(0o755)
    base_env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "BHIKSHA_REPO_ROOT": str(repo),
        "BHIKSHA_LAUNCHD_DIR": str(launchd_dir),
        "BHIKSHA_LAUNCHD_LOG_DIR": str(tmp_path / "logs"),
        "BHIKSHA_RUNTIME_FLAG_DIR": str(tmp_path / "flags"),
        "BHIKSHA_CHART_SCENARIO_ARTIFACT_ROOT": str(
            tmp_path / "artifacts/chart_scenarios"
        ),
    }
    base_env.pop("BHIKSHA_ACTIVE_PLAN_ID", None)
    base_env.pop("BHIKSHA_INSTALL_EXIT_EDGE_LIVE_SHADOW_ENABLED", None)

    subprocess.run(
        ["bash", "scripts/launchd/install_bhiksha_launchd.sh", "install"],
        cwd=repo,
        env=base_env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert all(
        "EnvironmentVariables" not in plistlib.loads(path.read_bytes())
        for path in launchd_dir.glob("*.plist")
    )

    failed = subprocess.run(
        ["bash", "scripts/launchd/install_bhiksha_launchd.sh", "install"],
        cwd=repo,
        env={**base_env, "BHIKSHA_ACTIVE_PLAN_ID": "  "},
        check=False,
        capture_output=True,
        text=True,
    )
    assert failed.returncode != 0
    assert "nonblank stable id" in failed.stderr


def test_exit_edge_restart_paths_read_persistent_allowlisted_marker() -> None:
    runner = Path("scripts/launchd/run_bhiksha_job.sh").read_text(encoding="utf-8")
    assert "runtime_flags/exit_edge_live_shadow.enabled" in runner
    assert "export BHIKSHA_EXIT_EDGE_LIVE_SHADOW_ENABLED=true" in runner
    assert "export BHIKSHA_EXIT_EDGE_LIVE_SHADOW_ENABLED=false" in runner
    assert "chart_scenario_shadow.enabled" in runner
    assert "runtime_flags/chart_scenario_shadow.enabled" not in runner
    assert "export BHIKSHA_CHART_SCENARIO_SHADOW_ENABLED=true" in runner
    assert "export BHIKSHA_CHART_SCENARIO_SHADOW_ENABLED=false" in runner


def test_bhiksha_launchd_installer_has_three_session_report_times() -> None:
    jobs = {job.runner_job: job for job in active_launchd_jobs()}
    session_report = jobs["session-report"]
    times = {(entry["Hour"], entry["Minute"]) for entry in session_report.schedule}

    assert (9, 10) in times
    assert (11, 45) in times
    assert (14, 45) in times


def test_reconciliation_supervisor_runs_independently_every_ten_minutes() -> None:
    jobs = {job.runner_job: job for job in active_launchd_jobs()}
    supervisor = jobs["reconciliation-supervisor"]

    assert supervisor.label == "com.bhiksha.reconciliation-supervisor"
    assert supervisor.schedule == jobs["live-watchdog"].schedule
    assert supervisor.risk_class == "trading_safety_observer"
    assert supervisor.allowed_manual_actions == ()


def test_schwab_guard_has_premarket_and_after_close_checks() -> None:
    jobs = {job.runner_job: job for job in active_launchd_jobs()}
    guard = jobs["schwab-refresh"]
    times = {(entry["Hour"], entry["Minute"]) for entry in guard.schedule}

    assert times == {(7, 10), (15, 20)}
    assert "renew-schwab-access" in guard.allowed_manual_actions
    assert "renew-schwab-access" in guard.requires_confirmation_actions


def test_bhiksha_launchd_has_one_friday_decision_review_and_no_duplicate_publishers() -> (
    None
):
    jobs = {job.runner_job: job for job in active_launchd_jobs()}
    weekly = jobs["weekly-trading-decisions"]

    assert weekly.schedule == ({"Weekday": 5, "Hour": 16, "Minute": 0},)
    assert weekly.skips_non_trading_days is False
    assert "weekly-scorecard" not in jobs
    assert "shadow-ev-report" not in jobs

    script = Path("scripts/launchd/install_bhiksha_launchd.sh").read_text(
        encoding="utf-8"
    )
    assert "RETIRED $retired_label" in script


def test_bhiksha_launchd_runner_points_at_bhiksha_policy_module() -> None:
    script = Path("scripts/launchd/run_bhiksha_job.sh").read_text(encoding="utf-8")

    assert "bhiksha.tools.launchd_job" in script
    assert (
        'PYTHONPATH="$REPO_ROOT/src${BHIKSHA_KERNEL_SRC:+:$BHIKSHA_KERNEL_SRC}"'
        in script
    )


def test_retired_weekly_calculators_are_not_live_publish_jobs() -> None:
    source = Path("src/bhiksha/tools/launchd_job.py").read_text(encoding="utf-8")

    assert '"weekly-scorecard"' not in source
    assert '"shadow-ev-report"' not in source
    assert "def _weekly_scorecard_job" not in source
    assert "def _shadow_ev_report_job" not in source
