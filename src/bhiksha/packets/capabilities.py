"""Bhiksha-owned shared-kernel capability declarations."""

from __future__ import annotations

from pathlib import Path

from bhiksha.shared_kernel import ensure_kernel_on_path

ensure_kernel_on_path()

from mala_bhiksha_kernel import (  # noqa: E402
    CapabilityManifest,
    FeatureContract,
    FeatureSpec,
    RuntimeCapability,
)


MEAN_REVERSION_CONTRACT_ID = "mean_reversion_at_extremes_intraday_v1"


def build_packet_capability_manifest() -> CapabilityManifest:
    contract = FeatureContract(
        contract_id=MEAN_REVERSION_CONTRACT_ID,
        bar_interval="1m",
        session="rth",
        provider="polygon",
        warmup_bars=60,
        features=[
            FeatureSpec(name="opening_vwap_rth", provider_sensitive=True),
            FeatureSpec(name="prior_rth_close_atr", provider_sensitive=True),
            FeatureSpec(name="vpoc_4h", provider_sensitive=True),
            FeatureSpec(name="market_pulse_stage", provider_sensitive=True),
            FeatureSpec(name="gap_state_rth_open", provider_sensitive=True),
            FeatureSpec(name="velocity", provider_sensitive=True),
            FeatureSpec(name="jerk", provider_sensitive=True),
            FeatureSpec(name="relative_volume_rth", provider_sensitive=True),
        ],
    )
    return CapabilityManifest(
        manifest_id="bhiksha.packet_capabilities.v1",
        feature_contracts=[contract],
        capabilities=[
            RuntimeCapability(
                capability_id=MEAN_REVERSION_CONTRACT_ID,
                label="IWM/QQQ mean-reversion runtime adapter",
                supported=True,
                supported_packet_kinds=["execution"],
                supported_symbols=["IWM", "QQQ"],
                feature_contracts=[contract.contract_id],
                runtime_modes=["shadow"],
                metadata={
                    "adapter": "bhiksha.strategy.intraday_mean_reversion",
                    "event_exporter": "bhiksha.tools.export_reversion_events",
                    "readiness": "signal_parity_passed_entry_shadow_only",
                },
            )
        ],
    )


def write_packet_capability_manifest(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        build_packet_capability_manifest().model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    return path
