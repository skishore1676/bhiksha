"""Bhiksha-owned operator alerts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
from typing import Any, Literal

AlertMode = Literal["off", "spool", "live"]

_URL_SECRET_RE = re.compile(r"(code|session|client_id)=([^&\s]+)")
_TOKEN_FIELD_RE = re.compile(r'("?access_token"?|"?refresh_token"?|"?id_token"?)([=:]\s*"?)?[^",\s}]+')
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._-]+")
_DANGER_LEVELS = {"error", "critical", "fatal", "failure", "failed"}
_WARNING_LEVELS = {"warning", "warn"}


@dataclass(slots=True)
class AlertResult:
    attempted: bool = False
    ok: bool = False
    mode: AlertMode = "off"
    command: list[str] | None = None
    cwd: str | None = None
    return_code: int | None = None
    live_send_requested: bool | None = None
    network_call_performed: bool | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def send_lathi_alert(
    *,
    title: str,
    body: str,
    level: str = "error",
    mode: AlertMode = "live",
    profile: str | None = None,
    command: list[str] | None = None,
    cwd: str | Path | None = None,
    timeout_seconds: float | None = None,
) -> AlertResult:
    """Send a short operator alert through Lathi Bus.

    Lathi Bus is the transport. Bhiksha owns the decision to alert and the
    alert body, so scheduled trading health does not depend on OpenClaw to
    notice that Bhiksha could not make progress.
    """
    if mode == "off":
        return AlertResult(mode="off")

    profile = profile or os.getenv("BHIKSHA_LATHI_PROFILE", "jarvis-northstar")
    if command is None:
        command, cwd = _default_lathi_invocation(cwd)
    cwd_path = Path(cwd).expanduser() if cwd else None
    args = [
        *command,
        "telegram-notify",
        "--profile",
        profile,
        "--title",
        _decorate_title(title, level),
        "--body",
        _redact(_decorate_body(body, level)),
        "--level",
        level,
    ]
    if mode == "live":
        args.append("--live")

    try:
        env = os.environ.copy()
        _populate_secret_fallbacks(env)
        completed = subprocess.run(  # noqa: S603
            args,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd_path) if cwd_path else None,
            env=env,
            timeout=timeout_seconds or float(os.getenv("BHIKSHA_ALERT_TIMEOUT_SECONDS", "30")),
        )
    except Exception as exc:
        return AlertResult(
            attempted=True,
            ok=False,
            mode=mode,
            command=args,
            cwd=str(cwd_path) if cwd_path else None,
            error=_redact(str(exc)),
        )

    receipt = _parse_lathi_receipt(completed.stdout)
    live_send_requested = _optional_bool(receipt.get("live_send_requested"))
    network_call_performed = _optional_bool(receipt.get("network_call_performed"))
    ok = completed.returncode == 0
    if mode == "live":
        ok = ok and network_call_performed is True

    return AlertResult(
        attempted=True,
        ok=ok,
        mode=mode,
        command=args,
        cwd=str(cwd_path) if cwd_path else None,
        return_code=completed.returncode,
        live_send_requested=live_send_requested,
        network_call_performed=network_call_performed,
        stdout_tail=_tail(_redact(completed.stdout)),
        stderr_tail=_tail(_redact(completed.stderr)),
    )


def _default_lathi_invocation(cwd: str | Path | None = None) -> tuple[list[str], str | Path | None]:
    raw = os.getenv("BHIKSHA_LATHI_BUS_CMD", "").strip()
    if raw:
        return shlex.split(raw), cwd or os.getenv("BHIKSHA_LATHI_BUS_CWD") or None
    configured_cwd = cwd or os.getenv("BHIKSHA_LATHI_BUS_CWD")
    if configured_cwd:
        return _lathi_bus_module_invocation(Path(configured_cwd).expanduser())
    for candidate in (Path.home() / "code" / "lathi-bus", Path("/Users/sunny/code/lathi-bus")):
        if (candidate / "lathi_bus" / "cli.py").is_file():
            return _lathi_bus_module_invocation(candidate)
    if shutil.which("lathi-bus"):
        return ["lathi-bus"], None
    return ["lathi-bus"], None


def _lathi_bus_module_invocation(repo_root: Path) -> tuple[list[str], Path]:
    python = repo_root / ".venv" / "bin" / "python"
    if python.is_file() and os.access(python, os.X_OK):
        return [str(python), "-m", "lathi_bus.cli"], repo_root
    return ["python3", "-m", "lathi_bus.cli"], repo_root


def _populate_secret_fallbacks(env: dict[str, str]) -> None:
    token_fallback = Path.home() / ".lane-host" / "secrets" / "telegram_gate_token"
    user_fallback = Path.home() / ".lane-host" / "secrets" / "telegram_gate_user_id"
    chat_fallback = Path.home() / ".lane-host" / "secrets" / "telegram_gate_chat_id"
    if "LATHI_BUS_TG_TOKEN_FILE" not in env and token_fallback.is_file():
        env["LATHI_BUS_TG_TOKEN_FILE"] = str(token_fallback)
    if "LATHI_BUS_TG_USER_ID_FILE" not in env and user_fallback.is_file():
        env["LATHI_BUS_TG_USER_ID_FILE"] = str(user_fallback)
    if "LATHI_BUS_TG_CHAT_ID_FILE" not in env and chat_fallback.is_file():
        env["LATHI_BUS_TG_CHAT_ID_FILE"] = str(chat_fallback)


def _tail(text: str, *, max_lines: int = 40) -> str:
    return "\n".join(text.splitlines()[-max_lines:])


def _parse_lathi_receipt(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _decorate_title(title: str, level: str) -> str:
    normalized = level.lower().strip()
    if normalized in _DANGER_LEVELS:
        return f"🚨🚨🚨 BHIKSHA FAILURE: {title}"
    if normalized in _WARNING_LEVELS:
        return f"⚠️ BHIKSHA WARNING: {title}"
    return title


def _decorate_body(body: str, level: str) -> str:
    normalized = level.lower().strip()
    if normalized in _DANGER_LEVELS:
        return "\n".join(
            [
                "🔴🔴🔴 ACTION REQUIRED 🔴🔴🔴",
                "🚨 BHIKSHA FAILURE - DO NOT IGNORE",
                "",
                body,
                "",
                "⚠️ Trading may be blocked or fail closed until this is fixed.",
            ]
        )
    if normalized in _WARNING_LEVELS:
        return "\n".join(["⚠️ BHIKSHA WARNING", "", body])
    return body


def _redact(text: str) -> str:
    text = _URL_SECRET_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    return _TOKEN_FIELD_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
