"""Local JSON store for Rail B demotions and operator re-promotion boundaries.

Follows the repo's ``config/`` file conventions (see
``config/risk/conservative.yaml``): a small local file the runtime reads and
(for this one) writes, versioned in the operator's config tree rather than
in the database, so it survives independently of the sqlite event log and is
trivially operator-editable by hand if needed.

Automated code only adds demotions. Re-promotion is a protected operator
action which removes the active demotion and records a timestamped evidence
reset. Rail B then evaluates only trades closed after that reset, so a manual
second chance cannot be immediately reversed by the same historical window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tempfile
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
# Audit fix (2026-07-02): repo-root-anchored absolute default (same pattern as
# strategy.capabilities.DEFAULT_CAPABILITY_MANIFEST_PATH) instead of a bare
# relative path — the runtime (launchd, WorkingDirectory-pinned) and the
# ad-hoc CLI tools (arbitrary cwd) must always read/write the SAME file.
# Env override for tests/alternate topologies.
DEMOTION_OVERRIDE_PATH_ENV = "BHIKSHA_RISK_DEMOTION_STORE_PATH"
DEFAULT_DEMOTION_OVERRIDE_PATH = _REPO_ROOT / "config" / "risk" / "demoted_deployments.json"


def default_demotion_override_path() -> Path:
    configured = os.getenv(DEMOTION_OVERRIDE_PATH_ENV)
    if configured and configured.strip():
        return Path(configured).expanduser()
    return DEFAULT_DEMOTION_OVERRIDE_PATH


@dataclass(slots=True, frozen=True)
class DemotionRecord:
    deployment_id: str
    demoted_at: str
    reason: str
    window_n: int
    mean_pnl_usd: float
    threshold_usd: float
    trade_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "deployment_id": self.deployment_id,
            "demoted_at": self.demoted_at,
            "reason": self.reason,
            "window_n": self.window_n,
            "mean_pnl_usd": self.mean_pnl_usd,
            "threshold_usd": self.threshold_usd,
            "trade_ids": list(self.trade_ids),
        }


@dataclass(slots=True, frozen=True)
class RepromotionRecord:
    deployment_id: str
    repromoted_at: str
    reason: str
    approved_by: str
    prior_demotion: DemotionRecord

    def to_dict(self) -> dict[str, Any]:
        return {
            "deployment_id": self.deployment_id,
            "repromoted_at": self.repromoted_at,
            "reason": self.reason,
            "approved_by": self.approved_by,
            "prior_demotion": self.prior_demotion.to_dict(),
        }


class DemotionStore:
    """Reads/writes ``config/risk/demoted_deployments.json``.

    Shape on disk::

        {
          "schema_version": 2,
          "demotions": {
            "<deployment_id>": { ...DemotionRecord.to_dict()... }
          },
          "repromotions": {
            "<deployment_id>": [
              { ...RepromotionRecord.to_dict()... }
            ]
          }
        }

    Schema v1 files remain readable. Operator re-promotion must use
    ``repromote_many`` so the old demotion remains auditable and Rail B gets a
    fresh-evidence cutoff.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_demotion_override_path()

    def _load_payload(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def load(self) -> dict[str, DemotionRecord]:
        return self._parse_demotions(self._load_payload().get("demotions"))

    @staticmethod
    def _parse_demotions(demotions: Any) -> dict[str, DemotionRecord]:
        if not isinstance(demotions, dict):
            return {}
        records: dict[str, DemotionRecord] = {}
        for deployment_id, payload in demotions.items():
            if not isinstance(payload, dict):
                continue
            try:
                records[deployment_id] = DemotionRecord(
                    deployment_id=str(payload.get("deployment_id") or deployment_id),
                    demoted_at=str(payload.get("demoted_at") or ""),
                    reason=str(payload.get("reason") or ""),
                    window_n=int(payload.get("window_n") or 0),
                    mean_pnl_usd=float(payload.get("mean_pnl_usd") or 0.0),
                    threshold_usd=float(payload.get("threshold_usd") or 0.0),
                    trade_ids=[str(item) for item in (payload.get("trade_ids") or [])],
                )
            except (TypeError, ValueError):
                continue
        return records

    def load_repromotion_history(self) -> dict[str, list[RepromotionRecord]]:
        raw = self._load_payload().get("repromotions")
        if not isinstance(raw, dict):
            return {}
        records: dict[str, list[RepromotionRecord]] = {}
        for deployment_id, payload in raw.items():
            # Accept the first v2 draft's single-object shape as well as the
            # append-only list written by current code.
            items = payload if isinstance(payload, list) else [payload]
            parsed: list[RepromotionRecord] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                prior = self._parse_demotions(
                    {deployment_id: item.get("prior_demotion")}
                ).get(deployment_id)
                if prior is None:
                    continue
                try:
                    parsed.append(
                        RepromotionRecord(
                            deployment_id=str(item.get("deployment_id") or deployment_id),
                            repromoted_at=str(item.get("repromoted_at") or ""),
                            reason=str(item.get("reason") or ""),
                            approved_by=str(item.get("approved_by") or ""),
                            prior_demotion=prior,
                        )
                    )
                except (TypeError, ValueError):
                    continue
            if parsed:
                records[deployment_id] = parsed
        return records

    def load_repromotions(self) -> dict[str, RepromotionRecord]:
        """Return the latest reset per deployment for cutoff compatibility."""
        return {
            deployment_id: history[-1]
            for deployment_id, history in self.load_repromotion_history().items()
            if history
        }

    def repromotion_cutoff(self, deployment_id: str) -> datetime | None:
        record = self.load_repromotions().get(deployment_id)
        if record is None or not record.repromoted_at:
            return None
        try:
            cutoff = datetime.fromisoformat(record.repromoted_at)
        except ValueError:
            return None
        return cutoff.replace(tzinfo=UTC) if cutoff.tzinfo is None else cutoff.astimezone(UTC)

    def is_demoted(self, deployment_id: str) -> bool:
        return deployment_id in self.load()

    def record_demotion(
        self,
        *,
        deployment_id: str,
        reason: str,
        window_n: int,
        mean_pnl_usd: float,
        threshold_usd: float,
        trade_ids: list[str],
        now: datetime | None = None,
    ) -> DemotionRecord:
        """Append a demotion entry. No-op (returns the existing record) if already demoted -- one-way, no flapping."""
        existing = self.load()
        if deployment_id in existing:
            return existing[deployment_id]
        record = DemotionRecord(
            deployment_id=deployment_id,
            demoted_at=(now or datetime.now(UTC)).isoformat(),
            reason=reason,
            window_n=window_n,
            mean_pnl_usd=mean_pnl_usd,
            threshold_usd=threshold_usd,
            trade_ids=list(trade_ids),
        )
        existing[deployment_id] = record
        self._write(existing, self.load_repromotion_history())
        return record

    def repromote_many(
        self,
        deployment_ids: list[str],
        *,
        reason: str,
        approved_by: str,
        now: datetime | None = None,
    ) -> dict[str, RepromotionRecord]:
        """Atomically re-promote deployments and establish fresh evidence cutoffs.

        Every id must be actively demoted or already carry a re-promotion
        record. The latter makes an exact operator retry idempotent; an unknown
        id rejects the entire batch before the file changes.
        """
        requested = list(dict.fromkeys(item.strip() for item in deployment_ids if item.strip()))
        if not requested:
            raise ValueError("at least one deployment_id is required")
        if not reason.strip():
            raise ValueError("reason is required")
        if not approved_by.strip():
            raise ValueError("approved_by is required")

        demotions = self.load()
        repromotion_history = self.load_repromotion_history()
        unknown = sorted(set(requested) - set(demotions) - set(repromotion_history))
        if unknown:
            raise ValueError(f"deployment ids are not actively demoted: {', '.join(unknown)}")

        effective_now = now or datetime.now(UTC)
        if effective_now.tzinfo is None:
            effective_now = effective_now.replace(tzinfo=UTC)
        timestamp = effective_now.astimezone(UTC).isoformat()
        for deployment_id in requested:
            prior = demotions.pop(deployment_id, None)
            if prior is None:
                continue
            record = RepromotionRecord(
                deployment_id=deployment_id,
                repromoted_at=timestamp,
                reason=reason.strip(),
                approved_by=approved_by.strip(),
                prior_demotion=prior,
            )
            repromotion_history.setdefault(deployment_id, []).append(record)
        self._write(demotions, repromotion_history)
        return {
            deployment_id: repromotion_history[deployment_id][-1]
            for deployment_id in requested
        }

    def _write(
        self,
        records: dict[str, DemotionRecord],
        repromotion_history: dict[str, list[RepromotionRecord]],
    ) -> None:
        payload = {
            "schema_version": 2,
            "demotions": {deployment_id: record.to_dict() for deployment_id, record in records.items()},
            "repromotions": {
                deployment_id: [record.to_dict() for record in history]
                for deployment_id, history in repromotion_history.items()
            },
        }
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(text)
                handle.flush()
            temp_path.replace(self.path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()
