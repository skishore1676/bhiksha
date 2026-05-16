from __future__ import annotations

import asyncio
import json
from pathlib import Path

from bhiksha.domain.models import OptionContractSnapshot
from bhiksha.execution.order_manager import PublicQuote
from bhiksha.packets.option_preview import build_playbook_option_preview
from bhiksha.shared_kernel import ensure_kernel_on_path
from bhiksha.tools.preview_playbook_option import main as preview_main

ensure_kernel_on_path()

from mala_bhiksha_kernel import (  # noqa: E402
    ExecutionPacket,
    FeatureContract,
    FeatureSpec,
    OperatorApproval,
    PacketKind,
    PacketLineage,
    PacketRef,
    PacketStatus,
    RuntimeMode,
    SourceArtifact,
    write_packet,
)


class StubChainService:
    def __init__(self, *, bid: float = 2.70, ask: float = 2.90, open_interest: int = 500) -> None:
        self.calls = 0
        self.bid = bid
        self.ask = ask
        self.open_interest = open_interest

    async def get_chain(self, symbol: str, **kwargs):
        self.calls += 1
        return [
            OptionContractSnapshot(
                option_symbol=f"{symbol}260330P00558000",
                underlying_symbol=symbol,
                contract_type="PUT",
                expiration_date="2026-03-30",
                dte=0,
                strike=558.0,
                delta=-0.31,
                bid=self.bid,
                ask=self.ask,
                open_interest=self.open_interest,
            )
        ]

    async def close(self):
        return None


class StubOrderManager:
    def __init__(self, *, bid: float = 2.70, ask: float = 2.90, open_interest: int = 500) -> None:
        self.quote_calls = 0
        self.bid = bid
        self.ask = ask
        self.open_interest = open_interest

    async def get_option_quote(self, option_symbol: str):
        self.quote_calls += 1
        return PublicQuote(
            symbol=option_symbol,
            bid=self.bid,
            ask=self.ask,
            last=(self.bid + self.ask) / 2,
            open_interest=self.open_interest,
            outcome="SUCCESS",
        )

    async def close(self):
        return None


def test_option_preview_blocks_non_take_intent_without_provider_calls(tmp_path: Path) -> None:
    intent_path = _write_intent(tmp_path, status="operator_pass", decision="pass", execution_ready=False)
    packet_path = write_packet(tmp_path, _execution_packet())
    chain = StubChainService()
    order_manager = StubOrderManager()

    result = asyncio.run(
        build_playbook_option_preview(
            intent_artifact=intent_path,
            packet_path=packet_path,
            chain_service=chain,
            order_manager=order_manager,
            out_root=tmp_path / "previews",
        )
    )

    assert result.status == "blocked"
    assert result.preview_ready is False
    assert "intent_status_not_shadow_ready:operator_pass" in result.block_reasons
    assert chain.calls == 0
    assert order_manager.quote_calls == 0


def test_option_preview_writes_ready_preview_without_order_submission(tmp_path: Path) -> None:
    intent_path = _write_intent(tmp_path)
    packet_path = write_packet(tmp_path, _execution_packet())
    chain = StubChainService()
    order_manager = StubOrderManager()

    result = asyncio.run(
        build_playbook_option_preview(
            intent_artifact=intent_path,
            packet_path=packet_path,
            chain_service=chain,
            order_manager=order_manager,
            out_root=tmp_path / "previews",
            underlying_price=210.25,
        )
    )

    assert result.status == "option_preview_ready"
    assert result.preview_ready is True
    assert result.option_symbol == "IWM260330P00558000"
    assert result.quantity == 1
    assert result.estimated_entry_price == 2.90
    assert result.underlying_entry_price == 210.25
    assert result.risk_reasons == ["approved"]
    assert result.order_submission_allowed is False
    assert result.live_approval_required is True
    payload = json.loads(Path(result.artifact_json).read_text(encoding="utf-8"))
    assert payload["safety_boundary"] == "option_preview_only_no_order_submission"
    assert Path(result.artifact_md).exists()


def test_option_preview_blocks_on_quote_liquidity_risk(tmp_path: Path) -> None:
    intent_path = _write_intent(tmp_path)
    packet_path = write_packet(tmp_path, _execution_packet())

    result = asyncio.run(
        build_playbook_option_preview(
            intent_artifact=intent_path,
            packet_path=packet_path,
            chain_service=StubChainService(bid=2.80, ask=2.90),
            order_manager=StubOrderManager(bid=2.00, ask=2.90),
            out_root=tmp_path / "previews",
        )
    )

    assert result.status == "blocked"
    assert result.preview_ready is False
    assert result.block_reasons == ["public_spread_above_maximum"]


def test_option_preview_requires_packet_preview_boundary(tmp_path: Path) -> None:
    intent_path = _write_intent(tmp_path)
    packet_path = write_packet(
        tmp_path,
        _execution_packet(runtime_controls={"allowed_management_policy_ids": ["reversal_extreme__fixed_1r"]}),
    )

    result = asyncio.run(
        build_playbook_option_preview(
            intent_artifact=intent_path,
            packet_path=packet_path,
            chain_service=StubChainService(),
            order_manager=StubOrderManager(),
            out_root=tmp_path / "previews",
        )
    )

    assert result.status == "blocked"
    assert "packet_option_preview_only_missing" in result.block_reasons
    assert "packet_live_automated_boundary_missing" in result.block_reasons
    assert "packet_shadow_only_missing" in result.block_reasons


def test_preview_playbook_option_cli_returns_block_code_for_pass_intent(tmp_path: Path) -> None:
    intent_path = _write_intent(tmp_path, status="operator_pass", decision="pass", execution_ready=False)
    packet_path = write_packet(tmp_path, _execution_packet())

    code = preview_main(
        [
            "--intent-artifact",
            str(intent_path),
            "--packet",
            str(packet_path),
            "--out-root",
            str(tmp_path / "previews"),
        ]
    )

    assert code == 2


def _write_intent(
    tmp_path: Path,
    *,
    status: str = "shadow_intent_ready",
    decision: str = "take",
    execution_ready: bool = True,
) -> Path:
    payload = {
        "status": status,
        "decision": decision,
        "execution_ready": execution_ready,
        "execution_mode": "shadow",
        "packet_id": "execution.mean_reversion_at_extremes.iwm_qqq",
        "packet_version": 1,
        "symbol": "IWM",
        "direction": "short",
        "timestamp": "2026-05-11 09:40 America/Chicago",
        "selected_management_policy_id": "reversal_extreme__fixed_1r",
        "order_submission_allowed": False,
        "artifact_json": str(tmp_path / "intent.json"),
        "consultation_artifact": str(tmp_path / "consultation.json"),
    }
    path = tmp_path / "intent.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _execution_packet(runtime_controls: dict | None = None) -> ExecutionPacket:
    return ExecutionPacket(
        packet_id="execution.mean_reversion_at_extremes.iwm_qqq",
        version=1,
        status=PacketStatus.APPROVED,
        title="IWM/QQQ Mean Reversion Execution",
        symbol_scope=["IWM", "QQQ"],
        intended_horizon="intraday-short-horizon",
        feature_contract=FeatureContract(
            contract_id="mean_reversion_at_extremes_intraday_v1",
            bar_interval="1m",
            session="rth",
            provider="polygon",
            warmup_bars=60,
            features=[FeatureSpec(name="opening_vwap_rth", provider_sensitive=True)],
        ),
        lineage=PacketLineage(
            source_system="mala_v2",
            source_artifacts=[SourceArtifact(label="test", uri="data/results/playbooks/test")],
        ),
        operator_approval=OperatorApproval(status="approved", actor="operator"),
        source_packet=PacketRef(
            packet_id="playbook.mean_reversion_at_extremes.iwm_qqq",
            version=1,
            kind=PacketKind.PLAYBOOK,
        ),
        runtime_mode=RuntimeMode.SHADOW,
        capability_manifest_id="bhiksha.test",
        parity_report_id="parity.mean_reversion.test",
        runtime_controls=runtime_controls
        or {
            "allowed_management_policy_ids": ["reversal_extreme__fixed_1r"],
            "shadow_only": True,
            "live_automated_allowed": False,
            "operator_must_select_management_policy": True,
            "option_selection_preview_only": True,
        },
    )
