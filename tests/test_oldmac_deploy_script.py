from pathlib import Path


def test_oldmac_deploy_script_does_not_copy_runtime_secrets_or_state() -> None:
    script = Path("scripts/deploy_oldmac.sh").read_text(encoding="utf-8")

    assert "--exclude \"config/schwab_tokens.json\"" in script
    assert "--exclude \"config/public_session.json\"" in script
    assert "--exclude \".env\"" in script
    assert "--exclude \"artifacts\"" in script
    assert "--exclude \"bhiksha.db\"" in script
    assert "--delete" not in script


def test_cron_run_uses_server_session_logging_contract() -> None:
    script = Path("scripts/cron_run_bhiksha.sh").read_text(encoding="utf-8")

    assert "bhiksha.tools.server_session restart --live" in script
    assert "bhiksha.tools.trade_session" not in script
    assert "cron_output.log" not in script
