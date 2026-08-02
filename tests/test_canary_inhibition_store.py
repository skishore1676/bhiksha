from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path

import pytest

from bhiksha.risk.canary_inhibition_store import (
    CanaryInhibitionStore,
    CanaryInhibitionStoreError,
)


NOW = datetime(2026, 8, 2, 18, 0, tzinfo=UTC)


def test_inhibition_is_atomic_idempotent_and_restart_durable(tmp_path) -> None:
    path = tmp_path / "canary_inhibitions.json"
    store = CanaryInhibitionStore(path)

    first = store.record_inhibition(
        deployment_id="pdd_live_canary",
        canary_id="pdd-v1",
        reason="provider_overlap_below_floor",
        evidence={"provider_overlap": 0.88, "floor": 0.90},
        now=NOW,
    )
    retry = store.record_inhibition(
        deployment_id="pdd_live_canary",
        canary_id="pdd-v1",
        reason="different_retry_reason",
        now=NOW,
    )

    assert retry == first
    assert CanaryInhibitionStore(path).is_latched(
        "pdd_live_canary", "pdd-v1"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert list(payload["inhibitions"]) == ["pdd_live_canary::pdd-v1"]
    assert payload["inhibitions"]["pdd_live_canary::pdd-v1"]["reason"] == (
        "provider_overlap_below_floor"
    )
    assert not hasattr(store, "clear")
    assert not hasattr(store, "delete")


def test_atomic_write_failure_preserves_existing_latch(tmp_path, monkeypatch) -> None:
    path = tmp_path / "canary_inhibitions.json"
    store = CanaryInhibitionStore(path)
    store.record_inhibition(
        deployment_id="pdd_live_canary",
        canary_id="pdd-v1",
        reason="first_latch",
        now=NOW,
    )

    def fail_replace(self: Path, target: Path) -> Path:
        raise OSError(f"replace blocked for {target}")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="replace blocked"):
        store.record_inhibition(
            deployment_id="meta_live_canary",
            canary_id="meta-v1",
            reason="second_latch",
            now=NOW,
        )

    records = CanaryInhibitionStore(path).load()
    assert list(records) == ["pdd_live_canary::pdd-v1"]


def test_malformed_store_raises_instead_of_failing_open(tmp_path) -> None:
    path = tmp_path / "canary_inhibitions.json"
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(CanaryInhibitionStoreError, match="cannot read"):
        CanaryInhibitionStore(path).load()


def test_initialized_store_cannot_silently_reset_when_state_disappears(
    tmp_path,
) -> None:
    path = tmp_path / "canary_inhibitions.json"
    store = CanaryInhibitionStore(path)
    store.initialize()
    assert path.exists()
    assert store.initialized_marker_path.exists()

    path.unlink()

    with pytest.raises(
        CanaryInhibitionStoreError,
        match="initialized canary inhibition state is missing",
    ):
        CanaryInhibitionStore(path).initialize()
