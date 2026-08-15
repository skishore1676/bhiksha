from __future__ import annotations

from pathlib import Path

import pytest

from bhiksha.active_plan.compiler import ActivePlanSheetRow, compile_active_plan_from_rows
from bhiksha.cartographer_profiles import canonical_hash
from bhiksha.integrations.cartographer_projector import MANUAL_ENTRY_HEADERS, project_signals, project_with_table, row_to_compiler_payload


def _signal() -> dict[str, object]:
    body: dict[str, object] = {
        "schema": "market_cartographer.signal.v1",
        "cartographer_version": "1.0",
        "run_id": "run-1",
        "trading_date": "2026-08-17",
        "symbol": "SPY",
        "direction": "long",
        "trigger_price": 600.0,
        "trigger_direction": "ABOVE",
        "invalidation_price": 590.0,
        "rationale": [{"text": "daily trend=rising", "evidence_refs": ["SPY:daily:trend"]}],
        "authorization_mode": "shadow",
        "management_policy": "TREND_CONTINUATION",
        "valid_after": "2026-08-17T09:35:00-05:00",
        "valid_through": "2026-08-17T15:00:00-05:00",
        "evidence": {"source_batch_hash": "sha256:batch", "candidate_id": "c1", "candidate_hash": "sha256:c", "evidence_refs": ["SPY:daily:trend"]},
        "effects": {"broker": False, "orders": False, "auth": False, "sheet": False, "active_plan": False, "external_send": False},
    }
    signal_hash = canonical_hash(body)
    return {**body, "signal_id": f"mc-v1-{signal_hash.split(':', 1)[1][:24]}", "signal_hash": signal_hash}


def _batch() -> dict[str, object]:
    body: dict[str, object] = {
        "schema": "market_cartographer.signal_batch.v1", "status": "succeeded", "run_id": "run-1",
        "as_of": "2026-08-14T20:00:00Z", "source_batch_hash": "sha256:batch", "signals": [_signal()],
        "effects": {"broker": False, "orders": False, "auth": False, "sheet": False, "active_plan": False, "external_send": False},
    }
    return {**body, "signal_batch_hash": canonical_hash(body)}


def test_projector_maps_av_and_round_trips_through_compiler(tmp_path: Path) -> None:
    rows, receipt = project_signals([], _batch(), operator_premium_ceiling=400.0, trading_date="2026-08-17")
    assert receipt["actions"][0]["action"] == "created"
    assert len(rows[0]) == 22
    row = ActivePlanSheetRow.model_validate(row_to_compiler_payload(rows[0]))
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    compiled = compile_active_plan_from_rows(
        rows=[row], strategy_catalog_path=catalog, trading_date="2026-08-17",
        operator_defaults={"max_trade_premium_usd": 400.0},
    )
    assert compiled.plan.suppressed == []
    assert compiled.plan.deployments[0].risk.max_trade_premium_usd == 400.0


def test_projector_retry_preserves_consumed_state_and_writebacks() -> None:
    rows, _ = project_signals([], _batch(), operator_premium_ceiling=500.0, trading_date="2026-08-17")
    rows[0][1] = False
    rows[0][2] = "live"
    rows[0][12:16] = ["triggered", "at", "note", "trade-1"]
    retried, receipt = project_signals(rows, _batch(), operator_premium_ceiling=500.0, trading_date="2026-08-17")
    assert receipt["actions"][0]["action"] == "preserved"
    assert retried[0][1:3] == [False, "live"]
    assert retried[0][12:16] == ["triggered", "at", "note", "trade-1"]


def test_projector_rejects_ownership_collision_and_expired_signal() -> None:
    rows = [[_signal()["signal_id"], True, "shadow"]]
    with pytest.raises(ValueError, match="non-Cartographer"):
        project_signals(rows, _batch(), operator_premium_ceiling=500.0, trading_date="2026-08-17")
    rows, receipt = project_signals([], _batch(), operator_premium_ceiling=500.0, trading_date="2026-08-18")
    assert rows == []
    assert receipt["actions"] == [{"signal_id": _signal()["signal_id"], "action": "expired"}]


class _Table:
    def __init__(self, headers, rows):
        self.headers, self.rows = headers, rows
        self.writes = []
    def read_headers(self): return self.headers
    def read_rows(self): return [dict(row) for row in self.rows]
    def update_exact_rows(self, *, headers, rows):
        self.writes.append(rows)
        for index, values in rows:
            found = next((row for row in self.rows if row["row_index"] == index), None)
            record = dict(zip(headers, values, strict=True)); record["row_index"] = index
            if found is None: self.rows.append(record)
            else: found.update(record)


def test_table_projector_validates_headers_dry_runs_and_readbacks() -> None:
    table = _Table(MANUAL_ENTRY_HEADERS, [])
    dry = project_with_table(table, _batch(), operator_premium_ceiling=400, trading_date="2026-08-17")
    assert dry["planned_updates"] == 1 and table.writes == []
    applied = project_with_table(table, _batch(), operator_premium_ceiling=400, trading_date="2026-08-17", apply=True)
    assert applied["status"] == "applied" and len(table.writes) == 1
    retry = project_with_table(table, _batch(), operator_premium_ceiling=400, trading_date="2026-08-17", apply=True)
    assert retry["planned_updates"] == 0


def test_table_projector_fails_header_duplicate_and_readback_errors() -> None:
    with pytest.raises(ValueError, match="headers"):
        project_with_table(_Table(["id"], []), _batch(), operator_premium_ceiling=400, trading_date="2026-08-17")
    duplicate = _Table(MANUAL_ENTRY_HEADERS, [{"id": "x", "row_index": 2}, {"id": "x", "row_index": 3}])
    with pytest.raises(ValueError, match="duplicate"):
        project_with_table(duplicate, _batch(), operator_premium_ceiling=400, trading_date="2026-08-17")

    class _ReadbackMismatch(_Table):
        def update_exact_rows(self, *, headers, rows):
            self.writes.append(rows)

    with pytest.raises(RuntimeError, match="readback mismatch"):
        project_with_table(
            _ReadbackMismatch(MANUAL_ENTRY_HEADERS, []), _batch(),
            operator_premium_ceiling=400, trading_date="2026-08-17", apply=True,
        )

    class _ReadFailure(_Table):
        def read_headers(self): raise OSError("read unavailable")

    with pytest.raises(OSError, match="read unavailable"):
        project_with_table(_ReadFailure(MANUAL_ENTRY_HEADERS, []), _batch(), operator_premium_ceiling=400, trading_date="2026-08-17")
