from __future__ import annotations

from bhiksha.packets.capabilities import build_packet_capability_manifest
from bhiksha.packets.runtime_compile import compile_packet_for_runtime
from tests.test_packet_compile import _execution_packet

from bhiksha.shared_kernel import ensure_kernel_on_path

ensure_kernel_on_path()
from mala_bhiksha_kernel import write_packet  # noqa: E402


def test_packet_capability_manifest_declares_reversion_blocked_until_parity(tmp_path):
    manifest = build_packet_capability_manifest()
    packet_path = write_packet(tmp_path, _execution_packet())

    result = compile_packet_for_runtime(packet_path, capability_manifest=manifest)

    assert manifest.capabilities[0].supported is False
    assert result.executable is False
    assert result.block_reasons == ["signal_parity_not_passed"]
