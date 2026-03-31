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
