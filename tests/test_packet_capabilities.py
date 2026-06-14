from __future__ import annotations

from bhiksha.packets.capabilities import (
    MANAGEMENT_POLICY_EXIT_PROFILE_CAPABILITY_ID,
    SUPPORTED_MANAGEMENT_POLICY_FIELDS,
    build_packet_capability_manifest,
)
from bhiksha.packets.runtime_compile import compile_packet_for_runtime
from tests.test_packet_compile import _execution_packet, _write_parity_report

from bhiksha.shared_kernel import ensure_kernel_on_path

ensure_kernel_on_path()
from mala_bhiksha_kernel import (  # noqa: E402
    CAPABILITY_CONTRACT_VERSION,
    write_packet,
)


def test_packet_capability_manifest_supports_reversion_shadow_after_signal_parity(tmp_path):
    manifest = build_packet_capability_manifest()
    _write_parity_report(tmp_path)
    packet_path = write_packet(tmp_path, _execution_packet(feature_contract=manifest.feature_contracts[0]))

    result = compile_packet_for_runtime(packet_path, capability_manifest=manifest)

    assert manifest.capabilities[0].supported is True
    assert manifest.capabilities[0].runtime_modes == ["shadow", "live_approval_gated"]
    assert result.executable is True
    assert result.block_reasons == []


def test_capability_manifest_declares_v2_exit_profile_support():
    """Mala fail-closes unless this capability advertises every v2 field.

    The exit-profile evaluator is shadow-first, so the capability advertises only
    the ``shadow`` runtime mode and the exhaustive list of v2 management-policy
    fields the evaluator actually runs.
    """
    manifest = build_packet_capability_manifest()
    capability = manifest.capability_for(MANAGEMENT_POLICY_EXIT_PROFILE_CAPABILITY_ID)

    assert capability is not None
    assert capability.supported is True
    assert capability.runtime_modes == ["shadow"]  # shadow-first; live not yet advertised
    metadata = capability.metadata
    assert metadata["capability_contract_version"] == CAPABILITY_CONTRACT_VERSION
    assert metadata["anchor"] == "option_premium"
    advertised = metadata["supported_management_policy_fields"]
    # Every v2 exit-profile field must be advertised so Mala does not fail closed.
    for field in (
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
    ):
        assert field in advertised
    assert advertised == SUPPORTED_MANAGEMENT_POLICY_FIELDS
