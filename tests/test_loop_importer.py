from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path

import yaml

from bhiksha.loop.importer import refresh_generated_deployments


def test_refresh_generated_deployments_writes_shadow_manifests_and_manual_board(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    deployments_root = config_root / "deployments"
    deployments_root.mkdir(parents=True)
    _write_manifest(deployments_root / "manual_live.yaml", _base_manifest("manual_live_spy"))
    (config_root / "bias_inputs.yaml").write_text(
        yaml.safe_dump(
            {
                "selections": [
                    {
                        "symbol": "QQQ",
                        "bias_template": "bearish_trend_intraday",
                        "horizon": "intraday",
                        "enabled": True,
                        "max_active_candidates": 1,
                    },
                    {
                        "symbol": "NVDA",
                        "bias_template": "bullish_mean_reversion_intraday",
                        "horizon": "intraday",
                        "enabled": True,
                        "max_active_candidates": 1,
                    },
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    now = datetime.now(UTC).isoformat()
    candidates_path = tmp_path / "deployment_candidates.json"
    playbook_path = tmp_path / "playbook_catalog.json"
    market_candidate = _candidate_payload(
        candidate_id="market_candidate",
        deployment_id="market_impulse_qqq_short_shadow_1234abcd",
        symbol="QQQ",
        direction="short",
        strategy_key="market_impulse",
        surface_class="supported",
        automation_status="shadow_ready",
        bias_template="bearish_trend_intraday",
    )
    elastic_candidate = _candidate_payload(
        candidate_id="elastic_candidate",
        deployment_id="elastic_band_nvda_long_shadow_1234abcd",
        symbol="NVDA",
        direction="long",
        strategy_key="elastic_band_reversion",
        surface_class="proposed",
        automation_status="manual_research_only",
        bias_template="bullish_mean_reversion_intraday",
        required_capabilities=[
            "Support for multi-leg debit spreads",
            "Spread-aware execution stress and live monitoring",
        ],
    )
    candidates_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": now,
                "candidates": [market_candidate, elastic_candidate],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    playbook_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": now,
                "contexts": {
                    "QQQ|bearish_trend_intraday|intraday": {
                        "symbol": "QQQ",
                        "bias_template": "bearish_trend_intraday",
                        "horizon": "intraday",
                        "supported_candidates": [
                            {
                                "candidate_id": "market_candidate",
                                "automation_status": "shadow_ready",
                                "rank": 1,
                            }
                        ],
                        "proposed_candidates": [],
                    },
                    "NVDA|bullish_mean_reversion_intraday|intraday": {
                        "symbol": "NVDA",
                        "bias_template": "bullish_mean_reversion_intraday",
                        "horizon": "intraday",
                        "supported_candidates": [],
                        "proposed_candidates": [
                            {
                                "candidate_id": "elastic_candidate",
                                "automation_status": "manual_research_only",
                                "rank": 1,
                            }
                        ],
                    },
                },
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    report = refresh_generated_deployments(
        config_root=config_root,
        deployment_candidates_path=candidates_path,
        playbook_catalog_path=playbook_path,
    )

    assert report.safe_for_live_review is True
    assert report.issues == []
    assert [item["candidate_id"] for item in report.generated_deployments] == ["market_candidate"]
    generated_path = Path(report.generated_deployments[0]["path"])
    assert generated_path.exists()
    generated_manifest = yaml.safe_load(generated_path.read_text(encoding="utf-8"))
    assert generated_manifest["execution"]["shadow_only"] is True
    assert generated_manifest["source"]["metadata"]["automation_lane"] == "automated_shadow"
    assert generated_manifest["source"]["metadata"]["candidate_id"] == "market_candidate"

    manual_board = json.loads(Path(report.output_paths["manual_board_json"]).read_text(encoding="utf-8"))
    assert manual_board["manual_research_candidates"][0]["candidate_id"] == "elastic_candidate"


def test_refresh_generated_deployments_reports_empty_bias_selection(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    (config_root / "deployments").mkdir(parents=True)
    (config_root / "bias_inputs.yaml").write_text("selections: []\n", encoding="utf-8")

    now = datetime.now(UTC).isoformat()
    candidates_path = tmp_path / "deployment_candidates.json"
    playbook_path = tmp_path / "playbook_catalog.json"
    candidates_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": now,
                "candidates": [
                    _candidate_payload(
                        candidate_id="market_candidate",
                        deployment_id="market_impulse_qqq_short_shadow_1234abcd",
                        symbol="QQQ",
                        direction="short",
                        strategy_key="market_impulse",
                        surface_class="supported",
                        automation_status="shadow_ready",
                        bias_template="bearish_trend_intraday",
                    )
                ],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    playbook_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": now,
                "contexts": {},
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    report = refresh_generated_deployments(
        config_root=config_root,
        deployment_candidates_path=candidates_path,
        playbook_catalog_path=playbook_path,
    )

    assert report.safe_for_live_review is False
    assert "empty_bias_selection" in report.issues
    assert "generated_deployments_skipped" in report.issues
    assert report.generated_deployments == []
    assert list((config_root / "deployments" / "generated").glob("*.yaml")) == []


def _candidate_payload(
    *,
    candidate_id: str,
    deployment_id: str,
    symbol: str,
    direction: str,
    strategy_key: str,
    surface_class: str,
    automation_status: str,
    bias_template: str,
    required_capabilities: list[str] | None = None,
) -> dict:
    return {
        "candidate_id": candidate_id,
        "surface_class": surface_class,
        "automation_status": automation_status,
        "strategy_key": strategy_key,
        "symbol": symbol,
        "direction": direction,
        "bias_template": bias_template,
        "horizon": "intraday",
        "ranking_score": 100.0,
        "manifest": {
            **_base_manifest(deployment_id),
            "symbol": symbol,
            "strategy": {
                "key": strategy_key,
                "version": 1,
                "params": {"direction": direction},
            },
            "source": {
                "origin": "mala_loop_v1_1",
                "run_date": "2026-03-31",
                "artifact": "m5_execution_mapping.csv",
                "metadata": {
                    "required_bhiksha_capabilities": required_capabilities or [],
                },
            },
        },
        "evidence": {
            "holdout": {"summary": {"mean_holdout_exp_r": 0.25}},
            "monte_carlo": {"mc_prob_positive_exp": 0.85, "mc_exp_r_p50": 0.21},
        },
        "source": {
            "run_date": "2026-03-31",
            "artifact": "m5_execution_mapping.csv",
        },
    }


def _base_manifest(deployment_id: str) -> dict:
    return {
        "deployment_id": deployment_id,
        "enabled": True,
        "symbol": "QQQ",
        "strategy": {
            "key": "market_impulse",
            "version": 1,
            "params": {"direction": "short"},
        },
        "execution": {
            "profile": "single_leg_long_premium_v1",
            "shadow_only": False,
            "option_mapping": {"long_signal": "CALL", "short_signal": "PUT"},
            "dte_min": 0,
            "dte_max": 7,
            "target_abs_delta_min": 0.2,
            "target_abs_delta_max": 0.4,
            "min_open_interest": 100,
            "max_bid_ask_spread_pct": 0.2,
        },
        "risk": {
            "profile": "conservative_day1",
            "max_trade_premium_usd": 300,
            "hard_flat_time_et": "15:55",
            "stop_loss_pct": 0.45,
        },
        "exit": {
            "profile": "market_impulse_exit_v1",
            "use_algorithmic_exit": True,
            "use_profit_target": False,
            "profit_target_multiple": None,
            "stop_loss_pct": 0.45,
            "stop_to_breakeven_after_r_multiple": None,
            "hard_flat_time_et": "15:55",
        },
        "source": {
            "origin": "mala_loop_v1_1",
            "run_date": "2026-03-31",
            "artifact": "m5_execution_mapping.csv",
            "metadata": {},
        },
    }


def _write_manifest(path: Path, payload: dict) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
