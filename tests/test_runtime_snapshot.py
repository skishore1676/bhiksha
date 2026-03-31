from pathlib import Path

import yaml

from bhiksha.app.bootstrap import build_runtime


def test_runtime_startup_snapshot_includes_fingerprint_and_enabled_deployments() -> None:
    runtime = build_runtime()

    snapshot = runtime.startup_snapshot(live=False, max_bars=5)

    assert "config_fingerprint" in snapshot
    assert len(snapshot["config_fingerprint"]) == 16
    assert snapshot["session"] == {"live": False, "max_bars": 5}
    assert snapshot["app"]["app_name"] == "bhiksha"
    assert snapshot["providers"]["execution_broker_primary"] == "public"
    assert snapshot["bias_inputs"] == []
    assert {deployment["deployment_id"] for deployment in snapshot["deployments"]} >= {
        "market_impulse_qqq_short_v1",
        "market_impulse_spy_short_v1",
    }


def test_build_runtime_respects_configured_bias_inputs_path(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    deployments_root = config_root / "deployments"
    deployments_root.mkdir(parents=True)
    bias_inputs_path = tmp_path / "research" / "bias_inputs.yaml"
    bias_inputs_path.parent.mkdir(parents=True)
    (config_root / "app.yaml").write_text(
        yaml.safe_dump(
            {
                "app_name": "bhiksha",
                "bias_inputs_path": "research/bias_inputs.yaml",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (config_root / "providers.yaml").write_text(
        yaml.safe_dump(
            {
                "underlying_live_primary": "polygon",
                "underlying_backfill_primary": "polygon",
                "execution_broker_primary": "public",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    bias_inputs_path.write_text(
        yaml.safe_dump(
            {
                "selections": [
                    {
                        "symbol": "QQQ",
                        "bias_template": "bearish_trend_intraday",
                        "horizon": "intraday",
                        "enabled": True,
                        "max_active_candidates": 1,
                    }
                ]
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    runtime = build_runtime(config_root)

    assert [selection.symbol for selection in runtime.bias_inputs] == ["QQQ"]
