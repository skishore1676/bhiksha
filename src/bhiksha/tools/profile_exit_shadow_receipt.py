"""Fixture SHADOW receipt for the profile-aware exit evaluator (no broker, no orders).

This is a self-contained driver that replays a representative multi-tick option
*premium* path through the profile exit ladder for each of the four operator exit
profiles, with the live-dispatch gate held CLOSED (shadow). It exists to:

* exercise ``evaluate_and_record_profile_exit`` end-to-end against synthetic
  premium paths and prove that **no dispatch occurs** while the gate is closed;
* give ``ExecutionSupervisor.get_or_create_profile_exit_state`` /
  ``clear_profile_exit_state`` their first production caller, proving ladder
  state persists across ticks (H3) and is cleared at the end of a position;
* emit an INSPECTABLE receipt artifact (JSON + a readable Markdown summary)
  showing, per profile, the tick-by-tick premium, every ladder rung that fired
  (target-1 partial -> breakeven -> target-2 / giveback / no-progress /
  max-hold / EOD), the recorded shadow events, and that dispatch never happened.

SAFETY (hard): this driver places NO orders and touches NO broker/live API. The
inputs are synthetic fixtures; the event sink is in-memory; the dispatch gate is
forced closed (``live=False``, ``deployment_shadow_only=True``,
``position_source="shadow"``). It is NOT wired into the live PositionMonitor
loop. The artifact is labeled a "fixture shadow".

Profile dial provenance: ``public_api_trading_v3`` operator exit profiles
(``src/domain/trading/exit_policies/profiles.py``, operator session 2026-06-12).
The dials are copied here (not imported) so the driver stays self-contained and
does not depend on a sibling repo at runtime; percentages are expressed in the
fractional form the bhiksha evaluator consumes (75.0% -> 0.75, 25% -> 0.25).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any

from bhiksha.execution.profile_exit import ProfileExitFields, ProfileMarketView, ProfileFsmAction
from bhiksha.execution.profile_exit_shadow import evaluate_and_record_profile_exit
from bhiksha.execution.supervisor import ExecutionSupervisor
from bhiksha.persistence.repository import NullEventRepository
from bhiksha.state.position_tracker import TrackedPosition

UTC = timezone.utc

ARTIFACT_LABEL = "fixture shadow"


class _InMemoryEventSink:
    """Collects ``profile_exit_shadow`` events in memory (no DB, no I/O)."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def append(self, event_type: str, payload: dict[str, Any]) -> None:
        self.events.append({"event_type": event_type, **payload})


# --------------------------------------------------------------------------- #
# The four operator exit profiles (dials copied from public_api_trading_v3).
# --------------------------------------------------------------------------- #


def _flash_reversal() -> ProfileExitFields:
    return ProfileExitFields(
        profile_id="FLASH_REVERSAL",
        target_1_r=1.0,
        target_2_r=2.0,
        target_1_quantity=0.75,
        initial_stop_pct=0.25,
        premium_disaster_stop_pct=0.30,
        no_progress_seconds=15 * 60,
        max_hold_seconds=90 * 60,
        high_water_giveback_policy="STRICT",
        breakeven_after_t1=True,
        eod_flat=True,
        hard_flat_time_et="15:55",
    )


def _exhaustion_reversal() -> ProfileExitFields:
    return ProfileExitFields(
        profile_id="EXHAUSTION_REVERSAL",
        target_1_r=1.0,
        target_2_r=2.5,
        target_1_quantity=0.50,
        initial_stop_pct=0.40,
        premium_disaster_stop_pct=0.45,
        no_progress_seconds=75 * 60,
        max_hold_seconds=4 * 60 * 60,
        high_water_giveback_policy="MODERATE",
        breakeven_after_t1=True,
        eod_flat=True,
        hard_flat_time_et="15:55",
    )


def _trend_continuation() -> ProfileExitFields:
    return ProfileExitFields(
        profile_id="TREND_CONTINUATION",
        target_1_r=1.0,
        target_2_r=2.0,
        target_1_quantity=0.60,
        initial_stop_pct=0.30,
        premium_disaster_stop_pct=0.35,
        no_progress_seconds=45 * 60,
        max_hold_seconds=3 * 60 * 60,
        high_water_giveback_policy="MODERATE",
        breakeven_after_t1=True,
        eod_flat=True,
        hard_flat_time_et="15:55",
    )


def _range_expansion() -> ProfileExitFields:
    return ProfileExitFields(
        profile_id="RANGE_EXPANSION",
        target_1_r=1.0,
        target_2_r=2.0,
        target_1_quantity=0.40,
        initial_stop_pct=0.35,
        premium_disaster_stop_pct=0.40,
        no_progress_seconds=2 * 60 * 60,
        max_hold_seconds=5 * 24 * 60 * 60,
        high_water_giveback_policy="LOOSE",
        breakeven_after_t1=True,
        eod_flat=False,  # the one profile allowed to ride overnight
        hard_flat_time_et="15:55",
    )


@dataclass(slots=True)
class _Tick:
    """One synthetic premium observation on a profile's path."""

    minute: int  # minutes since entry (drives elapsed-time rungs)
    premium: float | None  # exit-reference premium this tick
    bar_time: dt_time  # ET wall-clock (drives the EOD rung)


@dataclass(slots=True)
class _Scenario:
    """A representative path for one profile, ending on a named terminal rung."""

    key: str
    profile_id: str
    fields: ProfileExitFields
    entry_premium: float
    quantity: int
    ticks: list[_Tick]
    expected_terminal: str
    narrative: str
    events: list[dict[str, Any]] = field(default_factory=list)
    # Populated by ``_run_scenario`` (proof metadata).
    events_recorded_in_sink: int = 0
    state_was_present: bool = False
    state_cleared: bool = False


# Entry clock is 09:45 ET (kept in UTC for elapsed math; bar_time carries ET).
_ENTRY = datetime(2026, 6, 12, 13, 45, tzinfo=UTC)
_AM = dt_time(10, 0)


def _scenarios() -> list[_Scenario]:
    """Build one representative path per profile.

    Across the four paths the union of fired rungs covers the full ladder:
    target-1 partial, stop-to-breakeven, target-2 runner, high-water giveback,
    no-progress time stop, max-hold time stop, and EOD hard-flat. FLASH carries
    a second (EOD) path so every terminal rung appears in the receipt.
    """
    scenarios: list[_Scenario] = []

    # 1) FLASH_REVERSAL: rise to T1 (R=0.25*2.0=0.50), bank 75%, breakeven, run to
    #    T2 (entry + 2R = 3.00) -> target-2 runner exit.
    scenarios.append(
        _Scenario(
            key="flash_reversal_target2",
            profile_id="FLASH_REVERSAL",
            fields=_flash_reversal(),
            entry_premium=2.00,
            quantity=4,
            ticks=[
                _Tick(1, 2.10, _AM),   # hold, peak builds
                _Tick(2, 2.55, _AM),   # >= T1 (2.50) -> partial bank 3/4
                _Tick(3, 2.70, _AM),   # hold -> stop-to-breakeven emitted once
                _Tick(4, 2.80, _AM),   # hold
                _Tick(5, 3.05, _AM),   # >= T2 (3.00) -> target-2 runner exit
            ],
            expected_terminal="target_2_runner",
            narrative="rise to T1 (bank 75%), ratchet to breakeven, run to T2.",
        )
    )

    # 2) FLASH_REVERSAL (EOD path): T1 + breakeven, then the bar clock crosses
    #    15:55 while the runner is still held -> EOD hard-flat (highest precedence).
    scenarios.append(
        _Scenario(
            key="flash_reversal_eod_flat",
            profile_id="FLASH_REVERSAL",
            fields=_flash_reversal(),
            entry_premium=2.00,
            quantity=4,
            ticks=[
                _Tick(1, 2.55, _AM),               # >= T1 -> partial bank 3/4
                _Tick(2, 2.60, dt_time(15, 40)),   # hold -> stop-to-breakeven
                _Tick(3, 2.62, dt_time(15, 50)),   # hold (pre-EOD)
                _Tick(4, 2.62, dt_time(15, 56)),   # bar >= 15:55 -> EOD hard-flat
            ],
            expected_terminal="eod_flat",
            narrative="T1 + breakeven, then the EOD bar clock forces a hard-flat.",
        )
    )

    # 3) EXHAUSTION_REVERSAL: R=0.40*2.0=0.80. Rise to T1 (2.80), bank 50%,
    #    breakeven, peak to ~3.8 (peak_r ~1.25 arms MODERATE), then give back 50%
    #    of the peak excursion -> high-water giveback.
    scenarios.append(
        _Scenario(
            key="exhaustion_reversal_giveback",
            profile_id="EXHAUSTION_REVERSAL",
            fields=_exhaustion_reversal(),
            entry_premium=2.00,
            quantity=4,
            ticks=[
                _Tick(1, 2.50, _AM),   # hold
                _Tick(2, 2.85, _AM),   # >= T1 (2.80) -> partial bank 2/4
                _Tick(3, 3.40, _AM),   # hold, peak builds -> breakeven emitted
                _Tick(4, 3.85, _AM),   # peak_r ~ 2.31 (arms MODERATE @1.25)
                _Tick(5, 2.85, _AM),   # gives back > 50% of peak excursion -> exit
            ],
            expected_terminal="high_water_giveback",
            narrative="T1 + breakeven, run up, then surrender half the peak (MODERATE).",
        )
    )

    # 4) TREND_CONTINUATION: a stall that never reaches T1. Premium chops just
    #    above breakeven (peak_r < 0.25 floor) until elapsed >= no_progress (45m)
    #    -> no-progress time stop.
    scenarios.append(
        _Scenario(
            key="trend_continuation_no_progress",
            profile_id="TREND_CONTINUATION",
            fields=_trend_continuation(),
            entry_premium=2.00,
            quantity=4,
            ticks=[
                _Tick(5, 2.05, _AM),    # chop
                _Tick(20, 2.02, _AM),   # chop, no progress
                _Tick(40, 2.06, _AM),   # chop, still < 0.25R peak
                _Tick(46, 2.03, _AM),   # elapsed >= 45m, peak<floor -> no-progress
            ],
            expected_terminal="no_progress",
            narrative="never reaches T1; stalls below the favorable floor past 45m.",
        )
    )

    # 5) RANGE_EXPANSION: R=0.35*2.0=0.70. Rise to T1 (2.70), bank 40%, breakeven,
    #    then hold a runner for days (LOOSE giveback needs 1.5R arm and the runner
    #    stays modest) until elapsed >= max_hold (5 days) -> max-hold time stop.
    #    eod_flat is False so the EOD rung never pre-empts.
    five_days_min = 5 * 24 * 60
    scenarios.append(
        _Scenario(
            key="range_expansion_max_hold",
            profile_id="RANGE_EXPANSION",
            fields=_range_expansion(),
            entry_premium=2.00,
            quantity=4,
            ticks=[
                _Tick(2, 2.75, dt_time(10, 0)),                 # >= T1 (2.70) -> bank 40%
                _Tick(5, 2.80, dt_time(15, 56)),                # hold; eod_flat=False -> NO EOD
                _Tick(60, 2.72, dt_time(11, 0)),                # next session, holding runner
                _Tick(five_days_min + 1, 2.74, dt_time(11, 0)), # elapsed >= 5d -> max-hold
            ],
            expected_terminal="max_hold",
            narrative="T1 + breakeven, ride the runner overnight, exit at the 5-day max-hold.",
        )
    )

    return scenarios


async def _run_scenario(supervisor: ExecutionSupervisor, sink: _InMemoryEventSink, scenario: _Scenario) -> None:
    """Replay one scenario tick-by-tick through the shadow evaluator (gate closed)."""
    position = TrackedPosition(
        symbol="QQQ",
        deployment_id=f"shadow::{scenario.key}",
        trade_id=f"SHADOW_{scenario.key}",
        option_symbol="QQQ260612C00500000",
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
    return {
        "scenario": scenario.key,
        "profile_id": scenario.profile_id,
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
        "state_persisted_then_cleared": bool(scenario.state_was_present and scenario.state_cleared),
    }


def _build_receipt(scenarios: list[_Scenario], sink: _InMemoryEventSink) -> dict[str, Any]:
    summaries = [_scenario_summary(s) for s in scenarios]
    rungs_seen = sorted({r for s in summaries for r in s["rungs_fired"]})
    breakeven_actions = sum(
        1
        for e in sink.events
        if e.get("fsm_action") == ProfileFsmAction.STOP_TO_BREAKEVEN.value
    )
    total_dispatched = sum(1 for e in sink.events if e.get("dispatch_allowed") or e.get("mode") == "live_dispatch")
    return {
        "artifact_kind": "profile_exit_shadow_receipt",
        "artifact_label": ARTIFACT_LABEL,
        "generated_at": datetime.now(UTC).isoformat(),
        "gate_state": "CLOSED (shadow)",
        "dispatch_gate_inputs": {
            "live": False,
            "deployment_shadow_only": True,
            "position_source": "shadow",
            "runtime_mode": "shadow",
        },
        "placed_orders": 0,
        "touched_broker_api": False,
        "profiles_covered": sorted({s.profile_id for s in scenarios}),
        "ladder_rungs_observed": rungs_seen,
        "breakeven_actions_emitted": breakeven_actions,
        "total_shadow_events": len(sink.events),
        "total_dispatched_events": total_dispatched,  # MUST be 0
        "no_dispatch_occurred": total_dispatched == 0,
        "all_terminals_matched": all(s["terminal_matched"] for s in summaries),
        "scenarios": summaries,
        "raw_shadow_events": sink.events,
    }


def _markdown(receipt: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Profile-Exit Shadow Receipt (fixture shadow)")
    lines.append("")
    lines.append(f"- Generated: `{receipt['generated_at']}`")
    lines.append(f"- Gate state: **{receipt['gate_state']}**")
    lines.append(
        f"- Dispatch gate inputs: `{json.dumps(receipt['dispatch_gate_inputs'])}`"
    )
    lines.append(f"- Orders placed: **{receipt['placed_orders']}**  |  Broker/live API touched: **{receipt['touched_broker_api']}**")
    lines.append(
        f"- Shadow events recorded: **{receipt['total_shadow_events']}**  |  "
        f"Dispatched: **{receipt['total_dispatched_events']}**  "
        f"(no dispatch occurred: **{receipt['no_dispatch_occurred']}**)"
    )
    lines.append(f"- Profiles covered: {', '.join(receipt['profiles_covered'])}")
    lines.append(f"- Ladder rungs observed across all paths: {', '.join(receipt['ladder_rungs_observed'])}")
    lines.append(f"- All expected terminals matched: **{receipt['all_terminals_matched']}**")
    lines.append("")
    for s in receipt["scenarios"]:
        lines.append(f"## {s['profile_id']} — {s['scenario']}")
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
    lines.append(
        "All ticks were recorded as `shadow_record` (gate closed). No ExitDecision "
        "was returned for dispatch, no order was placed, and no broker/live API was "
        "touched. Inputs are synthetic fixtures."
    )
    lines.append("")
    return "\n".join(lines)


def run(out_root: Path) -> tuple[Path, Path, dict[str, Any]]:
    """Replay all scenarios, write the artifact, and return (json, md, receipt)."""
    sink = _InMemoryEventSink()
    supervisor = ExecutionSupervisor(event_repository=NullEventRepository())
    scenarios = _scenarios()
    for scenario in scenarios:
        asyncio.run(_run_scenario(supervisor, sink, scenario))

    receipt = _build_receipt(scenarios, sink)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    artifact_dir = out_root / f"shadow_receipt_{stamp}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    json_path = artifact_dir / "profile_exit_shadow_receipt.json"
    md_path = artifact_dir / "PROFILE_EXIT_SHADOW_RECEIPT.md"
    json_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(_markdown(receipt), encoding="utf-8")
    return json_path, md_path, receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-root",
        type=Path,
        default=Path("artifacts/profile_exit_shadow"),
        help="Directory under which the timestamped receipt folder is written.",
    )
    parser.add_argument("--json", action="store_true", help="Also print the full receipt JSON.")
    args = parser.parse_args(argv)

    json_path, md_path, receipt = run(args.out_root)

    # Console summary (also proves no dispatch occurred).
    print("PROFILE-EXIT SHADOW RECEIPT (fixture shadow)")
    print(f"  gate_state            = {receipt['gate_state']}")
    print(f"  profiles_covered      = {', '.join(receipt['profiles_covered'])}")
    print(f"  ladder_rungs_observed = {', '.join(receipt['ladder_rungs_observed'])}")
    print(f"  breakeven_actions     = {receipt['breakeven_actions_emitted']}")
    print(f"  total_shadow_events   = {receipt['total_shadow_events']}")
    print(f"  total_dispatched      = {receipt['total_dispatched_events']}  (no_dispatch_occurred={receipt['no_dispatch_occurred']})")
    print(f"  orders_placed         = {receipt['placed_orders']}  broker_api_touched={receipt['touched_broker_api']}")
    print(f"  all_terminals_matched = {receipt['all_terminals_matched']}")
    print("  per-scenario terminals:")
    for s in receipt["scenarios"]:
        print(
            f"    - {s['profile_id']:<20} {s['scenario']:<32} "
            f"terminal={s['observed_terminal_rule']:<20} "
            f"t1_partial={s['target_1_partial_fired']!s:<5} breakeven={s['breakeven_emitted']!s:<5} "
            f"dispatched={s['any_dispatched']}"
        )
    print(f"  artifact_json = {json_path}")
    print(f"  artifact_md   = {md_path}")

    if args.json:
        print(json.dumps(receipt, indent=2, sort_keys=True))

    # Exit non-zero if the shadow contract was violated (defense in depth).
    ok = receipt["no_dispatch_occurred"] and receipt["placed_orders"] == 0 and not receipt["touched_broker_api"]
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
