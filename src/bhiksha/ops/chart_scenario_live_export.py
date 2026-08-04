"""Read-only Schwab export for one chart-scenario observation cycle."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
from mala_bhiksha_kernel import canonical_sha256

from bhiksha.chart_scenarios.cycle import CYCLE_INPUT_SCHEMA
from bhiksha.chart_scenarios.models import timestamp_json
from bhiksha.chart_scenarios.repository import ScenarioEventRepository
from bhiksha.chart_scenarios.validation import ShadowPlan, validate_bundle
from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import Bar, OptionContractSnapshot, OptionSelectionRequest
from bhiksha.market_data.adapters.base import UnderlyingBarSource
from bhiksha.market_data.bar_store import RollingBarStore
from bhiksha.options.chain_service import OptionChainService
from bhiksha.options.selectors import SelectorEmptyError, SingleLegOptionSelector


async def export_live_cycle_input(
    plan: ShadowPlan,
    *,
    repository: ScenarioEventRepository,
    bar_source: UnderlyingBarSource,
    chain_service: OptionChainService,
    quote_client: Any,
    evaluated_at: datetime | None = None,
    observation_slot_ordinal: int | None = None,
) -> dict[str, Any]:
    """Capture completed bars and an authentic selected-option quote per candidate."""

    sealed = validate_bundle(plan.model_dump(mode="json"))
    now = (evaluated_at or datetime.now(UTC)).astimezone(UTC)
    candidates = sorted({scenario.candidate_id for scenario in sealed.scenarios})
    ordinal = observation_slot_ordinal or repository.next_observation_slot_ordinal(
        run_id=str(sealed.run_manifest["run_id"]),
        candidate_ids=tuple(candidates),
    )
    rows: list[dict[str, Any]] = []
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
        end = min(
            now,
            max(scenario.observation_window.end_at for scenario in scenarios),
        )
        errors: list[dict[str, str]] = []
        try:
            bars = await bar_source.warm_start(symbol, start, end)
            completed = _completed_bars(bars, evaluated_at=now)
            if not completed:
                errors.append(_diagnostic("bars", ValueError("no completed bars")))
        except Exception as exc:  # noqa: BLE001 - retain other candidate evidence.
            completed = []
            errors.append(_diagnostic("bars", exc))

        selected_symbol = _persisted_contract(
            repository, scenarios, trigger_version=sealed.trigger_version
        )
        selected_from_chain: OptionContractSnapshot | None = None
        if not selected_symbol:
            try:
                contracts = await chain_service.get_chain(
                    symbol,
                    contract_type=(
                        sealed.option_selection_policy.long_signal_contract_type
                        if direction == "long"
                        else sealed.option_selection_policy.short_signal_contract_type
                    ),
                    from_date=now.date(),
                )
                selected_from_chain = _select_contract(
                    contracts,
                    candidate_id=candidate_id,
                    symbol=symbol,
                    direction=direction,
                    evaluated_at=now,
                    plan=sealed,
                )
                selected_symbol = selected_from_chain.option_symbol
            except (
                SelectorEmptyError,
                httpx.HTTPError,
                OSError,
                RuntimeError,
                ValueError,
            ) as exc:
                errors.append(_diagnostic("option_selection", exc))

        quotes: list[dict[str, Any]] = []
        if selected_symbol:
            try:
                raw_quote = await quote_client.quote(selected_symbol)
                quotes.append(
                    _live_quote(
                        raw_quote,
                        option_symbol=selected_symbol,
                        fallback_contract=selected_from_chain,
                        policy_hash=str(sealed.option_selection_policy.content_hash),
                        selection_mode=(
                            "persisted_contract"
                            if selected_from_chain is None
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
        rows.append(
            {
                "candidate_id": candidate_id,
                "symbol": symbol,
                "bars": [_bar_payload(bar) for bar in completed],
                "quotes": quotes,
                "diagnostics": {
                    "provider_id": sealed.option_selection_policy.provider_id,
                    "option_selection_policy_hash": (
                        sealed.option_selection_policy.content_hash
                    ),
                    "bar_count": len(completed),
                    "quote_count": len(quotes),
                    "comparable": bool(completed and quotes and not errors),
                    "errors": errors,
                },
            }
        )
    body = {
        "schema_version": CYCLE_INPUT_SCHEMA,
        "plan_hash": sealed.plan_hash,
        "run_manifest_hash": sealed.run_manifest_hash,
        "treatment_manifest_hash": sealed.treatment_manifest_hash,
        "observation_slot_ordinal": ordinal,
        "evaluated_at": timestamp_json(now),
        "candidates": rows,
    }
    return {**body, "content_hash": canonical_sha256(body)}


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


def _live_quote(
    payload: Mapping[str, Any],
    *,
    option_symbol: str,
    fallback_contract: OptionContractSnapshot | None,
    policy_hash: str,
    selection_mode: str,
) -> dict[str, Any]:
    raw = payload.get(option_symbol) or payload.get(option_symbol.replace(" ", ""))
    if not isinstance(raw, Mapping):
        values = list(payload.values())
        if len(values) == 1 and isinstance(values[0], Mapping):
            raw = values[0]
        else:
            raise ValueError("Schwab quote response omitted selected contract")
    quote = raw.get("quote") if isinstance(raw.get("quote"), Mapping) else raw
    reference = (
        raw.get("reference") if isinstance(raw.get("reference"), Mapping) else {}
    )
    bid = _optional_float(quote.get("bidPrice", quote.get("bid")))
    ask = _optional_float(quote.get("askPrice", quote.get("ask")))
    last = _optional_float(quote.get("lastPrice", quote.get("last")))
    quote_time = _provider_timestamp(
        quote.get("quoteTime") if bid is not None and ask is not None else None,
        quote.get("tradeTime"),
    )
    if quote_time is None:
        raise ValueError("Schwab option quote has no provider timestamp")
    parsed_type, parsed_expiration, parsed_strike, parsed_root = _occ_contract(
        option_symbol
    )
    strike = _optional_float(reference.get("strikePrice"))
    if strike is None:
        strike = fallback_contract.strike if fallback_contract else parsed_strike
    delta = _optional_float(quote.get("delta"))
    if delta is None and fallback_contract is not None:
        delta = fallback_contract.delta
    open_interest = _optional_int(quote.get("openInterest"))
    if open_interest is None and fallback_contract is not None:
        open_interest = fallback_contract.open_interest
    facts: dict[str, Any] = {
        "option_symbol": option_symbol.replace(" ", ""),
        "underlying_symbol": str(
            reference.get("underlyingSymbol")
            or (
                fallback_contract.underlying_symbol
                if fallback_contract
                else parsed_root
            )
        ),
        "contract_type": str(reference.get("contractType") or parsed_type).upper(),
        "expiration_date": str(reference.get("expirationDate") or parsed_expiration),
        "quote_time": timestamp_json(quote_time),
        "source_id": "schwab-option-quote",
        "bid": bid,
        "ask": ask,
        "last": last,
        "strike": strike,
        "delta": delta,
        "open_interest": open_interest,
        "scenario_id": None,
        "is_selected": True,
        "provenance": {
            "provider_id": "schwab",
            "option_selection_policy_hash": policy_hash,
            "selection_mode": selection_mode,
            "raw_source_hash": canonical_sha256(raw),
        },
    }
    identity = canonical_sha256(
        {"schema": "bhiksha.chart-scenario-live-option-snapshot.v1", **facts}
    )
    return {
        "snapshot_id": "schwab-" + identity[:24],
        **facts,
        "snapshot_hash": None,
    }


def _provider_timestamp(*values: Any) -> datetime | None:
    for value in values:
        if value is None:
            continue
        try:
            if isinstance(value, int | float) and not isinstance(value, bool):
                return datetime.fromtimestamp(float(value) / 1000, tz=UTC)
            parsed = datetime.fromisoformat(str(value))
            if parsed.tzinfo is not None:
                return parsed.astimezone(UTC)
        except (OverflowError, TypeError, ValueError):
            continue
    return None


def _occ_contract(option_symbol: str) -> tuple[str, str, float, str]:
    compact = option_symbol.replace(" ", "")
    index = next((i for i, char in enumerate(compact) if char.isdigit()), -1)
    if index < 1 or len(compact) < index + 15:
        raise ValueError("selected option symbol is not a normalized OCC symbol")
    root = compact[:index]
    yymmdd = compact[index : index + 6]
    side = compact[index + 6]
    strike_digits = compact[index + 7 : index + 15]
    if side not in {"C", "P"} or not (yymmdd + strike_digits).isdigit():
        raise ValueError("selected option symbol is not a normalized OCC symbol")
    expiration = f"20{yymmdd[:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"
    return (
        "CALL" if side == "C" else "PUT",
        expiration,
        int(strike_digits) / 1000,
        root,
    )


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


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


def _one(values: set[str], field: str) -> str:
    if len(values) != 1:
        raise ValueError(f"candidate arms must share one {field}")
    return next(iter(values))


__all__ = ["export_live_cycle_input"]
