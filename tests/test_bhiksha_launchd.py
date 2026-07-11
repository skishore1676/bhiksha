from pathlib import Path

from bhiksha.ops.launchd_registry import active_launchd_jobs


def test_bhiksha_launchd_installer_owns_non_openclaw_labels() -> None:
    script = Path("scripts/launchd/install_bhiksha_launchd.sh").read_text(encoding="utf-8")

    assert "com.bhiksha.live-start" in script
    assert "com.bhiksha.live-watchdog" in script
    assert "com.bhiksha.live-stop" in script
    assert "com.bhiksha.schwab-guard" in script
    assert "com.bhiksha.session-report" in script
    assert "ai.openclaw.bhiksha" not in script


def test_exit_edge_launchd_enable_is_explicit_persistent_for_start_and_watchdog() -> None:
    script = Path("scripts/launchd/install_bhiksha_launchd.sh").read_text(encoding="utf-8")
    assert "BHIKSHA_INSTALL_EXIT_EDGE_LIVE_SHADOW_ENABLED" in script
    assert '"com.bhiksha.live-start", "com.bhiksha.live-watchdog"' in script
    assert '"BHIKSHA_EXIT_EDGE_LIVE_SHADOW_ENABLED": "true"' in script
    assert "exit_edge_live_shadow.enabled" in script


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
