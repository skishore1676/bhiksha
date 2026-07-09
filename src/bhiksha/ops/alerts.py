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
import sys
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


ReviewMode = Literal["off", "on"]


@dataclass(slots=True)
class ReviewPublishResult:
    """Outcome of projecting an artifact onto the Obsidian review surface."""

    attempted: bool = False
    ok: bool = False
    mode: ReviewMode = "off"
    profile: str | None = None
    command: list[str] | None = None
    cwd: str | None = None
    return_code: int | None = None
    review_id: str | None = None
    note_path: str | None = None
    surface: str | None = None
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
    template: Literal["plain", "compact", "urgent_gate", "status"] | None = None,
    fields: dict[str, Any] | list[tuple[str, Any]] | None = None,
    link_preview: Literal["default", "disabled", "enabled", "large", "small", "above"] | None = None,
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
    if template:
        args.extend(["--template", template])
    for field in _format_fields(fields):
        args.extend(["--field", field])
    if link_preview:
        args.extend(["--link-preview", link_preview])
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


def publish_lathi_review(
    *,
    source: str | Path,
    title: str,
    mode: ReviewMode = "on",
    profile: str | None = None,
    workspace_root: str | Path | None = None,
    artifact_id: str | None = None,
    owner_consumer: str = "bhiksha",
    resume_command: str | None = None,
    review_id: str | None = None,
    command: list[str] | None = None,
    cwd: str | Path | None = None,
    timeout_seconds: float | None = None,
) -> ReviewPublishResult:
    """Project a reviewable artifact onto the Obsidian coding-agent surface.

    Follows the ``lathi-review-bus`` contract: the default profile
    ``coding-agent-northstar`` routes to folder ``07 Agents/Coding`` and the
    published card carries an approve/archive decision affordance (the CLI's
    ``publish`` default). Bhiksha owns the decision to publish and the artifact
    body; Lathi Bus owns the vault surface.

    Transport-graceful, mirroring ``send_lathi_alert``: a missing/unreachable
    bus, a missing source file, or a non-zero CLI exit returns a non-ok result
    with ``error`` set rather than raising, so a scheduled session-report job is
    never failed by the review projection being unavailable.
    """
    if mode == "off":
        return ReviewPublishResult(mode="off")

    profile = profile or os.getenv("BHIKSHA_OBSIDIAN_REVIEW_PROFILE", "coding-agent-northstar")
    # The bus CLI runs with cwd switched to the lathi-bus checkout (see
    # _default_lathi_invocation), so a caller-relative source path would be
    # resolved against the wrong directory there — absolutize it here.
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        return ReviewPublishResult(
            attempted=False,
            ok=False,
            mode=mode,
            profile=profile,
            error=f"source artifact not found: {source_path}",
        )

    if command is None:
        command, cwd = _default_lathi_invocation(cwd)
    cwd_path = Path(cwd).expanduser() if cwd else None

    args = [
        *command,
        "publish",
        "--profile",
        profile,
        "--source",
        str(source_path),
        "--title",
        title,
        "--owner-consumer",
        owner_consumer,
    ]
    if workspace_root:
        args.extend(["--workspace-root", str(Path(workspace_root).expanduser())])
    if artifact_id:
        args.extend(["--artifact-id", str(artifact_id)])
    if resume_command:
        args.extend(["--resume-command", resume_command])
    if review_id:
        args.extend(["--review-id", review_id])

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
        return ReviewPublishResult(
            attempted=True,
            ok=False,
            mode=mode,
            profile=profile,
            command=args,
            cwd=str(cwd_path) if cwd_path else None,
            error=_redact(str(exc)),
        )

    receipt = _parse_lathi_receipt(completed.stdout)
    note_path = _receipt_str(receipt.get("note_path"))
    ok = completed.returncode == 0 and note_path is not None
    return ReviewPublishResult(
        attempted=True,
        ok=ok,
        mode=mode,
        profile=profile,
        command=args,
        cwd=str(cwd_path) if cwd_path else None,
        return_code=completed.returncode,
        review_id=_receipt_str(receipt.get("review_id")),
        note_path=note_path,
        surface=_receipt_str(receipt.get("surface")),
        stdout_tail=_tail(_redact(completed.stdout)),
        stderr_tail=_tail(_redact(completed.stderr)),
        error=None if ok else _publish_error(completed),
    )


def _publish_error(completed: subprocess.CompletedProcess[str]) -> str:
    stderr_tail = _tail(_redact(completed.stderr)).strip()
    if stderr_tail:
        return stderr_tail
    return f"publish returned code {completed.returncode} without a note_path"


def _receipt_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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
    return [sys.executable, "-m", "lathi_bus.cli"], repo_root


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


def _format_fields(fields: dict[str, Any] | list[tuple[str, Any]] | None) -> list[str]:
    if not fields:
        return []
    items = fields.items() if isinstance(fields, dict) else fields
    formatted: list[str] = []
    for key, value in items:
        formatted.append(f"{_redact(str(key))}={_redact(str(value))}")
    return formatted


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
