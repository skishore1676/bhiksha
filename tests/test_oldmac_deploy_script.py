from pathlib import Path


def test_oldmac_deploy_script_does_not_copy_runtime_secrets_or_state() -> None:
    script = Path("scripts/deploy_oldmac.sh").read_text(encoding="utf-8")

    assert "--exclude \"config/schwab_tokens.json\"" in script
    assert "--exclude \"config/public_session.json\"" in script
    assert "--exclude \".env\"" in script
    assert "--exclude \"artifacts\"" in script
    assert "--exclude \"bhiksha.db\"" in script
    assert "--delete" not in script


def test_oldmac_deploy_script_refuses_dirty_tree_by_default() -> None:
    script = Path("scripts/deploy_oldmac.sh").read_text(encoding="utf-8")

    assert "status --porcelain" in script
    assert "BHIKSHA_DEPLOY_ALLOW_DIRTY" in script
    assert "rev-parse --short HEAD" in script
    guard_index = script.index("status --porcelain")
    rsync_index = script.index("rsync -az")
    assert guard_index < rsync_index


def test_legacy_scheduler_scripts_stay_archived() -> None:
    retired = [
        "scripts/cron_run_bhiksha.sh",
        "scripts/cron_ensure_bhiksha_running.sh",
        "scripts/launchd_start_bhiksha.sh",
        "scripts/launchd_stop_bhiksha.sh",
    ]

    for path in retired:
        assert not Path(path).exists()


def test_bhiksha_launchd_runner_is_the_scheduler_entrypoint() -> None:
    script = Path("scripts/launchd/run_bhiksha_job.sh").read_text(encoding="utf-8")
    installer = Path("scripts/launchd/install_bhiksha_launchd.sh").read_text(encoding="utf-8")

    assert "bhiksha.tools.launchd_job" in script
    assert "com.bhiksha.live-start" in installer
    assert "com.bhiksha.live-watchdog" in installer
