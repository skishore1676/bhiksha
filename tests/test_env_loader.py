from pathlib import Path

from bhiksha.config.environment import get_mala_evidence_sheet_name, load_dotenv
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


def test_get_mala_evidence_sheet_name_prefers_clear_env(monkeypatch) -> None:
    monkeypatch.setenv("MALA_EVIDENCE_SHEET_NAME", "Mala_Evidence_v1")
    monkeypatch.setenv("STRATEGY_CATALOG_SHEET_NAME", "Strategy_Catalog")

    assert get_mala_evidence_sheet_name() == "Mala_Evidence_v1"


def test_get_mala_evidence_sheet_name_supports_legacy_alias(monkeypatch) -> None:
    monkeypatch.delenv("MALA_EVIDENCE_SHEET_NAME", raising=False)
    monkeypatch.setenv("STRATEGY_CATALOG_SHEET_NAME", "Mala_Evidence_v1")

    assert get_mala_evidence_sheet_name() == "Mala_Evidence_v1"
