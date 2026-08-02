"""Broker-inert Mala consultation service with no trading runtime imports."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
import subprocess
import threading
import time
from typing import Any
from zoneinfo import ZoneInfo

from bhiksha.packets.consultation_bridge import consult_mala_playbook
from bhiksha.packets.runtime_compile import (
    compile_packet_for_runtime,
    load_legacy_retirement_report,
)
from bhiksha.shared_kernel import ensure_kernel_on_path

ensure_kernel_on_path()
from mala_bhiksha_kernel import CapabilityManifest  # noqa: E402


CENTRAL = ZoneInfo("America/Chicago")
CONSULTATION_COMMAND_TIMEOUT_SECONDS = 240


class ConsultationBusyError(RuntimeError):
    """A broker-inert consultation is already using the research surface."""


class ConsultationUnavailableError(RuntimeError):
    """The broker-inert research query failed or exceeded its timebox."""


class _DeadlineCommandRunner:
    """Share one monotonic request budget across all bridge subprocesses."""

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        self.expires_at = time.monotonic() + timeout_seconds

    def __call__(
        self,
        cmd: list[str],
        cwd: Path,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        remaining = self.expires_at - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(cmd, self.timeout_seconds)
        return subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            check=True,
            timeout=remaining,
        )


@dataclass(frozen=True, slots=True)
class ConsultServiceConfig:
    packet: Path
    mala_repo: Path
    capability_manifest: Path
    legacy_retirement_report: Path
    artifact_root: Path


class BrokerInertConsultationService:
    """Expose packet preflight and consultation only.

    This module deliberately has no dependency on Bhiksha bootstrap, brokers,
    order managers, trade persistence, lifecycle stores, or submission code.
    """

    def __init__(self, config: ConsultServiceConfig) -> None:
        self.config = config
        self._consult_lock = threading.Lock()
        self._assert_packet_boundary()

    def preflight(self) -> dict[str, Any]:
        manifest = CapabilityManifest.model_validate_json(
            self.config.capability_manifest.read_text(encoding="utf-8")
        )
        result = compile_packet_for_runtime(
            self.config.packet,
            capability_manifest=manifest,
            legacy_retirement_report=load_legacy_retirement_report(
                self.config.legacy_retirement_report
            ),
        )
        return {
            "eligibility": result.eligibility,
            "executable": False,
            "packet_id": result.packet_id,
            "version": result.version,
            "runtime_mode": result.runtime_mode,
            "feature_contract_id": result.feature_contract_id,
            "feature_contract_fingerprint": result.feature_contract_fingerprint,
            "management_policy_ids": result.management_policy_ids or [],
            "block_reasons": result.block_reasons,
            "safety_boundary": "broker_inert_consultation_v1",
        }

    def latest(self) -> dict[str, str]:
        candidates = sorted(
            (self.config.artifact_root / "consultations").glob(
                "*/consultation_bridge.json"
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            return {"path": "", "status": ""}
        path = candidates[0]
        return {"path": str(path), "status": "available"}

    def status(self) -> dict[str, Any]:
        return {
            "desk": "bhiksha_trader_desk_consultation",
            "safety_boundary": "broker_inert_consultation_v1",
            "symbols": ["IWM", "QQQ"],
            "routes": ["status", "preflight", "latest", "consult"],
            "order_actions": [],
            "preflight": self.preflight(),
            "latest": self.latest(),
        }

    def consult(self, payload: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(
            set(payload) - {"symbol", "direction", "chart_read", "timestamp"}
        )
        if unknown:
            raise ValueError(f"unknown fields: {', '.join(unknown)}")
        symbol = _required_text(payload, "symbol").upper()
        direction = _required_text(payload, "direction").lower()
        chart_read = _required_text(payload, "chart_read")
        if symbol not in {"IWM", "QQQ"}:
            raise ValueError("symbol must be IWM or QQQ")
        if direction not in {"long", "short"}:
            raise ValueError("direction must be long or short")
        if len(chart_read) > 4000:
            raise ValueError("chart_read exceeds 4000 characters")
        timestamp = str(payload.get("timestamp") or "").strip()
        if timestamp:
            if len(timestamp) > 64:
                raise ValueError("timestamp is too long")
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        else:
            timestamp = datetime.now(CENTRAL).isoformat()
        lock = getattr(self, "_consult_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._consult_lock = lock
        if not lock.acquire(blocking=False):
            raise ConsultationBusyError("consultation already in progress")
        try:
            runner = _DeadlineCommandRunner(
                CONSULTATION_COMMAND_TIMEOUT_SECONDS
            )
            result = consult_mala_playbook(
                packet_path=self.config.packet,
                symbol=symbol,
                direction=direction,
                timestamp=timestamp,
                chart_read=chart_read,
                mala_repo=self.config.mala_repo,
                capability_manifest_path=self.config.capability_manifest,
                legacy_retirement_report_path=self.config.legacy_retirement_report,
                out_root=self.config.artifact_root / "consultations",
                update_mala_log=False,
                runner=runner,
            )
            return asdict(result)
        except (
            OSError,
            TypeError,
            ValueError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as exc:
            raise ConsultationUnavailableError(
                "consultation research query unavailable"
            ) from exc
        finally:
            lock.release()

    def _assert_packet_boundary(self) -> None:
        payload = self.preflight()
        if (
            payload.get("version") != 1
            or payload.get("runtime_mode") != "shadow"
            or payload.get("eligibility") != "eligible"
        ):
            raise ValueError(
                "consultation service requires the eligible v1 shadow packet"
            )
def _required_text(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value
