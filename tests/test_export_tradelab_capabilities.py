from bhiksha.tools.export_tradelab_capabilities import build_manifest


def test_tradelab_export_preserves_native_support_without_runtime_access() -> None:
    manifest = build_manifest()

    assert manifest["schema"] == "bhiksha.strategy_capabilities.tradelab.v1"
    assert manifest["capabilities"]
    assert any(row["declared_in_code"] for row in manifest["capabilities"])
    assert all(row["operationally_available"] is False for row in manifest["capabilities"])
    assert not any(manifest["protected_effects_performed"].values())
