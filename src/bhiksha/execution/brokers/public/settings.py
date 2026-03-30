"""Environment-backed settings for the Public broker integration."""

from __future__ import annotations

import os

from pydantic import BaseModel, Field

from bhiksha.config.environment import load_dotenv


class PublicBrokerSettings(BaseModel):
    public_secret_token: str | None = None
    public_api_base_url: str = "https://api.public.com"
    public_auth_endpoint: str = "https://api.public.com/userapiauthservice/personal/access-tokens"
    api_requests_per_second: float = 5.0
    api_burst_limit: int = 5
    session_file: str = "config/public_session.json"

    @classmethod
    def from_env(cls) -> "PublicBrokerSettings":
        load_dotenv()
        return cls(
            public_secret_token=os.getenv("PUBLIC_SECRET_TOKEN"),
            public_api_base_url=os.getenv("PUBLIC_API_BASE_URL", "https://api.public.com"),
            public_auth_endpoint=os.getenv(
                "PUBLIC_AUTH_ENDPOINT",
                "https://api.public.com/userapiauthservice/personal/access-tokens",
            ),
            api_requests_per_second=float(os.getenv("API_REQUESTS_PER_SECOND", 5.0)),
            api_burst_limit=int(os.getenv("API_BURST_LIMIT", 5)),
            session_file=os.getenv("PUBLIC_SESSION_FILE", "config/public_session.json"),
        )

    def validate_credentials(self) -> None:
        if not self.public_secret_token:
            raise ValueError("PUBLIC_SECRET_TOKEN is not set")
        if self.public_api_base_url.rstrip("/").endswith("/sandbox"):
            raise ValueError("PUBLIC_API_BASE_URL points to sandbox; switch to https://api.public.com for live trading")
