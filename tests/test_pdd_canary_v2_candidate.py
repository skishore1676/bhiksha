from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bhiksha.config.models import DeploymentManifest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "release"
    / "build_pdd_canary_v2_candidate.py"
)
SPEC = importlib.util.spec_from_file_location(
    "build_pdd_canary_v2_candidate",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
candidate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(candidate)


def _pdd_v1_row() -> dict:
    return {
        "row_index": 29,
        "enabled": True,
        "authorization_mode": "live",
        "strategy_id": candidate.PDD_STRATEGY_ID,
        "entry_window_start_et": "9:35",
        "max_trade_premium_usd": 300,
        "execution_overrides": "",
        "notes": "bounded v1",
        "exit": json.dumps(
            {
                "profile_exit_drives_live": True,
                "profile_exit_shadow_only": False,
            }
        ),
        "execution": json.dumps(
            {
                "dte_fallback_policy": "allow_nearest_after",
                "dte_max": 3,
                "dte_min": 0,
                "runtime_mode": "live_approval_gated",
                "shadow_only": False,
            }
        ),
        "max_contracts": 1,
        "source_metadata": json.dumps(
            {
                "authorization_sha256": candidate.PDD_V1_AUTHORIZATION_SHA256,
                "authorized_active_plan_id": candidate.ACTIVE_PLAN_V1_ID,
                "canary_policy": {
                    "max_cumulative_loss_r": -2.0,
                    "provider_overlap_floor": 0.9,
                    "r_definition": (
                        "sum_after_cost_trade_pnl_over_frozen_entry_stop_risk"
                    ),
                    "round_trip_cost_per_contract_usd": 2.0,
                    "scale_fraction_of_baseline": 0.2,
                    "scale_min_clean_closes": 10,
                    "stop_on_failed_exit_receipt": True,
                    "stop_on_missing_attribution": True,
                    "stop_on_unprotected_position": True,
                },
            }
        ),
    }


def _snapshot() -> dict[str, list[dict]]:
    return {
        "active_strategy": [_pdd_v1_row()],
        "manual_entry": [{"row_index": 2, "enabled": False}],
        "Mala_Evidence_v1": [{"row_index": 38, "catalog_key": "pdd"}],
        "Operator_Defaults_v1": [{"row_index": 2, "key": "risk"}],
    }


def test_sheet_snapshot_double_read_is_coherent_and_immutable() -> None:
    source = _snapshot()

    def read_rows(name: str) -> list[dict]:
        return copy.deepcopy(source[name])

    observed, hashes = candidate._read_coherent_sheet_snapshot(read_rows)
    source["active_strategy"][0]["max_trade_premium_usd"] = 999

    assert observed["active_strategy"][0]["max_trade_premium_usd"] == 300
    assert hashes["active_strategy"] == candidate.canonical_sha(
        observed["active_strategy"]
    )
    assert set(hashes) == set(candidate.SHEET_NAMES)


def test_sheet_snapshot_rejects_drift_between_complete_reads() -> None:
    source = _snapshot()
    calls = {name: 0 for name in candidate.SHEET_NAMES}

    def read_rows(name: str) -> list[dict]:
        calls[name] += 1
        result = copy.deepcopy(source[name])
        if name == "active_strategy" and calls[name] == 2:
            result[0]["notes"] = "concurrent change"
        return result

    with pytest.raises(RuntimeError, match="active_strategy"):
        candidate._read_coherent_sheet_snapshot(read_rows)


def test_pdd_projection_changes_only_four_authorized_cells() -> None:
    source = _snapshot()["active_strategy"]
    source_before = copy.deepcopy(source)

    observed, projected, pdd_row = candidate._project_pdd_v2(source)

    changed = {
        key
        for key in set(observed) | set(pdd_row)
        if observed.get(key) != pdd_row.get(key)
    }
    assert changed == candidate.PDD_TARGET_FIELDS
    assert source == source_before
    assert observed == source_before[0]
    assert projected is not source
    assert pdd_row["max_trade_premium_usd"] == 1_000.0
    assert pdd_row["max_contracts"] == 2
    assert pdd_row["enabled"] is True
    assert pdd_row["authorization_mode"] == "live"


def test_pdd_projection_rejects_header_letter_drift() -> None:
    active = _snapshot()["active_strategy"]
    row = active[0]
    row["notes"], row["exit"] = row.pop("exit"), row.pop("notes")

    with pytest.raises(RuntimeError, match="header layout drifted"):
        candidate._project_pdd_v2(active)


def test_active_header_contract_binds_physical_cell_letters() -> None:
    layout = SimpleNamespace(
        header_row_number=1,
        header_start_index=0,
        headers=list(candidate.EXPECTED_ACTIVE_STRATEGY_COLUMNS),
    )

    class Client:
        def _read_layout(self):
            return copy.deepcopy(layout)

    contract = candidate._active_header_contract(Client())

    assert contract["required_cell_mapping"] == {
        "E": "max_trade_premium_usd",
        "G": "notes",
        "J": "max_contracts",
        "K": "source_metadata",
    }
    payload = dict(contract)
    claimed_sha256 = payload.pop("sha256")
    assert candidate.canonical_sha(payload) == claimed_sha256


def test_active_header_contract_rejects_physical_reorder() -> None:
    headers = list(candidate.EXPECTED_ACTIVE_STRATEGY_COLUMNS)
    headers[4], headers[5] = headers[5], headers[4]
    layout = SimpleNamespace(
        header_row_number=1,
        header_start_index=0,
        headers=headers,
    )

    class Client:
        def _read_layout(self):
            return copy.deepcopy(layout)

    with pytest.raises(RuntimeError, match="physical header mapping drifted"):
        candidate._active_header_contract(Client())


def test_current_v1_plan_requires_exact_bytes_id_and_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "active_plan.json"
    payload = {
        "active_plan_id": candidate.ACTIVE_PLAN_V1_ID,
        "plan_revision_id": candidate.PDD_V1_PLAN_REVISION_ID,
    }
    plan_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
    path.write_bytes(plan_bytes)
    monkeypatch.setattr(
        candidate,
        "PDD_V1_ACTIVE_PLAN_SHA256",
        hashlib.sha256(plan_bytes).hexdigest(),
    )

    evidence = candidate._assert_current_v1_plan(path)

    assert evidence["active_plan_id"] == candidate.ACTIVE_PLAN_V1_ID
    path.write_bytes(plan_bytes + b"\n")
    with pytest.raises(RuntimeError, match="active plan SHA"):
        candidate._assert_current_v1_plan(path)


@pytest.mark.parametrize(
    "drift_surface",
    ["exit", "execution", "catalog", "operator_defaults"],
)
def test_v1_snapshot_replay_rejects_any_compiled_revision_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift_surface: str,
) -> None:
    receipt = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "docs"
            / "release_candidates"
            / "mala_research_release_20260802"
            / "pdd_canary_candidate.json"
        ).read_text(encoding="utf-8")
    )
    deployment = DeploymentManifest.model_validate(
        receipt["authorization_payload"]["deployment"]
    )
    fake_plan = SimpleNamespace(
        deployments=[deployment],
        model_dump=lambda mode: {
            "active_plan_id": candidate.ACTIVE_PLAN_V1_ID,
            "plan_revision_id": "sha256:" + "f" * 64,
        },
    )
    monkeypatch.setattr(
        candidate,
        "compile_active_plan_from_rows",
        lambda **kwargs: SimpleNamespace(plan=fake_plan),
    )
    snapshot = _snapshot()
    snapshot["manual_entry"] = []

    with pytest.raises(RuntimeError, match="does not replay retained v1 revision"):
        candidate._assert_v1_snapshot_replay(
            snapshot=snapshot,
            catalog_root=tmp_path,
            catalog_rows=[{"drift_surface": drift_surface}],
            operator_defaults={"drift_surface": drift_surface},
        )


def test_pdd_projection_rejects_unexpected_preimage() -> None:
    active = _snapshot()["active_strategy"]
    active[0]["enabled"] = False

    with pytest.raises(RuntimeError, match="not enabled"):
        candidate._project_pdd_v2(active)


def test_cutover_contract_binds_both_idle_jobs_to_v1_then_v2() -> None:
    header = {"sha256": "b" * 64}
    replay = {"plan_revision_id": candidate.PDD_V1_PLAN_REVISION_ID}
    current = {"active_plan_sha256": candidate.PDD_V1_ACTIVE_PLAN_SHA256}
    contract = candidate._cutover_contract(
        observed_row_sha256="a" * 64,
        active_header_contract=header,
        v1_replay=replay,
        current_v1_plan=current,
    )

    assert contract["preconditions"]["active_plan_id"] == (
        candidate.ACTIVE_PLAN_V1_ID
    )
    assert set(contract["preconditions"]["launchd_active_plan_ids"]) == {
        "com.bhiksha.live-start",
        "com.bhiksha.live-watchdog",
    }
    assert set(contract["preconditions"]["launchd_states"].values()) == {
        "not running"
    }
    assert contract["preconditions"]["active_strategy_header_contract"] == header
    assert contract["preconditions"]["v1_snapshot_replay"] == replay
    assert contract["preconditions"]["current_v1_plan"] == current
    assert set(contract["postconditions"]["launchd_active_plan_ids"].values()) == {
        candidate.ACTIVE_PLAN_ID
    }
    assert contract["postconditions"]["runtime_start_authorized"] is False
