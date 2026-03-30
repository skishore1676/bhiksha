from pathlib import Path

from bhiksha.config.environment import load_dotenv
from bhiksha.execution.brokers.public.settings import PublicBrokerSettings


def test_load_dotenv_sets_missing_values(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("PUBLIC_SECRET_TOKEN=test-secret\nAPI_REQUESTS_PER_SECOND=7\n", encoding="utf-8")

    monkeypatch.delenv("PUBLIC_SECRET_TOKEN", raising=False)
    monkeypatch.delenv("API_REQUESTS_PER_SECOND", raising=False)

    load_dotenv(env_file)
    settings = PublicBrokerSettings.from_env()

    assert settings.public_secret_token == "test-secret"
    assert settings.api_requests_per_second == 7.0

