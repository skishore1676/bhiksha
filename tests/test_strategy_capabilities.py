from __future__ import annotations

from bhiksha.strategy.capabilities import (
    derive_strategy_variant,
    evaluate_strategy_capability,
    load_capability_manifest,
)
from bhiksha.tools.generate_runtime_capabilities import generate_runtime_capability_manifest


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


def test_capability_manifest_marks_market_impulse_descendants_supported() -> None:
    manifest = load_capability_manifest()

    for strategy_name, entry_mode in (
        ("MI High Close Reclaim", "close_location_reclaim"),
        ("MI Shallow Spring", "same_bar_shallow_reclaim"),
        ("MI Push Through", "continuation_confirmation"),
    ):
        capability = evaluate_strategy_capability(
            strategy_key="market_impulse",
            strategy_name=strategy_name,
            strategy_params={"entry_mode": entry_mode},
            thesis_exit_policy="ma_trailing_underlying",
            manifest=manifest,
        )

        assert capability.strategy_variant == entry_mode
        assert capability.status == "supported"
        assert capability.supported is True


def test_capability_manifest_keeps_delayed_reclaim_unsupported() -> None:
    manifest = load_capability_manifest()

    capability = evaluate_strategy_capability(
        strategy_key="market_impulse",
        strategy_name="MI Second Touch",
        strategy_params={"entry_mode": "delayed_reclaim"},
        thesis_exit_policy="fixed_rr_underlying",
        manifest=manifest,
    )

    assert capability.strategy_variant == "delayed_reclaim"
    assert capability.status == "unsupported"
    assert capability.reason == "runtime_adapter_not_implemented"
    assert capability.supported is False


def test_capability_manifest_marks_compression_expansion_breakout_supported() -> None:
    manifest = load_capability_manifest()

    capability = evaluate_strategy_capability(
        strategy_key="compression_expansion_breakout",
        strategy_name="Compression Expansion Breakout",
        strategy_params={"breakout_lookback": 20, "compression_factor": 0.7, "compression_window": 15},
        thesis_exit_policy="time_stop_underlying",
        manifest=manifest,
    )

    assert capability.strategy_variant == "default"
    assert capability.status == "supported"
    assert capability.supported is True


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


def test_runtime_capability_manifest_is_verified_and_loadable(tmp_path) -> None:
    output = tmp_path / "bhiksha_runtime_capabilities_v2.json"

    payload = generate_runtime_capability_manifest(output_path=output)
    manifest = load_capability_manifest(output)
    capability = evaluate_strategy_capability(
        strategy_key="opening_drive_classifier",
        strategy_variant="default",
        thesis_exit_policy="fixed_rr_underlying",
        manifest=manifest,
    )

    assert payload["version"] == 2
    assert payload["verification"]["status"] == "passed"
    assert capability.status == "supported"
    assert capability.supported is True
