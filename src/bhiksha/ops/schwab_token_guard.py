"""Schwab token guard owned by Bhiksha."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Literal

from bhiksha.integrations.schwab import auth as schwab_auth
from bhiksha.integrations.schwab.settings import SchwabSettings
from bhiksha.integrations.schwab.token_store import read_tokens
from bhiksha.ops.alerts import AlertMode, AlertResult, send_lathi_alert

TokenState = Literal[
    "healthy",
    "access_token_stale",
    "refresh_token_near_expiry",
    "refresh_token_expired",
    "token_file_missing",
    "invalid_token_payload",
]

BrowserRenewalMode = Literal["off", "auto", "force"]

_TOKEN_FIELD_RE = re.compile(r'("?access_token"?|"?refresh_token"?|"?id_token"?)([=:]\s*"?)?[^",\s}]+')
_URL_SECRET_RE = re.compile(r"(code|session|client_id)=([^&\s]+)")
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._-]+")


@dataclass(slots=True)
class SchwabTokenSnapshot:
    state: TokenState
    token_file: str
    access_token_issued: str | None = None
    refresh_token_issued: str | None = None
    refresh_token_expires_at: str | None = None
    refresh_token_days_left: float | None = None
    detail: str = ""


@dataclass(slots=True)
class BrowserRenewalResult:
    invoked: bool = False
    command: list[str] = field(default_factory=list)
    return_code: int | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""


@dataclass(slots=True)
class SchwabTokenGuardResult:
    ok: bool
    mode: str
    checked_at: str
    initial: SchwabTokenSnapshot
    final: SchwabTokenSnapshot
    action: str
    direct_refresh_attempted: bool = False
    direct_refresh_ok: bool = False
    browser: BrowserRenewalResult = field(default_factory=BrowserRenewalResult)
    alert: AlertResult = field(default_factory=AlertResult)
    error: str | None = None
    receipt_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_schwab_token_state(
    settings: SchwabSettings,
    *,
    refresh_lead_days: float = 2.0,
    now: datetime | None = None,
) -> SchwabTokenSnapshot:
    now = now or datetime.now(UTC)
    token_file = str(settings.token_file)
    try:
        payload = read_tokens(settings.token_file)
    except Exception as exc:
        return SchwabTokenSnapshot(state="invalid_token_payload", token_file=token_file, detail=str(exc))
    if not payload:
        return SchwabTokenSnapshot(state="token_file_missing", token_file=token_file, detail="token file missing")

    try:
        access_issued_raw = str(payload["access_token_issued"])
        refresh_issued_raw = str(payload["refresh_token_issued"])
        refresh_issued = datetime.fromisoformat(refresh_issued_raw).astimezone(UTC)
        expires_at = refresh_issued + timedelta(days=7)
        days_left = (expires_at - now).total_seconds() / 86400
        if schwab_auth.refresh_token_is_expired(payload):
            state: TokenState = "refresh_token_expired"
            detail = "refresh token expired or inside expiry buffer"
        elif schwab_auth.access_token_is_stale(payload):
            state = "access_token_stale"
            detail = "access token should be refreshed"
        elif now >= expires_at - timedelta(days=refresh_lead_days):
            state = "refresh_token_near_expiry"
            detail = f"refresh token inside {refresh_lead_days:g} day renewal window"
        else:
            state = "healthy"
            detail = "token healthy"
        return SchwabTokenSnapshot(
            state=state,
            token_file=token_file,
            access_token_issued=access_issued_raw,
            refresh_token_issued=refresh_issued_raw,
            refresh_token_expires_at=expires_at.isoformat(),
            refresh_token_days_left=round(days_left, 4),
            detail=detail,
        )
    except Exception as exc:
        return SchwabTokenSnapshot(state="invalid_token_payload", token_file=token_file, detail=str(exc))


async def run_schwab_token_guard(
    *,
    settings: SchwabSettings | None = None,
    mode: str = "premarket",
    browser_renewal_mode: BrowserRenewalMode = "off",
    browser_renewal_cmd: list[str] | None = None,
    refresh_lead_days: float = 2.0,
    receipt_dir: Path | None = None,
    write_receipt: bool = True,
    alert_mode: AlertMode = "off",
    alert_profile: str | None = None,
    alert_command: list[str] | None = None,
    alert_cwd: str | Path | None = None,
    now: datetime | None = None,
) -> SchwabTokenGuardResult:
    settings = settings or SchwabSettings.from_env()
    checked_at = (now or datetime.now(UTC)).isoformat()
    initial = classify_schwab_token_state(settings, refresh_lead_days=refresh_lead_days, now=now)
    action = "none"
    direct_refresh_attempted = False
    direct_refresh_ok = False
    browser = BrowserRenewalResult()
    error: str | None = None

    state = initial.state
    if browser_renewal_mode == "force":
        action = "browser_renewal_forced"
        browser = _invoke_browser_renewal(browser_renewal_cmd)
    elif state in {"healthy"}:
        action = "healthy_noop"
    elif state == "access_token_stale":
        # Refresh-token-healthy, access-token-stale is the normal daily case:
        # only a direct refresh is needed, never a browser renewal.
        direct_refresh_attempted = True
        try:
            await schwab_auth.refresh_access_token(settings)
            direct_refresh_ok = True
            action = "direct_refresh"
        except Exception as exc:
            error = _redact(str(exc))
            if browser_renewal_mode == "auto":
                action = "direct_refresh_failed_browser_renewal"
                browser = _invoke_browser_renewal(browser_renewal_cmd)
            else:
                action = "direct_refresh_failed"
    elif state == "refresh_token_near_expiry":
        # Inside the renewal lead window: the refresh token still works today,
        # but waiting for it to actually expire before invoking browser
        # renewal is exactly the bug this branch exists to close (the browser
        # agent was previously invoked 0 times because near-expiry only ever
        # attempted a direct_refresh, which mints a new access token but not a
        # new refresh token). So: do the direct_refresh first (keeps the
        # access token fresh so trading/enrichment keeps working even if the
        # browser renewal below has trouble), then proactively invoke the
        # browser renewal to mint a NEW refresh token and reset the 7-day
        # clock while the current refresh token still works.
        direct_refresh_attempted = True
        try:
            await schwab_auth.refresh_access_token(settings)
            direct_refresh_ok = True
        except Exception as exc:
            error = _redact(str(exc))
        if browser_renewal_mode in {"auto"}:
            action = "refresh_token_near_expiry_browser_renewal"
            browser = _invoke_browser_renewal(browser_renewal_cmd)
        else:
            action = "direct_refresh" if direct_refresh_ok else "direct_refresh_failed"
    elif state in {"refresh_token_expired", "token_file_missing", "invalid_token_payload"}:
        if browser_renewal_mode == "auto":
            action = f"{state}_browser_renewal"
            browser = _invoke_browser_renewal(browser_renewal_cmd)
        else:
            action = f"{state}_operator_required"
    else:
        action = f"{state}_operator_required"

    final = classify_schwab_token_state(settings, refresh_lead_days=refresh_lead_days)
    ok = _state_is_usable(final.state)
    browser_failed = browser.invoked and browser.return_code != 0
    if browser_failed:
        # Near-expiry is a special case: the browser renewal here is
        # *proactive* (the refresh token is not dead yet), so a failure does
        # not make today unusable as long as the direct_refresh above kept
        # the access token fresh. It still needs operator attention before
        # the refresh token actually expires -- see alert_needed below.
        if state != "refresh_token_near_expiry" or not direct_refresh_ok:
            ok = False
    if error and not direct_refresh_ok and not browser.invoked:
        ok = False
    alert = AlertResult(mode=alert_mode)
    # Operator preference (2026-07-07): notify ONLY when a re-auth ATTEMPT
    # actually failed (or the token is otherwise unusable) — NOT on a silent
    # successful proactive renewal at the near-expiry mark. A near-expiry run
    # that browser-renews cleanly resets the 7-day clock and needs no ping;
    # ``near_expiry_needs_attention`` is intentionally NOT an alert trigger.
    alert_needed = (not ok) or browser_failed

    result = SchwabTokenGuardResult(
        ok=ok,
        mode=mode,
        checked_at=checked_at,
        initial=initial,
        final=final,
        action=action,
        direct_refresh_attempted=direct_refresh_attempted,
        direct_refresh_ok=direct_refresh_ok,
        browser=browser,
        alert=alert,
        error=error,
    )
    if write_receipt and receipt_dir is not None:
        result.receipt_path = str(_write_receipt(result, receipt_dir=receipt_dir))
    if alert_needed and alert_mode != "off":
        level = "error" if not ok else "warning"
        title = "Bhiksha Schwab token guard failed" if not ok else "Bhiksha Schwab token guard needs attention"
        result.alert = send_lathi_alert(
            title=title,
            body=_render_alert_body(result),
            level=level,
            mode=alert_mode,
            profile=alert_profile,
            command=alert_command,
            cwd=alert_cwd,
        )
        if write_receipt and receipt_dir is not None:
            result.receipt_path = str(_write_receipt(result, receipt_dir=receipt_dir))
    return result


def run_schwab_token_guard_sync(**kwargs: Any) -> SchwabTokenGuardResult:
    return asyncio.run(run_schwab_token_guard(**kwargs))


def _state_is_usable(state: TokenState) -> bool:
    return state in {"healthy", "access_token_stale", "refresh_token_near_expiry"}


def _invoke_browser_renewal(command: list[str] | None) -> BrowserRenewalResult:
    command = command or _default_browser_renewal_command()
    if not command:
        return BrowserRenewalResult(invoked=False, stderr_tail="browser renewal command not configured")
    env = os.environ.copy()
    env.setdefault("BROWSER_AGENT_NOTIFY", "0")
    completed = subprocess.run(  # noqa: S603
        command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=float(os.getenv("BHIKSHA_SCHWAB_BROWSER_RENEWAL_TIMEOUT_SECONDS", "900")),
    )
    return BrowserRenewalResult(
        invoked=True,
        command=command,
        return_code=completed.returncode,
        stdout_tail=_tail(_redact(completed.stdout)),
        stderr_tail=_tail(_redact(completed.stderr)),
    )


def _default_browser_renewal_command() -> list[str]:
    raw = os.getenv("BHIKSHA_SCHWAB_BROWSER_RENEWAL_CMD", "").strip()
    if raw:
        return ["/bin/bash", "-lc", raw]
    return []


def _write_receipt(result: SchwabTokenGuardResult, *, receipt_dir: Path) -> Path:
    receipt_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = receipt_dir / f"schwab_token_guard_{stamp}.json"
    path.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    latest = receipt_dir / "latest.json"
    latest.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _render_alert_body(result: SchwabTokenGuardResult) -> str:
    lines = [
        f"Mode: {result.mode}",
        f"Action: {result.action}",
        f"Initial state: {result.initial.state}",
        f"Final state: {result.final.state}",
        f"Browser renewal invoked: {result.browser.invoked}",
    ]
    if result.browser.return_code is not None:
        lines.append(f"Browser renewal rc: {result.browser.return_code}")
    if result.error:
        lines.append(f"Error: {result.error}")
    if result.receipt_path:
        lines.append(f"Receipt: {result.receipt_path}")
    if result.initial.state == "refresh_token_near_expiry":
        lines.append(
            f"Refresh token near expiry (days left: {result.initial.refresh_token_days_left}); "
            "proactive browser renewal to mint a new refresh token."
        )
        if result.browser.invoked and result.browser.return_code != 0:
            lines.append(
                "Proactive browser renewal FAILED. Access token is still fresh from direct_refresh "
                "(trading can continue today), but this needs operator attention before the current "
                "refresh token actually expires."
            )
    if result.ok:
        lines.append("Token is currently usable; trading can continue.")
    else:
        lines.append("Trading should fail closed until Schwab token health is restored.")
    return "\n".join(lines)


def _tail(text: str, *, max_lines: int = 40) -> str:
    return "\n".join(text.splitlines()[-max_lines:])


def _redact(text: str) -> str:
    text = _URL_SECRET_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    return _TOKEN_FIELD_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
