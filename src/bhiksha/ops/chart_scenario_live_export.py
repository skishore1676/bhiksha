"""Read-only Schwab export for one chart-scenario observation cycle."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from mala_bhiksha_kernel import canonical_sha256

from bhiksha.chart_scenarios.cycle import CYCLE_INPUT_SCHEMA
from bhiksha.chart_scenarios.models import timestamp_json
from bhiksha.chart_scenarios.repository import ScenarioEventRepository
from bhiksha.chart_scenarios.timeframes import is_xnys_session_date
from bhiksha.chart_scenarios.validation import ShadowPlan, validate_bundle
from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import Bar, OptionContractSnapshot, OptionSelectionRequest
from bhiksha.market_data.adapters.base import UnderlyingBarSource
from bhiksha.market_data.bar_store import RollingBarStore
from bhiksha.options.chain_service import OptionChainService
from bhiksha.options.selectors import SelectorEmptyError, SingleLegOptionSelector


_NEW_YORK = ZoneInfo("America/New_York")
_SUPPORTED_TIMEFRAMES = {"39m", "daily"}
_AGGREGATION_SCHEMA = "bhiksha.chart-scenario-bar-provenance.v1"
_AGGREGATION_IMPLEMENTATION = "xnys-session-anchor-v1"


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
            bars = await bar_source.warm_start(symbol, start - timedelta(days=30), end)
            completed = _completed_bars(bars, evaluated_at=now)
            if not completed:
                errors.append(_diagnostic("bars", ValueError("no completed bars")))
        except Exception as exc:  # noqa: BLE001 - retain other candidate evidence.
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
            if not selected_symbol:
                selected_symbol = selected_from_chain.option_symbol
        except (
            SelectorEmptyError,
            httpx.HTTPError,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            if not persisted_symbol:
                errors.append(_diagnostic("option_selection", exc))

        option_selection = _option_selection_evidence(
            contracts=contracts,
            selected_from_chain=selected_from_chain,
            effective_selected_symbol=selected_symbol,
            candidate_id=candidate_id,
            symbol=symbol,
            direction=direction,
            evaluated_at=now,
            plan=sealed,
            mode=("persisted_contract" if persisted_symbol else "canonical_selector"),
        )

        quotes: list[dict[str, Any]] = []
        if selected_symbol:
            quote_contract = next(
                (
                    contract
                    for contract in contracts
                    if contract.option_symbol == selected_symbol
                ),
                None,
            )
            try:
                raw_quote = await quote_client.quote(selected_symbol)
                quotes.append(
                    _live_quote(
                        raw_quote,
                        option_symbol=selected_symbol,
                        fallback_contract=quote_contract,
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
                **_timeframe_evidence(
                    completed,
                    timeframe=timeframe,
                    evaluated_at=now,
                ),
            }
            for timeframe in required_timeframes
        }
        rows.append(
            {
                "candidate_id": candidate_id,
                "symbol": symbol,
                "bars_by_timeframe": bars_by_timeframe,
                "option_selection": option_selection,
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


def _aggregate_completed_bars(
    bars: list[Bar], *, timeframe: str, evaluated_at: datetime
) -> list[Bar]:
    if timeframe not in _SUPPORTED_TIMEFRAMES:
        raise ValueError(f"unsupported chart-scenario timeframe: {timeframe}")
    local_cutoff = evaluated_at.astimezone(_NEW_YORK)
    regular = [
        bar
        for bar in bars
        if is_xnys_session_date((local := bar.timestamp.astimezone(_NEW_YORK)).date())
        and (local.hour, local.minute) >= (9, 30)
        and (local.hour, local.minute) < (16, 0)
    ]
    sessions: dict[object, list[Bar]] = {}
    for bar in regular:
        sessions.setdefault(bar.timestamp.astimezone(_NEW_YORK).date(), []).append(bar)
    if timeframe == "daily":
        return [
            _aggregate_bucket(values, timestamp=max(item.timestamp for item in values))
            for session_day, values in sorted(sessions.items())
            if session_day < local_cutoff.date()
        ]
    minutes = 39
    aggregated: list[Bar] = []
    for session_day, values in sorted(sessions.items()):
        session_open = datetime.combine(
            session_day, datetime.min.time(), tzinfo=_NEW_YORK
        ).replace(hour=9, minute=30)
        buckets: dict[int, list[Bar]] = {}
        for bar in values:
            local = bar.timestamp.astimezone(_NEW_YORK)
            offset = int((local - session_open).total_seconds() // 60)
            buckets.setdefault(offset // minutes, []).append(bar)
        for bucket_ordinal, bucket in sorted(buckets.items()):
            bucket_start = session_open + timedelta(minutes=bucket_ordinal * minutes)
            bucket_end = bucket_start + timedelta(minutes=minutes)
            expected = [bucket_start + timedelta(minutes=index) for index in range(minutes)]
            actual = [
                item.timestamp.astimezone(_NEW_YORK)
                for item in sorted(bucket, key=lambda item: item.timestamp)
            ]
            if bucket_end.time() > datetime.min.replace(hour=16).time():
                continue
            if bucket_end > local_cutoff or actual != expected:
                continue
            # Match Cartographer: a 39m bar is session-open anchored and labeled
            # by its bucket start; provenance below records its visible-through time.
            aggregated.append(_aggregate_bucket(bucket, timestamp=bucket_start.astimezone(UTC)))
    return aggregated


def _option_selection_evidence(
    *,
    contracts: list[OptionContractSnapshot],
    selected_from_chain: OptionContractSnapshot | None,
    effective_selected_symbol: str | None,
    candidate_id: str,
    symbol: str,
    direction: str,
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
    body = {
        "schema_version": "bhiksha.chart-scenario-option-selection.v1",
        "mode": mode,
        "policy_hash": plan.option_selection_policy.content_hash,
        "evaluated_at": timestamp_json(evaluated_at),
        "request": request_payload,
        "contracts": [asdict(contract) for contract in contracts],
        "canonical_selected_option_symbol": canonical_selection,
        "effective_selected_option_symbol": effective_selected_symbol,
    }
    return {**body, "receipt_hash": canonical_sha256(body)}


def _aggregate_bucket(values: list[Bar], *, timestamp: datetime) -> Bar:
    ordered = sorted(values, key=lambda item: item.timestamp)
    return Bar(
        symbol=ordered[0].symbol,
        timestamp=timestamp,
        open=ordered[0].open,
        high=max(item.high for item in ordered),
        low=min(item.low for item in ordered),
        close=ordered[-1].close,
        volume=sum(item.volume for item in ordered),
    )


def _timeframe_evidence(
    bars: list[Bar], *, timeframe: str, evaluated_at: datetime
) -> dict[str, Any]:
    aggregated = _aggregate_completed_bars(
        bars, timeframe=timeframe, evaluated_at=evaluated_at
    )
    source_payload = [_bar_payload(bar) for bar in bars]
    output_payload = [_bar_payload(bar) for bar in aggregated]
    completed_through = (
        max(bar.timestamp for bar in bars).astimezone(UTC) if bars else None
    )
    provenance_body = {
        "schema_version": _AGGREGATION_SCHEMA,
        "implementation": _AGGREGATION_IMPLEMENTATION,
        "calendar": "XNYS",
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
