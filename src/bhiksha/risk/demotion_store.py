"""Local JSON store for Rail B live-to-shadow demotion overrides.

Follows the repo's ``config/`` file conventions (see
``config/risk/conservative.yaml``): a small local file the runtime reads and
(for this one) writes, versioned in the operator's config tree rather than
in the database, so it survives independently of the sqlite event log and is
trivially operator-editable by hand if needed.

One-way by design: this module only ever ADDS or leaves entries; nothing in
this repo calls remove/clear from the automated path. Re-promotion is a
deliberate operator action -- delete the deployment's entry from the JSON
file (or the whole file) -- never an automated flip. See
``RiskManager._evaluate_rail_b`` in ``risk_manager.py`` for the one caller
that appends entries, and ``active_plan.compiler`` for the one reader that
consumes them at compile time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import tempfile
from typing import Any

DEFAULT_DEMOTION_OVERRIDE_PATH = "config/risk/demoted_deployments.json"


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


class DemotionStore:
    """Reads/writes ``config/risk/demoted_deployments.json``.

    Shape on disk::

        {
          "schema_version": 1,
          "demotions": {
            "<deployment_id>": { ...DemotionRecord.to_dict()... }
          }
        }

    Operator re-promotion: delete the deployment's key under ``demotions``
    (or delete the file). Nothing in the automated runtime does this.
    """

    def __init__(self, path: str | Path = DEFAULT_DEMOTION_OVERRIDE_PATH) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, DemotionRecord]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        demotions = raw.get("demotions")
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
        self._write(existing)
        return record

    def _write(self, records: dict[str, DemotionRecord]) -> None:
        payload = {
            "schema_version": 1,
            "demotions": {deployment_id: record.to_dict() for deployment_id, record in records.items()},
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
