from __future__ import annotations

from pathlib import Path

import yaml

from bhiksha.tools.legacy_retirement import build_report, scan_legacy_wires


def test_legacy_retirement_reports_active_mala_promoted_wires(tmp_path: Path) -> None:
    deployments = tmp_path / "deployments"
    catalog = tmp_path / "strategy_catalog"
    deployments.mkdir()
    catalog.mkdir()
    (deployments / "legacy.yaml").write_text(
        yaml.safe_dump(
            {
                "deployment_id": "market_impulse_qqq_short_v1",
                "enabled": True,
                "symbol": "QQQ",
                "strategy": {"key": "market_impulse"},
                "source": {
                    "origin": "mala",
                    "artifact": "m5_execution_mapping.csv",
                    "metadata": {"promoted_from": "market_impulse_qqq_short_v1"},
                },
                "tags": ["mala_promoted"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (catalog / "retired.yaml").write_text(
        yaml.safe_dump(
            {
                "strategy_id": "old_strategy",
                "enabled": False,
                "approval_status": "retired",
                "symbol": "SPY",
                "strategy": {"key": "market_impulse"},
                "source": {"origin": "mala"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report = build_report(
        scan_legacy_wires(
            deployments_dir=deployments,
            strategy_catalog_dir=catalog,
        )
    )

    assert report["status"] == "blocked"
    assert report["legacy_wire_count"] == 2
    assert report["active_legacy_wire_count"] == 1
    assert report["active_legacy_ids"] == ["market_impulse_qqq_short_v1"]
    assert report["next_action"] == "retire_or_repromote_active_legacy_wires"


def test_legacy_retirement_clears_when_only_retired_wires_remain(tmp_path: Path) -> None:
    deployments = tmp_path / "deployments"
    catalog = tmp_path / "strategy_catalog"
    deployments.mkdir()
    catalog.mkdir()
    (catalog / "retired.yaml").write_text(
        yaml.safe_dump(
            {
                "strategy_id": "old_strategy",
                "enabled": False,
                "approval_status": "retired",
                "symbol": "SPY",
                "strategy": {"key": "market_impulse"},
                "source": {"origin": "mala"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report = build_report(
        scan_legacy_wires(
            deployments_dir=deployments,
            strategy_catalog_dir=catalog,
        )
    )

    assert report["status"] == "clear"
    assert report["legacy_wire_count"] == 1
    assert report["active_legacy_wire_count"] == 0
    assert report["next_action"] == "continue_new_packet_path"
