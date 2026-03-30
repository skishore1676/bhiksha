import json
import time
from datetime import UTC, datetime, timedelta

from bhiksha.app.token_daemon import PublicTokenRefreshDaemon, SchwabTokenRefreshDaemon
from bhiksha.execution.brokers.public.settings import PublicBrokerSettings
from bhiksha.integrations.schwab.settings import SchwabSettings


def test_public_token_daemon_refresh_delay_uses_expiration_buffer(tmp_path) -> None:
    session_file = tmp_path / "public_session.json"
    session_file.write_text(
        json.dumps(
            {
                "access_token": "abc",
                "expiration_timestamp": time.time() + 900,
            }
        ),
        encoding="utf-8",
    )
    settings = PublicBrokerSettings(
        public_secret_token="secret",
        session_file=str(session_file),
    )

    delay = PublicTokenRefreshDaemon.next_refresh_delay_seconds(settings, buffer_seconds=300)

    assert 590 <= delay <= 600


def test_schwab_token_daemon_refresh_delay_uses_access_issue_time(tmp_path) -> None:
    token_file = tmp_path / "schwab_tokens.json"
    access_issued = datetime.now(UTC) - timedelta(minutes=10)
    token_file.write_text(
        json.dumps(
            {
                "access_token_issued": access_issued.isoformat(),
                "refresh_token_issued": access_issued.isoformat(),
                "token_dictionary": {
                    "access_token": "abc",
                    "refresh_token": "refresh",
                },
            }
        ),
        encoding="utf-8",
    )
    settings = SchwabSettings(
        app_key="key",
        app_secret="secret",
        token_file=str(token_file),
    )

    delay = SchwabTokenRefreshDaemon.next_refresh_delay_seconds(settings, buffer_seconds=300)

    assert 880 <= delay <= 905
