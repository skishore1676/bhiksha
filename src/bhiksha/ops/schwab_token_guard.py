"""Schwab token guard owned by Bhiksha."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import re
import signal
import subprocess
from typing import Any, Literal

from bhiksha.integrations.schwab import auth as schwab_auth
from bhiksha.integrations.schwab.settings import SchwabSettings
from bhiksha.integrations.schwab.token_store import read_tokens
from bhiksha.market_data.trading_calendar import next_trading_session_day, next_trading_session_required_through
from bhiksha.ops.alerts import AlertMode, AlertResult, send_lathi_alert
from bhiksha.ops.schwab_health import SchwabHealthResult, run_schwab_healthcheck

TokenState = Literal[
    "healthy",
    "access_token_stale",
    "refresh_token_near_expiry",
    "refresh_token_expired",
    "token_file_missing",
    "invalid_token_payload",
]

BrowserRenewalMode = Literal["off", "auto", "force"]

_TOKEN_FIELD_RE = re.compile(
    r'("?access_token"?|"?refresh_token"?|"?id_token"?|"?app_secret"?|"?client_secret"?)'
    r'([=:]\s*"?)?[^",\s}]+',
    re.IGNORECASE,
)
_URL_SECRET_RE = re.compile(r"(code|state|session(?:_?id)?|client_id)=([^&\s]+)", re.IGNORECASE)
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._-]+", re.IGNORECASE)
_BASIC_RE = re.compile(r"Authorization:\s*Basic\s+[A-Za-z0-9+/=]+", re.IGNORECASE)


@dataclass(slots=True)
class SchwabTokenSnapshot:
    state: TokenState
    token_file: str
    access_token_issued: str | None = None
    refresh_token_issued: str | None = None
    refresh_token_expires_at: str | None = None
    refresh_token_trusted_until: str | None = None
    refresh_token_days_left: float | None = None
    next_trading_session: str | None = None
    required_through_at: str | None = None
    refresh_token_survives_next_session: bool | None = None
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
    health: SchwabHealthResult | None = None
    alert: AlertResult = field(default_factory=AlertResult)
    attention_required: bool = False
    failure_kind: str | None = None
    error: str | None = None
    receipt_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_schwab_token_state(
    settings: SchwabSettings,
    *,
    refresh_lead_days: float | None = None,
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
        access_issued = datetime.fromisoformat(access_issued_raw).astimezone(UTC)
        refresh_issued = datetime.fromisoformat(refresh_issued_raw).astimezone(UTC)
        expires_at = refresh_issued + timedelta(days=7)
        trusted_until = expires_at - timedelta(minutes=30)
        days_left = (expires_at - now).total_seconds() / 86400
        session_day = next_trading_session_day(now)
        required_through = next_trading_session_required_through(now)
        survives_next_session = trusted_until > required_through.astimezone(UTC)
        inside_optional_lead = refresh_lead_days is not None and now >= expires_at - timedelta(days=refresh_lead_days)
        if now >= trusted_until:
            state: TokenState = "refresh_token_expired"
            detail = "refresh token expired or inside expiry buffer"
        elif not survives_next_session or inside_optional_lead:
            state = "refresh_token_near_expiry"
            detail = (
                f"refresh token will not survive the {session_day.isoformat()} trading session"
                if not survives_next_session
                else f"refresh token inside optional {refresh_lead_days:g} day renewal window"
            )
        elif now >= access_issued + timedelta(minutes=29):
            state = "access_token_stale"
            detail = "access token should be refreshed"
        else:
            state = "healthy"
            detail = "token healthy"
        return SchwabTokenSnapshot(
            state=state,
            token_file=token_file,
            access_token_issued=access_issued_raw,
            refresh_token_issued=refresh_issued_raw,
            refresh_token_expires_at=expires_at.isoformat(),
            refresh_token_trusted_until=trusted_until.isoformat(),
            refresh_token_days_left=round(days_left, 4),
            next_trading_session=session_day.isoformat(),
            required_through_at=required_through.isoformat(),
            refresh_token_survives_next_session=survives_next_session,
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
    refresh_lead_days: float | None = None,
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
    browser_requested = False
    error: str | None = None

    state = initial.state
    if browser_renewal_mode == "force":
        action = "browser_renewal_forced"
        browser_requested = True
        browser = _invoke_browser_renewal(browser_renewal_cmd, force=True)
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
                browser_requested = True
                browser = _invoke_browser_renewal(browser_renewal_cmd, force=True)
            else:
                action = "direct_refresh_failed"
    elif state == "refresh_token_near_expiry":
        # The token will not survive the next full trading session. Refresh the
        # short-lived access token first, then proactively mint a new refresh
        # token while the existing grant still works.
        direct_refresh_attempted = True
        try:
            await schwab_auth.refresh_access_token(settings)
            direct_refresh_ok = True
        except Exception as exc:
            error = _redact(str(exc))
        if browser_renewal_mode in {"auto"}:
            action = "refresh_token_near_expiry_browser_renewal"
            browser_requested = True
            browser = _invoke_browser_renewal(browser_renewal_cmd, force=True)
        else:
            action = "direct_refresh" if direct_refresh_ok else "direct_refresh_failed"
    elif state in {"refresh_token_expired", "token_file_missing", "invalid_token_payload"}:
        if browser_renewal_mode == "auto":
            action = f"{state}_browser_renewal"
            browser_requested = True
            browser = _invoke_browser_renewal(browser_renewal_cmd, force=True)
        else:
            action = f"{state}_operator_required"
    else:
        action = f"{state}_operator_required"

    final = classify_schwab_token_state(settings, refresh_lead_days=refresh_lead_days, now=now)
    renewal_advanced = initial.refresh_token_issued != final.refresh_token_issued
    health = None
    if browser_requested and browser.invoked and browser.return_code == 0 and renewal_advanced:
        try:
            health = await asyncio.wait_for(
                run_schwab_healthcheck(settings=settings),
                timeout=float(os.getenv("BHIKSHA_SCHWAB_POST_RENEWAL_HEALTH_TIMEOUT_SECONDS", "120")),
            )
        except TimeoutError:
            health = SchwabHealthResult(ok=False, error="healthcheck_timeout")
    browser_failed = browser_requested and (
        not browser.invoked
        or browser.return_code != 0
        or not renewal_advanced
        or health is None
        or not health.ok
    )
    ok = final.state == "healthy" and not browser_failed
    if error and not direct_refresh_ok and not browser.invoked:
        ok = False
    attention_required = not ok
    failure_kind = _failure_kind(final.state, browser_failed=browser_failed, error=error)
    alert = AlertResult(mode=alert_mode)
    # Operator preference (2026-07-07): notify ONLY when a re-auth ATTEMPT
    # actually failed (or the token is otherwise unusable) — NOT on a silent
    # successful proactive renewal at the near-expiry mark. A near-expiry run
    # that browser-renews cleanly resets the 7-day clock and needs no ping;
    # ``near_expiry_needs_attention`` is intentionally NOT an alert trigger.
    alert_needed = attention_required

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
        health=health,
        alert=alert,
        attention_required=attention_required,
        failure_kind=failure_kind,
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


def _failure_kind(state: TokenState, *, browser_failed: bool, error: str | None) -> str | None:
    if browser_failed:
        return "browser_renewal_failed"
    if state == "refresh_token_expired":
        return "schwab_authentication_expired"
    if state == "refresh_token_near_expiry":
        return "schwab_authentication_renewal_required"
    if state in {"token_file_missing", "invalid_token_payload"}:
        return "schwab_authentication_unavailable"
    if error or state == "access_token_stale":
        return "schwab_access_refresh_failed"
    return None


def _invoke_browser_renewal(command: list[str] | None, *, force: bool = False) -> BrowserRenewalResult:
    command = command or _default_browser_renewal_command()
    if not command:
        return BrowserRenewalResult(invoked=False, stderr_tail="browser renewal command not configured")
    receipt_command = [_redact(part) for part in command]
    env = os.environ.copy()
    env.setdefault("BROWSER_AGENT_NOTIFY", "0")
    if force:
        env["FORCE"] = "1"
    try:
        process = subprocess.Popen(  # noqa: S603
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        timeout = float(os.getenv("BHIKSHA_SCHWAB_BROWSER_RENEWAL_TIMEOUT_SECONDS", "900"))
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        timeout_stdout = _subprocess_text(exc.stdout)
        timeout_stderr = _subprocess_text(exc.stderr)
        stopped_stdout, stopped_stderr = _stop_process_group(process)
        stdout = "\n".join(part for part in (timeout_stdout, stopped_stdout) if part)
        stderr = "\n".join(part for part in (timeout_stderr, stopped_stderr) if part)
        detail = f"browser renewal timed out after {timeout:g}s"
        return BrowserRenewalResult(
            invoked=True,
            command=receipt_command,
            return_code=124,
            stdout_tail=_tail(_redact(stdout)),
            stderr_tail=_tail(_redact("\n".join(part for part in (stderr, detail) if part))),
        )
    except OSError as exc:
        return BrowserRenewalResult(
            invoked=True,
            command=receipt_command,
            return_code=127,
            stderr_tail=_tail(_redact(f"browser renewal could not start: {exc}")),
        )
    return BrowserRenewalResult(
        invoked=True,
        command=receipt_command,
        return_code=process.returncode,
        stdout_tail=_tail(_redact(stdout)),
        stderr_tail=_tail(_redact(stderr)),
    )


def _stop_process_group(process: subprocess.Popen[str]) -> tuple[str, str]:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        return process.communicate()


def _subprocess_text(value: str | bytes | None) -> str:
    return value.decode(errors="replace") if isinstance(value, bytes) else (value or "")


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
    if result.health is not None:
        lines.append(
            "Post-renewal Schwab proof: "
            + ("linked account and QQQ/IWM quote/chain checks passed" if result.health.ok else "FAILED")
        )
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
                "Proactive browser renewal FAILED. The token is not trusted for the next full "
                "trading session; live startup must remain blocked until renewal succeeds."
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
    text = _BASIC_RE.sub("Authorization: Basic <redacted>", text)
    return _TOKEN_FIELD_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
