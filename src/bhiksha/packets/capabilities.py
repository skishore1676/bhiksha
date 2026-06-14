"""Bhiksha-owned shared-kernel capability declarations."""

from __future__ import annotations

from pathlib import Path

from bhiksha.shared_kernel import ensure_kernel_on_path

ensure_kernel_on_path()

from mala_bhiksha_kernel import (  # noqa: E402
    CAPABILITY_CONTRACT_VERSION,
    MANAGEMENT_POLICY_SPEC_VERSION,
    CapabilityManifest,
    FeatureContract,
    FeatureSpec,
    RuntimeCapability,
)


MEAN_REVERSION_CONTRACT_ID = "mean_reversion_at_extremes_intraday_v1"

# Capability id for the v2 exit-profile management contract. Mala MUST find this
# (supported == True) before promoting a packet whose management policy carries
# any v2 exit-profile field, otherwise it fails closed. The supported field set
# below is the exhaustive list the profile-aware evaluator
# (``bhiksha.execution.profile_exit``) actually runs.
MANAGEMENT_POLICY_EXIT_PROFILE_CAPABILITY_ID = "management_policy_exit_profile_v2"

SUPPORTED_MANAGEMENT_POLICY_FIELDS = [
    "target_r",
    "option_stop_fallback_pct",
    "hard_flat_time_et",
    "target_1_r",
    "target_2_r",
    "target_1_quantity",
    "initial_stop_pct",
    "premium_disaster_stop_pct",
    "no_progress_seconds",
    "max_hold_seconds",
    "high_water_giveback_policy",
    "breakeven_after_t1",
    "eod_flat",
]


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
                runtime_modes=["shadow", "live_approval_gated"],
                metadata={
                    "adapter": "bhiksha.strategy.intraday_mean_reversion",
                    "event_exporter": "bhiksha.tools.export_reversion_events",
                    "readiness": "signal_parity_passed_live_approval_gated",
                    "live_boundary": "operator_ticket_required",
                },
            ),
            _build_exit_profile_capability(),
        ],
    )


def _build_exit_profile_capability() -> RuntimeCapability:
    """Declare runtime support for the v2 ManagementPolicySpec exit-profile fields.

    Mala feature-detects this capability (matching ``CAPABILITY_CONTRACT_VERSION``
    and the ``supported_management_policy_fields`` list) before promoting any
    packet whose selected management policy carries v2 exit-profile dials. If the
    capability is absent or any required field is missing from the supported list,
    Mala must fail closed (keep the packet in shadow / block live promotion).

    Declared as ``shadow`` only here: the profile-aware evaluator
    (``bhiksha.execution.profile_exit``) is wired live-READY but ships
    SHADOW-FIRST, so it advertises only the shadow runtime mode until parity is
    proven and live is explicitly enabled.
    """
    return RuntimeCapability(
        capability_id=MANAGEMENT_POLICY_EXIT_PROFILE_CAPABILITY_ID,
        label="Operator exit-profile management policy (v2) evaluator",
        supported=True,
        supported_packet_kinds=["execution", "playbook"],
        supported_symbols=[],  # symbol-agnostic exit management
        feature_contracts=[],  # exit management, not a feature contract
        runtime_modes=["shadow"],
        metadata={
            "adapter": "bhiksha.execution.profile_exit",
            "shadow_recorder": "bhiksha.execution.profile_exit_shadow",
            "capability_contract_version": CAPABILITY_CONTRACT_VERSION,
            "management_policy_spec_version": MANAGEMENT_POLICY_SPEC_VERSION,
            "supported_management_policy_fields": SUPPORTED_MANAGEMENT_POLICY_FIELDS,
            "anchor": "option_premium",
            "ladder": [
                "initial_stop|disaster_stop",
                "target_1_partial->stop_to_breakeven",
                "target_2_runner",
                "high_water_giveback",
                "no_progress|max_hold",
                "eod_flat",
            ],
            "fsm_actions": [
                "square_off",
                "partial_scale(square_off reduced-quantity)",
                "stop_to_breakeven(place_stop_loss_order at entry)",
                "hard_flat(square_off)",
            ],
            "readiness": "shadow_first_live_ready",
            "live_boundary": "operator_enablement_required",
        },
    )


def write_packet_capability_manifest(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        build_packet_capability_manifest().model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    return path
