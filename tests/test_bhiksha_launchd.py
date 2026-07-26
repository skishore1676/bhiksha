import os
from pathlib import Path
import plistlib
import subprocess

from bhiksha.ops.launchd_registry import active_launchd_jobs


def test_bhiksha_launchd_installer_owns_non_openclaw_labels() -> None:
    script = Path("scripts/launchd/install_bhiksha_launchd.sh").read_text(encoding="utf-8")

    assert "com.bhiksha.live-start" in script
    assert "com.bhiksha.live-watchdog" in script
    assert "com.bhiksha.reconciliation-supervisor" in script
    assert "com.bhiksha.live-stop" in script
    assert "com.bhiksha.schwab-guard" in script
    assert "com.bhiksha.session-report" in script
    assert "ai.openclaw.bhiksha" not in script


def test_exit_edge_launchd_enable_is_explicit_persistent_for_start_and_watchdog() -> None:
    script = Path("scripts/launchd/install_bhiksha_launchd.sh").read_text(encoding="utf-8")
    assert "BHIKSHA_INSTALL_EXIT_EDGE_LIVE_SHADOW_ENABLED" in script
    assert '"com.bhiksha.live-start", "com.bhiksha.live-watchdog"' in script
    assert (
        'environment["BHIKSHA_EXIT_EDGE_LIVE_SHADOW_ENABLED"] = "true"'
        in script
    )
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
        "BHIKSHA_INSTALL_EXIT_EDGE_LIVE_SHADOW_ENABLED": "true",
        "BHIKSHA_ACTIVE_PLAN_ID": (
            "active_plan_2026-07-27_exit_engine_v2_iwm_canary"
        ),
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


def test_bhiksha_launchd_has_one_friday_decision_review_and_no_duplicate_publishers() -> None:
    jobs = {job.runner_job: job for job in active_launchd_jobs()}
    weekly = jobs["weekly-trading-decisions"]

    assert weekly.schedule == ({"Weekday": 5, "Hour": 16, "Minute": 0},)
    assert weekly.skips_non_trading_days is False
    assert "weekly-scorecard" not in jobs
    assert "shadow-ev-report" not in jobs

    script = Path("scripts/launchd/install_bhiksha_launchd.sh").read_text(encoding="utf-8")
    assert "RETIRED $retired_label" in script


def test_bhiksha_launchd_runner_points_at_bhiksha_policy_module() -> None:
    script = Path("scripts/launchd/run_bhiksha_job.sh").read_text(encoding="utf-8")

    assert "bhiksha.tools.launchd_job" in script
    assert "PYTHONPATH=src" in script


def test_retired_weekly_calculators_are_not_live_publish_jobs() -> None:
    source = Path("src/bhiksha/tools/launchd_job.py").read_text(encoding="utf-8")

    assert '"weekly-scorecard"' not in source
    assert '"shadow-ev-report"' not in source
    assert "def _weekly_scorecard_job" not in source
    assert "def _shadow_ev_report_job" not in source
