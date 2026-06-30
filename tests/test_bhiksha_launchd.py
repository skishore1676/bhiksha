from pathlib import Path


def test_bhiksha_launchd_installer_owns_non_openclaw_labels() -> None:
    script = Path("scripts/launchd/install_bhiksha_launchd.sh").read_text(encoding="utf-8")

    assert "com.bhiksha.live-start" in script
    assert "com.bhiksha.live-watchdog" in script
    assert "com.bhiksha.live-stop" in script
    assert "com.bhiksha.schwab-guard" in script
    assert "com.bhiksha.session-report" in script
    assert "ai.openclaw.bhiksha" not in script


def test_bhiksha_launchd_installer_has_three_session_report_times() -> None:
    script = Path("scripts/launchd/install_bhiksha_launchd.sh").read_text(encoding="utf-8")

    assert "weekdays(9, 10)" in script
    assert "weekdays(11, 45)" in script
    assert "weekdays(14, 45)" in script


def test_bhiksha_launchd_runner_points_at_bhiksha_policy_module() -> None:
    script = Path("scripts/launchd/run_bhiksha_job.sh").read_text(encoding="utf-8")

    assert "bhiksha.tools.launchd_job" in script
    assert "PYTHONPATH=src" in script
