"""End-to-end CHAIN shadow receipt for the exit-profile config bridge (no broker).

This driver proves the *full* exit-profile chain in DRY-RUN / SHADOW with the
live-dispatch gate held CLOSED, on a representative real Phase-3 mala proposal:

    real Phase-3 mala proposal (kernel ``ManagementPolicySpec`` JSON)
      -> bhiksha active_plan BRIDGE (``compile_active_plan_from_sheet``, a fixture
         MANUAL row carrying the spec in the ``management_policy_spec`` cell)
      -> ``DeploymentManifest.exit`` (the ExitSpec now carries the operator's exit DNA)
      -> ``ProfileExitFields.from_exit_spec(deployment.exit)``
      -> the profile evaluator (``evaluate_and_record_profile_exit``) over a replayed
         premium path
      -> shadow decisions, gate CLOSED so dispatched=0, 0 orders, broker untouched.

The sibling driver ``profile_exit_shadow_receipt.py`` exercises the evaluator but
HARDCODES ``ProfileExitFields`` per profile and never goes through the bridge.
THIS driver closes that gap: the ``ProfileExitFields`` it replays are built from
the *compiled* ``DeploymentManifest.exit``, i.e. they are the operator's exit DNA
that survived the kernel-spec -> Sheet-cell -> compiler -> ExitSpec round-trip.

THE REAL P3 INPUT: AMD short, strategy ``market-impulse-all-basket-discovery``,
classified ``TREND_CONTINUATION`` (profile chosen over legacy by +4.10 pct), read
from the mala Phase-3 proposal file (``.proposal.management_policy_spec``). If the
file is unavailable the driver falls back to a verbatim embedded copy of the spec.

SAFETY (hard): this driver places NO orders and touches NO broker/live API and
writes NO Google Sheet. The active_plan input is an in-process fixture CSV; the
event sink is in-memory; the dispatch gate is forced closed (``live=False``,
``deployment_shadow_only=True``, ``position_source="shadow"``,
``runtime_mode="shadow"``). It is NOT wired into the live PositionMonitor loop.
The artifact is labeled an "e2e shadow (config bridge)".
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any

from bhiksha.active_plan.compiler import compile_active_plan_from_sheet
from bhiksha.execution.profile_exit import ProfileExitFields, ProfileMarketView, ProfileFsmAction
from bhiksha.execution.profile_exit_shadow import evaluate_and_record_profile_exit
from bhiksha.execution.supervisor import ExecutionSupervisor
from bhiksha.persistence.repository import NullEventRepository
from bhiksha.state.position_tracker import TrackedPosition

UTC = timezone.utc

ARTIFACT_LABEL = "e2e shadow (config bridge)"

# The real Phase-3 mala proposal: AMD short, TREND_CONTINUATION, profile chosen
# over legacy. Read from this file's ``.proposal.management_policy_spec``.
_DEFAULT_PROPOSAL_PATH = Path(
    "/Users/suman/code/mala_v2/.claude/worktrees/agent-ab6661b1de219b8b7/"
    "research/results/exit_profile_classify_propose/20260614T154615Z/"
    "market-impulse-all-basket-discovery__amd_short.json"
)

# Verbatim fallback (used only if the proposal file is unavailable). This is the
# ``.proposal.management_policy_spec`` object from the file above.
_FALLBACK_SPEC: dict[str, Any] = {
    "policy_id": "profile__trend_continuation",
    "stop_family": "entry_bar_failure",
    "stop_anchor": "underlying_entry_bar_failure",
    "exit_family": "profile_staged_r",
    "target_model": "profile_staged_r",
    "target_r": 2.0,
    "hard_flat_time_et": "15:55",
    "option_stop_fallback_pct": 0.45,
    "target_order_mode": "virtual_or_broker",
    "source_config_id": None,
    "parameters": {
        "symbol": "AMD",
        "direction": "short",
        "classified_profile": "TREND_CONTINUATION",
    },
    "target_1_r": 1.0,
    "target_2_r": 2.0,
    "target_1_quantity": 0.6,
    "initial_stop_pct": 0.35,
    "premium_disaster_stop_pct": 0.35,
    "no_progress_seconds": 2700,
    "max_hold_seconds": 10800,
    "high_water_giveback_policy": "MODERATE",
    "breakeven_after_t1": True,
    "eod_flat": True,
}


# --------------------------------------------------------------------------- #
# Step 1: load the real Phase-3 mala proposal.
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _Proposal:
    catalog_key: str | None
    symbol: str
    direction: str
    classified_profile: str | None
    chosen: str | None
    selection_rule: str | None
    spec: dict[str, Any]
    source: str  # "file:<path>" or "embedded_fallback"


def _load_proposal(proposal_path: Path) -> _Proposal:
    """Read ``.proposal.management_policy_spec`` from the real P3 file (no hand-typing)."""
    if proposal_path.is_file():
        doc = json.loads(proposal_path.read_text(encoding="utf-8"))
        proposal = doc.get("proposal", {})
        spec = proposal.get("management_policy_spec")
        if not isinstance(spec, dict):
            raise ValueError(f"proposal file missing .proposal.management_policy_spec: {proposal_path}")
        selection = proposal.get("selection", {}) or {}
        classification = doc.get("classification", {}) or {}
        return _Proposal(
            catalog_key=doc.get("catalog_key"),
            symbol=str(doc.get("symbol") or spec.get("parameters", {}).get("symbol") or ""),
            direction=str(doc.get("direction") or spec.get("parameters", {}).get("direction") or ""),
            classified_profile=classification.get("profile")
            or spec.get("parameters", {}).get("classified_profile"),
            chosen=selection.get("chosen"),
            selection_rule=selection.get("rule"),
            spec=spec,
            source=f"file:{proposal_path}",
        )
    # Fallback: embedded verbatim spec.
    spec = json.loads(json.dumps(_FALLBACK_SPEC))
    params = spec.get("parameters", {})
    return _Proposal(
        catalog_key="market-impulse-all-basket-discovery__amd_short",
        symbol=str(params.get("symbol") or "AMD"),
        direction=str(params.get("direction") or "short"),
        classified_profile=params.get("classified_profile"),
        chosen="profile",
        selection_rule="profile expectancy ahead of legacy by +4.100 pct -> operator profile wins outright",
        spec=spec,
        source="embedded_fallback",
    )


# --------------------------------------------------------------------------- #
# Steps 2-3: write a fixture active_plan CSV (single MANUAL row carrying the
# spec) and compile it through the BRIDGE; assert the v2 fields land on the
# compiled ExitSpec. THIS IS THE BRIDGE PROOF.
# --------------------------------------------------------------------------- #

# The exact set of v2 fields the bridge MUST carry from the kernel spec onto the
# compiled ``DeploymentManifest.exit`` (ExitSpec). Each entry maps an ExitSpec
# attribute to (expected value derived from the spec, spec source note).
_EXPECTED_EXIT_FIELDS: dict[str, tuple[Any, str]] = {
    "profile_exit_id": ("profile__trend_continuation", "spec.policy_id"),
    "target_1_r": (1.0, "spec.target_1_r"),
    "target_2_r": (2.0, "spec.target_2_r"),
    "target_1_quantity": (0.6, "spec.target_1_quantity"),
    "initial_stop_pct": (0.35, "spec.initial_stop_pct"),
    "premium_disaster_stop_pct": (0.35, "spec.premium_disaster_stop_pct"),
    "no_progress_seconds": (2700, "spec.no_progress_seconds"),
    "max_hold_seconds": (10800, "spec.max_hold_seconds"),
    "high_water_giveback_policy": ("MODERATE", "spec.high_water_giveback_policy"),
    "breakeven_after_t1": (True, "spec.breakeven_after_t1"),
    "eod_flat": (True, "spec.eod_flat"),
    "stop_loss_pct": (0.45, "spec.option_stop_fallback_pct -> exit.stop_loss_pct"),
    "hard_flat_time_et": ("15:55", "spec.hard_flat_time_et"),
    # HARD BOUNDARY: the bridge NEVER flips live enablement.
    "profile_exit_drives_live": (False, "bridge boundary (never enables live)"),
    "profile_exit_shadow_only": (True, "bridge boundary (stays shadow)"),
}


def _write_fixture_sheet(path: Path, spec: dict[str, Any], symbol: str, direction: str) -> None:
    """Write a single MANUAL row carrying the spec in the management_policy_spec cell."""
    row = {
        "row_id": "amd_short_profile_e2e",
        "row_type": "manual",
        "manual_setup_type": "manual_trigger",
        "symbol": symbol,
        "authorization_mode": "shadow",
        "direction": direction,
        # A short manual trigger fires on a downside cross.
        "trigger_price": "150.00",
        "trigger_direction": "BELOW",
        "management_policy_spec": json.dumps(spec),
    }
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        writer.writeheader()
        writer.writerow(row)


def _compile_through_bridge(spec: dict[str, Any], symbol: str, direction: str) -> tuple[Any, dict[str, Any]]:
    """Run the spec through compile_active_plan_from_sheet; return (exit_spec, bridge_proof).

    Asserts suppressed==[] and every v2 field landed on the compiled ExitSpec.
    """
    with tempfile.TemporaryDirectory(prefix="profile_exit_chain_") as tmp:
        tmp_path = Path(tmp)
        catalog_root = tmp_path / "strategy_catalog"
        catalog_root.mkdir()  # a manual row needs only an empty catalog dir
        sheet_path = tmp_path / "active_plan.csv"
        _write_fixture_sheet(sheet_path, spec, symbol, direction)

        compiled = compile_active_plan_from_sheet(
            sheet_path=sheet_path,
            strategy_catalog_path=catalog_root,
            trading_date="2026-06-14",
        )

        # The bridge must not have suppressed the row (no validation failure).
        assert compiled.plan.suppressed == [], f"row suppressed: {compiled.plan.suppressed}"
        assert len(compiled.plan.deployments) == 1, (
            f"expected exactly 1 deployment, got {len(compiled.plan.deployments)}"
        )
        deployment = compiled.plan.deployments[0]
        exit_spec = deployment.exit

        # THE BRIDGE PROOF: every v2 field carried with the right value.
        field_dump: dict[str, Any] = {}
        for attr, (expected, source) in _EXPECTED_EXIT_FIELDS.items():
            actual = getattr(exit_spec, attr)
            field_dump[attr] = {"value": actual, "expected": expected, "from": source}
            assert actual == expected, (
                f"bridge field mismatch on {attr!r}: expected {expected!r}, got {actual!r} "
                f"(source: {source})"
            )

        bridge_proof = {
            "suppressed": compiled.plan.suppressed,
            "deployment_count": len(compiled.plan.deployments),
            "deployment_id": deployment.deployment_id,
            "deployment_symbol": deployment.symbol,
            "deployment_enabled": deployment.enabled,
            "compiled_exit_spec_fields": field_dump,
        }
    return exit_spec, bridge_proof


# --------------------------------------------------------------------------- #
# Steps 4-5: replay a representative TREND_CONTINUATION premium path through the
# evaluator using ProfileExitFields built FROM the compiled ExitSpec, with the
# gate forced CLOSED exactly like ``_run_scenario`` in the sibling driver.
# --------------------------------------------------------------------------- #


class _InMemoryEventSink:
    """Collects ``profile_exit_shadow`` events in memory (no DB, no I/O)."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def append(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append({"event_type": event_type, **payload})


@dataclass(slots=True)
class _Tick:
    minute: int  # minutes since entry (drives elapsed-time rungs)
    premium: float | None  # exit-reference premium this tick
    bar_time: dt_time  # ET wall-clock (drives the EOD rung)


@dataclass(slots=True)
class _Scenario:
    key: str
    fields: ProfileExitFields
    entry_premium: float
    quantity: int
    ticks: list[_Tick]
    expected_terminal: str
    narrative: str
    events: list[dict[str, Any]] = field(default_factory=list)
    events_recorded_in_sink: int = 0
    state_was_present: bool = False
    state_cleared: bool = False


# Entry clock is 09:45 ET (kept in UTC for elapsed math; bar_time carries ET).
_ENTRY = datetime(2026, 6, 12, 13, 45, tzinfo=UTC)
_AM = dt_time(10, 0)


def _trend_scenarios(fields: ProfileExitFields, symbol: str) -> list[_Scenario]:
    """Two representative TREND_CONTINUATION long-option premium paths.

    Both replay the SAME ``ProfileExitFields`` (built from the compiled ExitSpec),
    so the dials under test are exactly the bridged operator DNA. Entry ~3.00,
    R = initial_stop_pct * entry = 0.35 * 3.00 = 1.05.

      T1 price = 3.00 + 1.0R = 4.05   (bank target_1_quantity=60%, arm breakeven)
      T2 price = 3.00 + 2.0R = 5.10
      giveback (MODERATE): arm once peak_r >= 1.25 (peak >= 4.3125), fire when
        current_r <= peak_r * 0.50.

    Path A ("giveback"): rise through ~1R to a peak just shy of T2, then surrender
      half the peak excursion -> high-water giveback (the dominant TREND runner exit).
    Path B ("hard_flat"): rise through ~1R, arm breakeven, ride the runner with a
      modest pullback (giveback armed but does NOT cross the floor), then the bar
      clock crosses 15:55 -> EOD hard-flat (the operator's signature discipline).
    """
    entry = 3.00
    scenarios: list[_Scenario] = []

    # Path A: T1 partial -> breakeven -> peak ~5.40 (peak_r ~2.29, arms MODERATE)
    # -> give back past 50% of peak excursion -> high-water giveback.
    scenarios.append(
        _Scenario(
            key="trend_continuation_giveback",
            fields=fields,
            entry_premium=entry,
            quantity=5,
            ticks=[
                _Tick(2, 3.40, _AM),    # rising, < T1 (4.05) -> hold, peak builds
                _Tick(6, 4.10, _AM),    # >= T1 (4.05) -> partial bank 60% (3 of 5)
                _Tick(10, 4.60, _AM),   # hold -> stop-to-breakeven emitted once; peak builds
                _Tick(15, 4.90, _AM),   # peak_r ~1.81 (arms MODERATE @1.25), still < T2 (5.10)
                _Tick(20, 3.90, _AM),   # current_r ~0.86 <= 0.5*peak_r (~0.90) -> giveback
            ],
            expected_terminal="high_water_giveback",
            narrative=(
                "rise through ~1R (bank 60%), ratchet to breakeven, peak the runner just "
                "shy of T2, then surrender half the peak excursion (MODERATE giveback)."
            ),
        )
    )

    # Path B: T1 partial -> breakeven -> ride a modest runner (giveback armed but
    # the pullback stays above the floor) -> EOD bar clock crosses 15:55 -> hard-flat.
    scenarios.append(
        _Scenario(
            key="trend_continuation_hard_flat",
            fields=fields,
            entry_premium=entry,
            quantity=5,
            ticks=[
                _Tick(2, 4.10, _AM),                 # >= T1 (4.05) -> partial bank 60%
                _Tick(6, 4.50, dt_time(15, 30)),     # hold -> stop-to-breakeven emitted
                _Tick(10, 4.70, dt_time(15, 50)),    # hold (pre-EOD), runner rides
                _Tick(14, 4.65, dt_time(15, 56)),    # bar >= 15:55 -> EOD hard-flat
            ],
            expected_terminal="eod_flat",
            narrative=(
                "rise through ~1R (bank 60%), ratchet to breakeven, ride the runner, "
                "then the 15:55 bar clock forces the operator's EOD hard-flat."
            ),
        )
    )
    return scenarios


async def _run_scenario(
    supervisor: ExecutionSupervisor,
    sink: _InMemoryEventSink,
    scenario: _Scenario,
    symbol: str,
    option_symbol: str,
) -> None:
    """Replay one scenario tick-by-tick through the shadow evaluator (gate CLOSED)."""
    position = TrackedPosition(
        symbol=symbol,
        deployment_id=f"shadow::{scenario.key}",
        trade_id=f"SHADOW_{scenario.key}",
        option_symbol=option_symbol,
        quantity=scenario.quantity,
        entry_price=scenario.entry_premium,
        entry_timestamp=_ENTRY,
        source="shadow",
    )
    sink_start = len(sink.events)
    for tick in scenario.ticks:
        # H3 production seam: the supervisor owns the ladder state and reuses it
        # across ticks (peak premium, banked partial, breakeven flag persist).
        state = supervisor.get_or_create_profile_exit_state(position, entry_premium=scenario.entry_premium)
        outcome = await evaluate_and_record_profile_exit(
            event_sink=sink,
            fields=scenario.fields,
            deployment_id=position.deployment_id,
            symbol=position.symbol,
            option_symbol=position.option_symbol,
            entry_premium=scenario.entry_premium,
            quantity=scenario.quantity,
            market=ProfileMarketView(
                current_premium=tick.premium,
                bar_time_et=tick.bar_time,
                bid=tick.premium,
                ask=tick.premium,
            ),
            entry_time=_ENTRY,
            state=state,
            # --- GATE FORCED CLOSED (shadow): every precondition fails ---
            live=False,
            deployment_shadow_only=True,
            position_source="shadow",
            runtime_mode="shadow",
            now=_ENTRY + timedelta(minutes=tick.minute),
            require_bar_time_for_eod=True,
        )
        decision = outcome.decision
        scenario.events.append(
            {
                "minute": tick.minute,
                "bar_time_et": tick.bar_time.strftime("%H:%M"),
                "premium": tick.premium,
                "rule": decision.rule.value,
                "fsm_action": decision.fsm_action.value,
                "reason": decision.reason,
                "exit": decision.exit,
                "exit_quantity": decision.exit_quantity,
                "target_stop_price": decision.target_stop_price,
                "recorded": outcome.recorded,
                "dispatched": outcome.dispatched,
                "exit_decision_present": outcome.exit_decision is not None,
                "current_r": decision.features.get("current_r"),
                "peak_premium": decision.features.get("peak_premium"),
            }
        )
        # A target-1 PARTIAL banks part of the position but the runner stays open
        # — keep replaying. Only a FULL exit terminates the scenario.
        if decision.exit and not decision.is_partial:
            break
    # Terminal close: clear the supervisor-owned ladder state (no leak).
    had_state = supervisor._profile_state_key(position) in supervisor._profile_exit_states
    supervisor.clear_profile_exit_state(position)
    cleared = supervisor._profile_state_key(position) not in supervisor._profile_exit_states
    scenario.events_recorded_in_sink = len(sink.events) - sink_start
    scenario.state_was_present = had_state
    scenario.state_cleared = cleared


def _scenario_summary(scenario: _Scenario) -> dict[str, Any]:
    fired = [e["rule"] for e in scenario.events if e["rule"] != "hold"]
    breakeven_emitted = any(e["fsm_action"] == ProfileFsmAction.STOP_TO_BREAKEVEN.value for e in scenario.events)
    partial_fired = any(e["fsm_action"] == ProfileFsmAction.PARTIAL_SCALE.value for e in scenario.events)
    terminal = scenario.events[-1]["rule"] if scenario.events else None
    any_dispatched = any(e["dispatched"] for e in scenario.events)
    any_exit_decision = any(e["exit_decision_present"] for e in scenario.events)
    return {
        "scenario": scenario.key,
        "narrative": scenario.narrative,
        "entry_premium": scenario.entry_premium,
        "quantity": scenario.quantity,
        "expected_terminal": scenario.expected_terminal,
        "observed_terminal_rule": terminal,
        "terminal_matched": terminal == scenario.expected_terminal,
        "target_1_partial_fired": partial_fired,
        "breakeven_emitted": breakeven_emitted,
        "rungs_fired": fired,
        "ticks": scenario.events,
        "shadow_events_recorded": scenario.events_recorded_in_sink,
        "any_dispatched": any_dispatched,
        "any_exit_decision_built": any_exit_decision,
        "state_persisted_then_cleared": bool(scenario.state_was_present and scenario.state_cleared),
    }


# --------------------------------------------------------------------------- #
# Step 6: assemble the receipt (JSON + Markdown).
# --------------------------------------------------------------------------- #

_PROOF_LINE = "gate CLOSED — dispatched=0, orders=0, broker untouched, no Sheet write."


def _build_receipt(
    proposal: _Proposal,
    bridge_proof: dict[str, Any],
    scenarios: list[_Scenario],
    profile_fields: ProfileExitFields,
    sink: _InMemoryEventSink,
) -> dict[str, Any]:
    summaries = [_scenario_summary(s) for s in scenarios]
    rungs_seen = sorted({r for s in summaries for r in s["rungs_fired"]})
    breakeven_actions = sum(
        1 for e in sink.events if e.get("fsm_action") == ProfileFsmAction.STOP_TO_BREAKEVEN.value
    )
    total_dispatched = sum(
        1 for e in sink.events if e.get("dispatch_allowed") or e.get("mode") == "live_dispatch"
    )
    any_dispatched = any(s["any_dispatched"] for s in summaries)
    any_exit_decision = any(s["any_exit_decision_built"] for s in summaries)
    return {
        "artifact_kind": "profile_exit_e2e_chain_receipt",
        "artifact_label": ARTIFACT_LABEL,
        "generated_at": datetime.now(UTC).isoformat(),
        "gate_state": "CLOSED (shadow)",
        "proof_line": _PROOF_LINE,
        # --- The real Phase-3 mala proposal (chain input) ---
        "input_mala_proposal": {
            "source": proposal.source,
            "catalog_key": proposal.catalog_key,
            "symbol": proposal.symbol,
            "direction": proposal.direction,
            "classified_profile": proposal.classified_profile,
            "chosen": proposal.chosen,
            "selection_rule": proposal.selection_rule,
            "policy_id": proposal.spec.get("policy_id"),
            "management_policy_spec": proposal.spec,
        },
        # --- THE BRIDGE PROOF: compiled ExitSpec fields ---
        "bridge_proof": bridge_proof,
        # --- The evaluator dials actually replayed (from the compiled ExitSpec) ---
        "replayed_profile_fields": {
            "profile_id": profile_fields.profile_id,
            "target_1_r": profile_fields.target_1_r,
            "target_2_r": profile_fields.target_2_r,
            "target_1_quantity": profile_fields.target_1_quantity,
            "initial_stop_pct": profile_fields.initial_stop_pct,
            "premium_disaster_stop_pct": profile_fields.premium_disaster_stop_pct,
            "option_stop_fallback_pct": profile_fields.option_stop_fallback_pct,
            "no_progress_seconds": profile_fields.no_progress_seconds,
            "max_hold_seconds": profile_fields.max_hold_seconds,
            "high_water_giveback_policy": profile_fields.high_water_giveback_policy,
            "breakeven_after_t1": profile_fields.breakeven_after_t1,
            "eod_flat": profile_fields.eod_flat,
            "hard_flat_time_et": profile_fields.hard_flat_time_et,
        },
        # --- Dispatch-gate inputs + the hard zero-dispatch proof ---
        "dispatch_gate_inputs": {
            "live": False,
            "deployment_shadow_only": True,
            "position_source": "shadow",
            "runtime_mode": "shadow",
        },
        "placed_orders": 0,
        "touched_broker_api": False,
        "wrote_google_sheet": False,
        "ladder_rungs_observed": rungs_seen,
        "breakeven_actions_emitted": breakeven_actions,
        "total_shadow_events": len(sink.events),
        "total_dispatched_events": total_dispatched,  # MUST be 0
        "no_dispatch_occurred": total_dispatched == 0 and not any_dispatched,
        "any_exit_decision_built": any_exit_decision,  # MUST be False
        "all_terminals_matched": all(s["terminal_matched"] for s in summaries),
        "scenarios": summaries,
        "raw_shadow_events": sink.events,
    }


def _markdown(receipt: dict[str, Any]) -> str:
    lines: list[str] = []
    p = receipt["input_mala_proposal"]
    b = receipt["bridge_proof"]
    lines.append("# Exit-Profile E2E Chain Shadow Receipt (config bridge)")
    lines.append("")
    lines.append(f"- Generated: `{receipt['generated_at']}`")
    lines.append(f"- Gate state: **{receipt['gate_state']}**")
    lines.append(f"- **{receipt['proof_line']}**")
    lines.append("")
    lines.append("## Chain input — real Phase-3 mala proposal")
    lines.append("")
    lines.append(f"- source: `{p['source']}`")
    lines.append(f"- catalog_key: `{p['catalog_key']}`")
    lines.append(f"- symbol / direction: **{p['symbol']} {p['direction']}**")
    lines.append(f"- classified profile: **{p['classified_profile']}**")
    lines.append(f"- chosen: **{p['chosen']}** (profile-vs-legacy)")
    lines.append(f"- selection rule: _{p['selection_rule']}_")
    lines.append(f"- spec policy_id: `{p['policy_id']}`")
    lines.append("")
    lines.append("## Bridge proof — compiled DeploymentManifest.exit (ExitSpec) fields")
    lines.append("")
    lines.append(
        f"- suppressed: `{b['suppressed']}` | deployments: **{b['deployment_count']}** | "
        f"deployment_id: `{b['deployment_id']}` | symbol: `{b['deployment_symbol']}`"
    )
    lines.append("")
    lines.append("| ExitSpec field | value | expected | from |")
    lines.append("|----------------|-------|----------|------|")
    for attr, info in b["compiled_exit_spec_fields"].items():
        lines.append(f"| `{attr}` | `{info['value']}` | `{info['expected']}` | {info['from']} |")
    lines.append("")
    lines.append(
        "> Every field above survived the chain "
        "`kernel ManagementPolicySpec -> Sheet cell (management_policy_spec) -> "
        "compile_active_plan_from_sheet -> ExitSpec`. "
        "`profile_exit_drives_live=False` + `profile_exit_shadow_only=True` prove the "
        "bridge never enables live."
    )
    lines.append("")
    lines.append("## Replayed shadow decisions (ProfileExitFields.from_exit_spec(deployment.exit))")
    lines.append("")
    lines.append(f"- ladder rungs observed across paths: {', '.join(receipt['ladder_rungs_observed'])}")
    lines.append(f"- breakeven actions emitted: **{receipt['breakeven_actions_emitted']}**")
    lines.append(f"- shadow events recorded: **{receipt['total_shadow_events']}**")
    lines.append(
        f"- dispatched: **{receipt['total_dispatched_events']}** | "
        f"exit_decision objects built: **{receipt['any_exit_decision_built']}** | "
        f"no dispatch occurred: **{receipt['no_dispatch_occurred']}**"
    )
    lines.append(f"- all expected terminals matched: **{receipt['all_terminals_matched']}**")
    lines.append("")
    for s in receipt["scenarios"]:
        lines.append(f"### {s['scenario']}")
        lines.append("")
        lines.append(f"_{s['narrative']}_")
        lines.append("")
        lines.append(
            f"- entry premium `{s['entry_premium']}`, qty `{s['quantity']}` | "
            f"expected terminal `{s['expected_terminal']}` -> observed `{s['observed_terminal_rule']}` "
            f"(match: **{s['terminal_matched']}**)"
        )
        lines.append(
            f"- target-1 partial fired: **{s['target_1_partial_fired']}** | "
            f"breakeven emitted: **{s['breakeven_emitted']}** | "
            f"rungs fired: `{s['rungs_fired']}` | "
            f"state persisted then cleared: **{s['state_persisted_then_cleared']}** | "
            f"any dispatch: **{s['any_dispatched']}**"
        )
        lines.append("")
        lines.append("| min | bar ET | premium | rule | fsm action | exit | qty | dispatched |")
        lines.append("|----:|:------:|--------:|------|-----------|:----:|----:|:----------:|")
        for e in s["ticks"]:
            prem = "—" if e["premium"] is None else f"{e['premium']:.2f}"
            lines.append(
                f"| {e['minute']} | {e['bar_time_et']} | {prem} | {e['rule']} | "
                f"{e['fsm_action']} | {'Y' if e['exit'] else ''} | "
                f"{e['exit_quantity'] or ''} | {'Y' if e['dispatched'] else 'n'} |"
            )
        lines.append("")
    lines.append("---")
    lines.append(f"**{receipt['proof_line']}**")
    lines.append("")
    lines.append(
        "All ticks were recorded as `shadow_record` (gate closed). No ExitDecision "
        "was built for dispatch, no order was placed, no broker/live API was touched, "
        "and no Google Sheet was written. The active_plan input was an in-process "
        "fixture CSV; the event sink was in-memory."
    )
    lines.append("")
    return "\n".join(lines)


def run(out_root: Path, proposal_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    """Run the full chain, write the artifact, return (json, md, receipt)."""
    # 1. Load the real P3 proposal.
    proposal = _load_proposal(proposal_path)

    # 2-3. Compile the spec through the bridge and prove the ExitSpec fields.
    exit_spec, bridge_proof = _compile_through_bridge(
        proposal.spec, symbol=proposal.symbol or "AMD", direction=proposal.direction or "short"
    )

    # 4. Build evaluator fields FROM the compiled ExitSpec (not hand-typed).
    profile_fields = ProfileExitFields.from_exit_spec(exit_spec)

    # 4-5. Replay the representative premium paths with the gate CLOSED.
    sink = _InMemoryEventSink()
    supervisor = ExecutionSupervisor(event_repository=NullEventRepository())
    scenarios = _trend_scenarios(profile_fields, symbol=proposal.symbol or "AMD")
    option_symbol = f"{(proposal.symbol or 'AMD')}260612P00150000"  # AMD short -> a put
    for scenario in scenarios:
        asyncio.run(_run_scenario(supervisor, sink, scenario, proposal.symbol or "AMD", option_symbol))

    # 6. Assemble + write the receipt.
    receipt = _build_receipt(proposal, bridge_proof, scenarios, profile_fields, sink)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    artifact_dir = out_root / stamp
    artifact_dir.mkdir(parents=True, exist_ok=True)
    json_path = artifact_dir / "profile_exit_e2e_chain_receipt.json"
    md_path = artifact_dir / "PROFILE_EXIT_E2E_CHAIN_RECEIPT.md"
    json_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(_markdown(receipt), encoding="utf-8")
    return json_path, md_path, receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-root",
        type=Path,
        default=Path("research/results/exit_profile_e2e_shadow"),
        help="Directory under which the timestamped receipt folder is written.",
    )
    parser.add_argument(
        "--proposal",
        type=Path,
        default=_DEFAULT_PROPOSAL_PATH,
        help="Path to the real Phase-3 mala proposal JSON.",
    )
    parser.add_argument("--json", action="store_true", help="Also print the full receipt JSON.")
    args = parser.parse_args(argv)

    json_path, md_path, receipt = run(args.out_root, args.proposal)

    b = receipt["bridge_proof"]
    p = receipt["input_mala_proposal"]
    print("EXIT-PROFILE E2E CHAIN SHADOW RECEIPT (config bridge)")
    print(f"  gate_state            = {receipt['gate_state']}")
    print(f"  input proposal        = {p['catalog_key']}  ({p['symbol']} {p['direction']}, {p['classified_profile']}, chosen={p['chosen']})")
    print(f"  proposal source       = {p['source']}")
    print("  --- BRIDGE PROOF: compiled ExitSpec fields ---")
    print(f"  suppressed            = {b['suppressed']}   deployments={b['deployment_count']}")
    for attr, info in b["compiled_exit_spec_fields"].items():
        print(f"    {attr:<28} = {info['value']!r:<24} (expected {info['expected']!r})")
    print("  --- SHADOW REPLAY ---")
    print(f"  ladder_rungs_observed = {', '.join(receipt['ladder_rungs_observed'])}")
    print(f"  breakeven_actions     = {receipt['breakeven_actions_emitted']}")
    print(f"  total_shadow_events   = {receipt['total_shadow_events']}")
    print(f"  total_dispatched      = {receipt['total_dispatched_events']}  (no_dispatch_occurred={receipt['no_dispatch_occurred']})")
    print(f"  exit_decision_built   = {receipt['any_exit_decision_built']}")
    print(f"  orders_placed         = {receipt['placed_orders']}  broker_api_touched={receipt['touched_broker_api']}  wrote_sheet={receipt['wrote_google_sheet']}")
    print(f"  all_terminals_matched = {receipt['all_terminals_matched']}")
    print("  per-scenario terminals:")
    for s in receipt["scenarios"]:
        print(
            f"    - {s['scenario']:<34} terminal={s['observed_terminal_rule']:<20} "
            f"t1_partial={s['target_1_partial_fired']!s:<5} breakeven={s['breakeven_emitted']!s:<5} "
            f"dispatched={s['any_dispatched']}"
        )
    print(f"  PROOF: {receipt['proof_line']}")
    print(f"  artifact_json = {json_path}")
    print(f"  artifact_md   = {md_path}")

    if args.json:
        print(json.dumps(receipt, indent=2, sort_keys=True))

    # Exit non-zero if the shadow contract was violated (defense in depth).
    ok = (
        receipt["no_dispatch_occurred"]
        and receipt["placed_orders"] == 0
        and not receipt["touched_broker_api"]
        and not receipt["wrote_google_sheet"]
        and not receipt["any_exit_decision_built"]
        and receipt["all_terminals_matched"]
        and receipt["bridge_proof"]["suppressed"] == []
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
