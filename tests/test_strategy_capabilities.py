from __future__ import annotations

from bhiksha.strategy.capabilities import (
    derive_strategy_variant,
    evaluate_strategy_capability,
    load_capability_manifest,
)


def test_capability_manifest_marks_baseline_market_impulse_supported() -> None:
    manifest = load_capability_manifest()

    capability = evaluate_strategy_capability(
        strategy_key="market_impulse",
        strategy_name="Market Impulse (Cross & Reclaim)",
        strategy_params={"entry_buffer_minutes": 5, "entry_window_minutes": 45},
        thesis_exit_policy="fixed_rr_underlying",
        manifest=manifest,
    )

    assert capability.strategy_variant == "cross_reclaim"
    assert capability.status == "supported"
    assert capability.supported is True


def test_capability_manifest_marks_market_impulse_descendant_unsupported() -> None:
    manifest = load_capability_manifest()

    capability = evaluate_strategy_capability(
        strategy_key="market_impulse",
        strategy_name="MI High Close Reclaim",
        strategy_params={"entry_mode": "close_location_reclaim", "min_close_location": 0.7},
        thesis_exit_policy="fixed_rr_underlying",
        manifest=manifest,
    )

    assert capability.strategy_variant == "close_location_reclaim"
    assert capability.status == "unsupported"
    assert capability.reason == "runtime_adapter_not_implemented"
    assert capability.supported is False


def test_capability_manifest_blocks_unknown_exit_policy() -> None:
    manifest = load_capability_manifest()

    capability = evaluate_strategy_capability(
        strategy_key="market_impulse",
        strategy_variant="cross_reclaim",
        thesis_exit_policy="new_exit_policy",
        manifest=manifest,
    )

    assert capability.status == "unsupported"
    assert capability.exit_policy_status == "unsupported"
    assert capability.reason == "unsupported_thesis_exit_policy:new_exit_policy"


def test_missing_manifest_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("BHIKSHA_CAPABILITIES_PATH", "/tmp/not-a-real-bhiksha-capability-manifest.yaml")

    capability = evaluate_strategy_capability(
        strategy_key="market_impulse",
        strategy_name="Market Impulse (Cross & Reclaim)",
    )

    assert capability.strategy_variant == "cross_reclaim"
    assert capability.status == "unknown_manifest"
    assert capability.supported is False


def test_strategy_variant_derives_from_market_impulse_name() -> None:
    assert derive_strategy_variant(strategy_key="market_impulse", strategy_name="MI High Close Reclaim") == (
        "close_location_reclaim"
    )
