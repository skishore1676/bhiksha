"""Environment-backed settings for the Schwab integration."""

from __future__ import annotations

import os

from pydantic import BaseModel

from bhiksha.config.environment import load_dotenv


class SchwabSettings(BaseModel):
    app_key: str | None = None
    app_secret: str | None = None
    callback_url: str = "https://127.0.0.1:8080"
    authorize_url: str = "https://api.schwabapi.com/v1/oauth/authorize"
    token_url: str = "https://api.schwabapi.com/v1/oauth/token"
    api_base_url: str = "https://api.schwabapi.com"
    token_file: str = "config/schwab_tokens.json"
    timeout_seconds: float = 20.0

    @classmethod
    def from_env(cls) -> "SchwabSettings":
        load_dotenv()
        return cls(
            app_key=os.getenv("SCHWAB_APP_KEY"),
            app_secret=os.getenv("SCHWAB_APP_SECRET"),
            callback_url=os.getenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8080"),
            authorize_url=os.getenv("SCHWAB_AUTHORIZE_URL", "https://api.schwabapi.com/v1/oauth/authorize"),
            token_url=os.getenv("SCHWAB_TOKEN_URL", "https://api.schwabapi.com/v1/oauth/token"),
            api_base_url=os.getenv("SCHWAB_API_BASE_URL", "https://api.schwabapi.com"),
            token_file=os.getenv("SCHWAB_TOKEN_FILE", "config/schwab_tokens.json"),
            timeout_seconds=float(os.getenv("SCHWAB_TIMEOUT_SECONDS", 20.0)),
        )

    def validate_credentials(self) -> None:
        if not self.app_key:
            raise ValueError("SCHWAB_APP_KEY is not set")
        if not self.app_secret:
            raise ValueError("SCHWAB_APP_SECRET is not set")
        if not self.callback_url.startswith("https://"):
            raise ValueError("SCHWAB_CALLBACK_URL must start with https://")

    @property
    def callback_needs_attention(self) -> bool:
        """Flag the callback URL the user said is still pending approval."""
        return self.callback_url.rstrip("/") == "https://127.0.0.1:8182/callback"

