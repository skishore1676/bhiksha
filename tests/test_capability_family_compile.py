"""Capability-gating tests for the market-impulse descendant and
compression/vpoc catalog families.

The fixture ``tests/fixtures/mala_evidence/capability_family_rows.json`` holds
the nine real Mala_Evidence_v1 rows that were blocked with
``runtime_adapter_not_implemented`` before these detector families landed
(snapshot: oldmac sheet_backups/phase2_shadow_expand_20260702T051914Z).

Layered proof:
1. Capability gating passes for all nine rows (variant + status recomputed
   from the manifest at compile time).
2. Nothing arms from this change alone: the stale runtime-readiness cells on
   the sheet keep ``bhiksha_ready`` False until the Mala steward refreshes.
3. After a steward refresh, shadow-tier rows promote as shadow-only generated
   configs; watch-only rows stay held; KILL rows stay suppressed; live
   authorization stays suppressed on the evidence gates.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from bhiksha.active_plan.compiler import (
    StrategyCatalogSheetRow,
    compile_active_plan_from_google_sheets,
    sync_google_strategy_catalog,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "mala_evidence" / "capability_family_rows.json"

EXPECTED_VARIANTS = {
    "compression-breakout-current-basket-discovery__amd_short": ("compression_expansion_breakout", "default"),
    "compression-breakout-current-basket-discovery__tsla_short": ("compression_expansion_breakout", "default"),
    "mi-desc-high-close-semiconductors-m1__amd_short": ("market_impulse", "close_location_reclaim"),
    "mi-desc-push-through-semiconductors-m1__amd_short": ("market_impulse", "continuation_confirmation"),
    "mi-desc-push-through-semiconductors-m1__mu_long": ("market_impulse", "continuation_confirmation"),
    "mi-desc-push-through-semiconductors-m1__smh_short": ("market_impulse", "continuation_confirmation"),
    "mi-desc-shallow-spring-semiconductors-m1__amd_short": ("market_impulse", "same_bar_shallow_reclaim"),
    "vpoc-migration-discovery-01__amd_short": ("compression_expansion_breakout", "default"),
    "vpoc-migration-discovery-01__tsla_short": ("compression_expansion_breakout", "default"),
}

SHADOW_TIER_KEYS = {
    "compression-breakout-current-basket-discovery__tsla_short",
    "mi-desc-push-through-semiconductors-m1__smh_short",
    "mi-desc-shallow-spring-semiconductors-m1__amd_short",
    "vpoc-migration-discovery-01__tsla_short",
}


def _fixture_rows() -> list[dict]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _steward_refreshed(row: dict) -> dict:
    """Simulate the Mala steward re-publishing the row after the capability bump."""
    return {
        **row,
        "bhiksha_runtime_supported": "TRUE",
        "bhiksha_runtime_reason": "runtime_verified",
        "bhiksha_ready": "TRUE",
        "bhiksha_capability_status": "supported",
        "bhiksha_capability_reason": "runtime_verified",
    }


def test_all_nine_blocked_rows_now_pass_capability_gating() -> None:
    rows = _fixture_rows()
    assert len(rows) == 9

    for raw in rows:
        validated = StrategyCatalogSheetRow.model_validate(raw)
        expected_key, expected_variant = EXPECTED_VARIANTS[validated.catalog_key]
        assert validated.strategy_key == expected_key, validated.catalog_key
        assert validated.strategy_variant == expected_variant, validated.catalog_key
        # Capability status is recomputed from the manifest at compile time;
        # the stale sheet cell ("unsupported") must not survive normalization.
        assert validated.bhiksha_capability_status == "supported", validated.catalog_key
        assert validated.bhiksha_capability_reason != "runtime_adapter_not_implemented", validated.catalog_key


def test_stale_runtime_cells_alone_do_not_arm_anything() -> None:
    # The July snapshot rows still carry bhiksha_runtime_supported=FALSE from
    # the pre-capability steward run. Until Mala republishes, bhiksha_ready
    # stays False, so this change cannot promote or arm anything by itself.
    for raw in _fixture_rows():
        validated = StrategyCatalogSheetRow.model_validate(raw)
        assert validated.bhiksha_ready is False, validated.catalog_key


def test_refreshed_shadow_tier_rows_promote_as_shadow_only_configs(tmp_path: Path) -> None:
    catalog_root = tmp_path / "strategy_catalog"
    catalog_root.mkdir()
    rows = [StrategyCatalogSheetRow.model_validate(_steward_refreshed(raw)) for raw in _fixture_rows()]

    written = sync_google_strategy_catalog(
        strategy_catalog_path=catalog_root,
        google_strategy_catalog=rows,
    )

    written_keys = {path.stem for path in written}
    # Shadow-tier rows promote; watch-only rows stay held by the evidence tier
    # (lifecycle 'hold'), which is an evidence gate, not a capability gate.
    assert written_keys == SHADOW_TIER_KEYS
    for path in written:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert payload["execution"]["shadow_only"] is True, path.stem
        expected_key, _ = EXPECTED_VARIANTS[payload["strategy_id"]]
        assert payload["strategy"]["key"] == expected_key


@pytest.fixture()
def _smh_push_through_compile(tmp_path: Path):
    def compile_with(authorization_mode: str, catalog_key: str = "mi-desc-push-through-semiconductors-m1__smh_short"):
        catalog_root = tmp_path / "strategy_catalog"
        catalog_root.mkdir(exist_ok=True)
        raw = next(row for row in _fixture_rows() if row["catalog_key"] == catalog_key)
        catalog_client = _FakeSheetClient(sheet_name="Mala_Evidence_v1", rows=[_steward_refreshed(raw)])
        strategy_client = _FakeSheetClient(
            sheet_name="active_strategy",
            rows=[
                {
                    "enabled": "TRUE",
                    "authorization_mode": authorization_mode,
                    "strategy_id": catalog_key,
                }
            ],
        )
        manual_client = _FakeSheetClient(sheet_name="manual_entry", rows=[])
        return compile_active_plan_from_google_sheets(
            spreadsheet_id="spreadsheet123",
            credentials_path=tmp_path / "credentials.json",
            catalog_sheet_name="Mala_Evidence_v1",
            strategy_sheet_name="active_strategy",
            manual_sheet_name="manual_entry",
            strategy_catalog_path=catalog_root,
            catalog_client=catalog_client,
            strategy_client=strategy_client,
            manual_client=manual_client,
        )

    return compile_with


def test_smh_push_through_row_compiles_as_shadow_lane(_smh_push_through_compile) -> None:
    compiled = _smh_push_through_compile("shadow")

    assert compiled.plan.summary["suppressed_count"] == 0
    assert len(compiled.plan.deployments) == 1
    deployment = compiled.plan.deployments[0]
    assert deployment.execution.shadow_only is True
    assert deployment.strategy.key == "market_impulse"
    assert deployment.strategy.params["entry_mode"] == "continuation_confirmation"
    assert deployment.strategy.params["confirmation_type"] == "break_reclaim_high_low"
    assert deployment.symbol == "SMH"


def test_smh_push_through_live_authorization_stays_suppressed(_smh_push_through_compile) -> None:
    # Capability support must not weaken the live evidence bar: the row is
    # sub-activation (activation_candidate FALSE), so live stays suppressed.
    compiled = _smh_push_through_compile("live")

    assert compiled.plan.deployments == []
    assert compiled.plan.summary["suppressed_count"] == 1
    assert "activation_candidate" in compiled.plan.suppressed[0]["reason"]


def test_kill_verdict_rows_stay_suppressed_despite_capability_support(_smh_push_through_compile) -> None:
    compiled = _smh_push_through_compile(
        "shadow", catalog_key="compression-breakout-current-basket-discovery__tsla_short"
    )

    assert compiled.plan.deployments == []
    assert compiled.plan.summary["suppressed_count"] == 1
    assert "triage_verdict=KILL" in compiled.plan.suppressed[0]["reason"]


class _FakeSheetClient:
    def __init__(self, *, sheet_name: str, rows: list[dict], spreadsheet_id: str = "spreadsheet123") -> None:
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name
        self._rows = rows

    def read_rows(self, *, range_suffix: str = "A1:ZZ2000") -> list[dict]:
        del range_suffix
        return [
            {
                **row,
                "row_index": index,
            }
            for index, row in enumerate(self._rows, start=2)
        ]
