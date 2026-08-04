"""Read-only Schwab export for one chart-scenario observation cycle."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from mala_bhiksha_kernel import canonical_sha256

from bhiksha.chart_scenarios.cycle import CYCLE_INPUT_SCHEMA
from bhiksha.chart_scenarios.models import timestamp_json
from bhiksha.chart_scenarios.quote_evidence import (
    build_live_quote,
    normalize_option_symbol,
    selected_raw_quote,
)
from bhiksha.chart_scenarios.repository import ScenarioEventRepository
from bhiksha.chart_scenarios.timeframes import (
    CALENDAR_VERSION,
    aggregate_completed_bars_with_visibility,
)
from bhiksha.chart_scenarios.validation import ShadowPlan, validate_bundle
from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import Bar, OptionContractSnapshot, OptionSelectionRequest
from bhiksha.market_data.adapters.base import UnderlyingBarSource
from bhiksha.market_data.bar_store import RollingBarStore
from bhiksha.options.chain_service import OptionChainService
from bhiksha.options.selectors import SelectorEmptyError, SingleLegOptionSelector

_AGGREGATION_SCHEMA = "bhiksha.chart-scenario-bar-provenance.v2"
_AGGREGATION_IMPLEMENTATION = "xnys-session-anchor-v2"


async def export_live_cycle_input(
    plan: ShadowPlan,
    *,
    repository: ScenarioEventRepository,
    bar_source: UnderlyingBarSource,
    chain_service: OptionChainService,
    quote_client: Any,
    evaluated_at: datetime | None = None,
    observation_slot_ordinal: int | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Capture completed bars and an authentic selected-option quote per candidate."""

    sealed = validate_bundle(plan.model_dump(mode="json"))
    fixed = evaluated_at.astimezone(UTC) if evaluated_at is not None else None
    capture_now = clock or (lambda: fixed or datetime.now(UTC))
    cycle_started_at = capture_now().astimezone(UTC)
    candidates = sorted({scenario.candidate_id for scenario in sealed.scenarios})
    ordinal = observation_slot_ordinal or repository.next_observation_slot_ordinal(
        run_id=str(sealed.run_manifest["run_id"]),
        candidate_ids=tuple(candidates),
    )
    pending_rows: list[dict[str, Any]] = []
    for candidate_id in candidates:
        scenarios = [
            scenario
            for scenario in sealed.scenarios
            if scenario.candidate_id == candidate_id
        ]
        symbol = _one({scenario.symbol for scenario in scenarios}, "symbol")
        direction = _one(
            {scenario.direction.value for scenario in scenarios}, "direction"
        )
        start = min(scenario.observation_window.start_at for scenario in scenarios)
        requested_at = capture_now().astimezone(UTC)
        end = min(requested_at, max(s.observation_window.end_at for s in scenarios))
        errors: list[dict[str, str]] = []
        try:
            bars = await bar_source.warm_start(symbol, start - timedelta(days=30), end)
            bar_acquired_at = capture_now().astimezone(UTC)
            completed = _completed_bars(bars, evaluated_at=bar_acquired_at)
            if not completed:
                errors.append(_diagnostic("bars", ValueError("no completed bars")))
        except Exception as exc:  # noqa: BLE001 - retain other candidate evidence.
            bar_acquired_at = capture_now().astimezone(UTC)
            completed = []
            errors.append(_diagnostic("bars", exc))

        persisted_symbol = _persisted_contract(
            repository, scenarios, trigger_version=sealed.trigger_version
        )
        selected_symbol = persisted_symbol
        selected_from_chain: OptionContractSnapshot | None = None
        contracts: list[OptionContractSnapshot] = []
        try:
            contracts = await chain_service.get_chain(
                symbol,
                contract_type=(
                    sealed.option_selection_policy.long_signal_contract_type
                    if direction == "long"
                    else sealed.option_selection_policy.short_signal_contract_type
                ),
                from_date=bar_acquired_at.date(),
            )
            chain_acquired_at = capture_now().astimezone(UTC)
            contracts = [_normalize_contract(contract) for contract in contracts]
            selected_from_chain = _select_contract(
                contracts,
                candidate_id=candidate_id,
                symbol=symbol,
                direction=direction,
                evaluated_at=chain_acquired_at,
                plan=sealed,
            )
            if not selected_symbol:
                selected_symbol = selected_from_chain.option_symbol
        except (
            SelectorEmptyError,
            httpx.HTTPError,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            chain_acquired_at = capture_now().astimezone(UTC)
            if not persisted_symbol:
                errors.append(_diagnostic("option_selection", exc))

        quotes: list[dict[str, Any]] = []
        if selected_symbol:
            quote_contract = next(
                (
                    contract
                    for contract in contracts
                    if normalize_option_symbol(contract.option_symbol)
                    == normalize_option_symbol(selected_symbol)
                ),
                None,
            )
            try:
                if quote_contract is None:
                    raise ValueError(
                        "selected contract is absent from acquired chain evidence"
                    )
                raw_quote = await quote_client.quote(selected_symbol)
                quote_acquired_at = capture_now().astimezone(UTC)
                raw_source = selected_raw_quote(
                    raw_quote, option_symbol=selected_symbol
                )
                quotes.append(
                    build_live_quote(
                        raw_source,
                        option_symbol=selected_symbol,
                        selected_contract=quote_contract,
                        acquired_at=quote_acquired_at,
                        policy_hash=str(sealed.option_selection_policy.content_hash),
                        selection_mode=(
                            "persisted_contract"
                            if persisted_symbol
                            else "canonical_selector"
                        ),
                    )
                )
            except (
                httpx.HTTPError,
                OSError,
                RuntimeError,
                ValueError,
                KeyError,
                TypeError,
            ) as exc:
                errors.append(_diagnostic("option_quote", exc))
        required_timeframes = sorted(
            {
                condition.timeframe
                for scenario in scenarios
                for condition in (
                    scenario.entry_condition,
                    scenario.validation_condition,
                    scenario.invalidation_condition,
                )
            }
        )
        bars_by_timeframe = {
            timeframe: {
                "timeframe": timeframe,
                "bar_acquired_at": timestamp_json(bar_acquired_at),
                **_timeframe_evidence(
                    completed,
                    timeframe=timeframe,
                    evaluated_at=bar_acquired_at,
                ),
            }
            for timeframe in required_timeframes
        }
        pending_rows.append(
            {
                "candidate_id": candidate_id,
                "symbol": symbol,
                "bars_by_timeframe": bars_by_timeframe,
                "_option_context": {
                    "contracts": contracts,
                    "selected_from_chain": selected_from_chain,
                    "effective_selected_symbol": selected_symbol,
                    "direction": direction,
                    "chain_acquired_at": chain_acquired_at,
                    "mode": (
                        "persisted_contract"
                        if persisted_symbol
                        else "canonical_selector"
                    ),
                },
                "quotes": quotes,
                "diagnostics": {
                    "provider_id": sealed.option_selection_policy.provider_id,
                    "option_selection_policy_hash": (
                        sealed.option_selection_policy.content_hash
                    ),
                    "bar_count": sum(
                        len(series["bars"]) for series in bars_by_timeframe.values()
                    ),
                    "bar_counts_by_timeframe": {
                        timeframe: len(series["bars"])
                        for timeframe, series in bars_by_timeframe.items()
                    },
                    "quote_count": len(quotes),
                    "comparable": bool(
                        all(series["bars"] for series in bars_by_timeframe.values())
                        and quotes
                        and not errors
                    ),
                    "errors": errors,
                },
            }
        )
    sealed_at = capture_now().astimezone(UTC)
    if sealed_at < cycle_started_at:
        raise ValueError("cycle seal precedes cycle start")
    rows: list[dict[str, Any]] = []
    for pending in pending_rows:
        context = pending.pop("_option_context")
        pending["option_selection"] = _option_selection_evidence(
            contracts=context["contracts"],
            selected_from_chain=context["selected_from_chain"],
            effective_selected_symbol=context["effective_selected_symbol"],
            candidate_id=pending["candidate_id"],
            symbol=pending["symbol"],
            direction=context["direction"],
            chain_acquired_at=context["chain_acquired_at"],
            evaluated_at=sealed_at,
            plan=sealed,
            mode=context["mode"],
        )
        rows.append(pending)
    body = {
        "schema_version": CYCLE_INPUT_SCHEMA,
        "plan_hash": sealed.plan_hash,
        "run_manifest_hash": sealed.run_manifest_hash,
        "treatment_manifest_hash": sealed.treatment_manifest_hash,
        "observation_slot_ordinal": ordinal,
        "cycle_started_at": timestamp_json(cycle_started_at),
        "evaluated_at": timestamp_json(sealed_at),
        "sealed_at": timestamp_json(sealed_at),
        "candidates": rows,
    }
    return {**body, "content_hash": canonical_sha256(body)}


def _aggregate_completed_bars(
    bars: list[Bar], *, timeframe: str, evaluated_at: datetime
) -> list[Bar]:
    """Return only bars whose full XNYS visibility boundary has passed."""
    return [
        bar
        for bar, _visible_at in aggregate_completed_bars_with_visibility(
            bars, timeframe=timeframe, evaluated_at=evaluated_at
        )
    ]


def _option_selection_evidence(
    *,
    contracts: list[OptionContractSnapshot],
    selected_from_chain: OptionContractSnapshot | None,
    effective_selected_symbol: str | None,
    candidate_id: str,
    symbol: str,
    direction: str,
    chain_acquired_at: datetime,
    evaluated_at: datetime,
    plan: ShadowPlan,
    mode: str,
) -> dict[str, Any]:
    request = OptionSelectionRequest(
        deployment_id=f"chart-scenario:{candidate_id}",
        symbol=symbol,
        direction=SignalDirection(direction),
        signal_timestamp=evaluated_at,
        execution_profile="chart_scenario_shadow_v1",
        execution_params=plan.option_selection_policy.selector_params(),
    )
    request_payload = {
        **asdict(request),
        "direction": request.direction.value,
        "signal_timestamp": timestamp_json(request.signal_timestamp),
    }
    canonical_selection = (
        selected_from_chain.option_symbol if selected_from_chain is not None else None
    )
    contracts_payload = [asdict(contract) for contract in contracts]
    chain_body = {
        "schema_version": "bhiksha.chart-scenario-option-chain-evidence.v1",
        "provider_id": plan.option_selection_policy.provider_id,
        "observed_at": timestamp_json(chain_acquired_at),
        "contract_count": len(contracts_payload),
        "contracts_hash": canonical_sha256(contracts_payload),
    }
    body = {
        "schema_version": "bhiksha.chart-scenario-option-selection.v3",
        "mode": mode,
        "provider_id": plan.option_selection_policy.provider_id,
        "observed_at": timestamp_json(chain_acquired_at),
        "chain_acquired_at": timestamp_json(chain_acquired_at),
        "policy_hash": plan.option_selection_policy.content_hash,
        "evaluated_at": timestamp_json(evaluated_at),
        "request": request_payload,
        "contracts": contracts_payload,
        "chain_evidence": {
            **chain_body,
            "content_hash": canonical_sha256(chain_body),
        },
        "canonical_selected_option_symbol": canonical_selection,
        "effective_selected_option_symbol": effective_selected_symbol,
    }
    return {**body, "receipt_hash": canonical_sha256(body)}


def _normalize_contract(contract: OptionContractSnapshot) -> OptionContractSnapshot:
    normalized_symbol = contract.option_symbol.replace(" ", "").upper()
    normalized_underlying = contract.underlying_symbol.strip().upper()
    normalized_type = contract.contract_type.strip().upper()
    expiration = date.fromisoformat(contract.expiration_date).isoformat()
    return OptionContractSnapshot(
        option_symbol=normalized_symbol,
        underlying_symbol=normalized_underlying,
        contract_type=normalized_type,
        expiration_date=expiration,
        dte=contract.dte,
        strike=contract.strike,
        delta=contract.delta,
        bid=contract.bid,
        ask=contract.ask,
        open_interest=contract.open_interest,
    )


def _timeframe_evidence(
    bars: list[Bar], *, timeframe: str, evaluated_at: datetime
) -> dict[str, Any]:
    aggregated_with_visibility = aggregate_completed_bars_with_visibility(
        bars, timeframe=timeframe, evaluated_at=evaluated_at
    )
    source_payload = [_source_bar_payload(bar) for bar in bars]
    output_payload = [
        _bar_payload(bar) for bar, _visible_at in aggregated_with_visibility
    ]
    completed_through = (
        max(visible_at for _bar, visible_at in aggregated_with_visibility)
        if aggregated_with_visibility
        else None
    )
    provenance_body = {
        "schema_version": _AGGREGATION_SCHEMA,
        "implementation": _AGGREGATION_IMPLEMENTATION,
        "calendar": "XNYS",
        "calendar_version": CALENDAR_VERSION,
        "timezone": "America/New_York",
        "session_anchor": "09:30",
        "interval": timeframe,
        "completed_through": (
            timestamp_json(completed_through) if completed_through else None
        ),
        "source_bar_count": len(source_payload),
        "source_hash": canonical_sha256(source_payload),
        "output_hash": canonical_sha256(output_payload),
    }
    return {
        "provenance": {
            **provenance_body,
            "content_hash": canonical_sha256(provenance_body),
        },
        "source_bars": source_payload,
        "bars": output_payload,
    }


def _completed_bars(bars: list[Bar], *, evaluated_at: datetime) -> list[Bar]:
    minute_floor = evaluated_at.replace(second=0, microsecond=0)
    by_identity = {
        (bar.symbol, bar.timestamp.astimezone(UTC)): bar
        for bar in bars
        if bar.timestamp.astimezone(UTC) < minute_floor
    }
    store = RollingBarStore(max_bars_per_symbol=max(len(by_identity), 1))
    for bar in sorted(by_identity.values(), key=lambda item: item.timestamp):
        store.append(bar)
    symbols = {bar.symbol for bar in by_identity.values()}
    if not symbols:
        return []
    return store.get(_one(symbols, "bar symbol"))


def _persisted_contract(
    repository: ScenarioEventRepository,
    scenarios: list[Any],
    *,
    trigger_version: str,
) -> str | None:
    selected: set[str] = set()
    for scenario in scenarios:
        state = repository.get_state(scenario, trigger_version) or {}
        value = str(state.get("selected_option_symbol") or "").strip()
        if value:
            selected.add(value)
    if len(selected) > 1:
        raise ValueError("paired arms disagree on the persisted selected contract")
    return next(iter(selected), None)


def _select_contract(
    contracts: list[OptionContractSnapshot],
    *,
    candidate_id: str,
    symbol: str,
    direction: str,
    evaluated_at: datetime,
    plan: ShadowPlan,
) -> OptionContractSnapshot:
    request = OptionSelectionRequest(
        deployment_id=f"chart-scenario:{candidate_id}",
        symbol=symbol,
        direction=SignalDirection(direction),
        signal_timestamp=evaluated_at,
        execution_profile="chart_scenario_shadow_v1",
        execution_params=plan.option_selection_policy.selector_params(),
    )
    selection = SingleLegOptionSelector().select(request, contracts)
    matches = [
        item for item in contracts if item.option_symbol == selection.option_symbol
    ]
    if len(matches) != 1:
        raise ValueError("canonical selector did not resolve one source contract")
    return matches[0]


def _diagnostic(stage: str, error: Exception) -> dict[str, str]:
    return {"stage": stage, "error_type": type(error).__name__, "error": str(error)}


def _bar_payload(bar: Bar) -> dict[str, Any]:
    return {
        "timestamp": timestamp_json(bar.timestamp),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "completed": True,
        "bar_id": None,
    }


def _source_bar_payload(bar: Bar) -> dict[str, Any]:
    return {
        "symbol": bar.symbol,
        "timestamp": timestamp_json(bar.timestamp),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
    }


def _one(values: set[str], field: str) -> str:
    if len(values) != 1:
        raise ValueError(f"candidate arms must share one {field}")
    return next(iter(values))


__all__ = ["export_live_cycle_input"]
