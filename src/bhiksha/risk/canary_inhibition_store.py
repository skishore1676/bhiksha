"""Restart-durable one-way inhibition latches for live triage canaries.

Automated code may add a latch but this module intentionally exposes no clear,
delete, or re-enable operation.  A latch is keyed by both deployment and
canary identity, while deployment-level reads let the existing RiskManager
consult seam fail closed without widening its caller contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Any


_REPO_ROOT = Path(__file__).resolve().parents[3]
CANARY_INHIBITION_STORE_PATH_ENV = "BHIKSHA_CANARY_INHIBITION_STORE_PATH"
DEFAULT_CANARY_INHIBITION_STORE_PATH = (
    _REPO_ROOT
    / "artifacts"
    / "playbook"
    / "governance"
    / "live_triage_canary_inhibitions.json"
)


class CanaryInhibitionStoreError(RuntimeError):
    """The durable inhibition state could not be read or validated."""


def default_canary_inhibition_store_path() -> Path:
    configured = os.getenv(CANARY_INHIBITION_STORE_PATH_ENV)
    if configured and configured.strip():
        return Path(configured).expanduser()
    return DEFAULT_CANARY_INHIBITION_STORE_PATH


@dataclass(slots=True, frozen=True)
class CanaryInhibitionRecord:
    deployment_id: str
    canary_id: str
    latched_at: str
    reason: str
    evidence: dict[str, Any]

    @property
    def key(self) -> str:
        return _record_key(self.deployment_id, self.canary_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "deployment_id": self.deployment_id,
            "canary_id": self.canary_id,
            "latched_at": self.latched_at,
            "reason": self.reason,
            "evidence": dict(self.evidence),
        }


class CanaryInhibitionStore:
    """Atomic JSON store with append-only latch semantics."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = (
            Path(path)
            if path is not None
            else default_canary_inhibition_store_path()
        )

    @property
    def initialized_marker_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".initialized")

    def initialize(self) -> None:
        """Create the durable empty state once; fail if it later disappears.

        The marker is separate from the JSON payload so a deploy or operator
        mistake that removes only the latch file cannot silently reset a
        previously initialized one-way store.
        """

        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                marker_exists = self.initialized_marker_path.exists()
                state_exists = self.path.exists()
                if marker_exists and not state_exists:
                    raise CanaryInhibitionStoreError(
                        "initialized canary inhibition state is missing: "
                        f"{self.path}"
                    )
                if state_exists:
                    self.load()
                else:
                    self._atomic_write({})
                if not marker_exists:
                    self._atomic_write_marker()
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def load(self) -> dict[str, CanaryInhibitionRecord]:
        if not self.path.exists():
            if self.initialized_marker_path.exists():
                raise CanaryInhibitionStoreError(
                    "initialized canary inhibition state is missing: "
                    f"{self.path}"
                )
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CanaryInhibitionStoreError(
                f"cannot read canary inhibition store {self.path}: {exc}"
            ) from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise CanaryInhibitionStoreError(
                f"invalid canary inhibition store schema at {self.path}"
            )
        inhibitions = raw.get("inhibitions")
        if not isinstance(inhibitions, dict):
            raise CanaryInhibitionStoreError(
                f"invalid canary inhibition records at {self.path}"
            )
        records: dict[str, CanaryInhibitionRecord] = {}
        for key, payload in inhibitions.items():
            if not isinstance(payload, dict):
                raise CanaryInhibitionStoreError(
                    f"invalid canary inhibition record {key!r} at {self.path}"
                )
            try:
                record = CanaryInhibitionRecord(
                    deployment_id=_required_text(
                        payload.get("deployment_id"), "deployment_id"
                    ),
                    canary_id=_required_text(payload.get("canary_id"), "canary_id"),
                    latched_at=_required_text(payload.get("latched_at"), "latched_at"),
                    reason=_required_text(payload.get("reason"), "reason"),
                    evidence=(
                        dict(payload.get("evidence"))
                        if isinstance(payload.get("evidence"), dict)
                        else {}
                    ),
                )
                expected_key = record.key
            except ValueError as exc:
                raise CanaryInhibitionStoreError(
                    f"invalid canary inhibition record {key!r} at {self.path}: {exc}"
                ) from exc
            if key != expected_key:
                raise CanaryInhibitionStoreError(
                    f"canary inhibition key mismatch {key!r} at {self.path}"
                )
            records[key] = record
        return records

    def matching(
        self,
        deployment_id: str,
        canary_id: str | None = None,
    ) -> list[CanaryInhibitionRecord]:
        deployment = _required_text(deployment_id, "deployment_id")
        canary = (
            _required_text(canary_id, "canary_id")
            if canary_id is not None
            else None
        )
        return sorted(
            (
                record
                for record in self.load().values()
                if record.deployment_id == deployment
                and (not canary or record.canary_id == canary)
            ),
            key=lambda record: (record.latched_at, record.canary_id),
        )

    def is_latched(
        self,
        deployment_id: str,
        canary_id: str | None = None,
    ) -> bool:
        return bool(self.matching(deployment_id, canary_id))

    def record_inhibition(
        self,
        *,
        deployment_id: str,
        canary_id: str,
        reason: str,
        evidence: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> CanaryInhibitionRecord:
        deployment = _required_text(deployment_id, "deployment_id")
        canary = _required_text(canary_id, "canary_id")
        normalized_reason = _required_text(reason, "reason")
        key = _record_key(deployment, canary)
        effective_now = now or datetime.now(UTC)
        if effective_now.tzinfo is None:
            effective_now = effective_now.replace(tzinfo=UTC)
        record = CanaryInhibitionRecord(
            deployment_id=deployment,
            canary_id=canary,
            latched_at=effective_now.astimezone(UTC).isoformat(),
            reason=normalized_reason,
            evidence=dict(evidence or {}),
        )

        self.initialize()
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                records = self.load()
                existing = records.get(key)
                if existing is not None:
                    return existing
                records[key] = record
                self._atomic_write(records)
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        return record

    def _atomic_write(
        self,
        records: dict[str, CanaryInhibitionRecord],
    ) -> None:
        payload = {
            "schema_version": 1,
            "inhibitions": {
                key: record.to_dict() for key, record in sorted(records.items())
            },
        }
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def _atomic_write_marker(self) -> None:
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.initialized_marker_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write("schema_version=1\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.initialized_marker_path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()


def _record_key(deployment_id: str, canary_id: str) -> str:
    if "::" in deployment_id or "::" in canary_id:
        raise ValueError("deployment_id and canary_id may not contain '::'")
    return f"{deployment_id}::{canary_id}"


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value.strip()
