"""Broker-inert chart-scenario observation state machine."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from mala_bhiksha_kernel import (
    ChartScenarioSpec,
    ExitProfile,
    ShadowEventType,
    canonical_sha256,
)

from .exits import ExitObservation, evaluate_exit_profile
from .models import CompletedBar, OptionQuoteSnapshot, as_utc, timestamp_json
from .policies import CostModel, QuoteEligibilityPolicy
from .quotes import (
    ReadOnlyOptionSnapshotSource,
    ensure_read_only_quote_source,
    select_snapshot,
)
from .repository import (
    EventWrite,
    ScenarioEventRepository,
    TerminalScenarioError,
    canonical_observation_slot_id,
)
from .triggers import evaluate_condition, normalize_bars
from .validation import TRIGGER_VERSION, ShadowPlan, validate_bundle


@dataclass(frozen=True, slots=True)
class ObservationResult:
    scenario_id: str
    status: str
    terminal: bool
    new_events: tuple[Any, ...]
    broker_effect_count: int = 0
    error: str | None = None

    @property
    def events(self) -> tuple[Any, ...]:
        """Short alias used by CLI/readback callers."""

        return self.new_events

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "status": self.status,
            "terminal": self.terminal,
            "new_event_count": len(self.new_events),
            "events": [event.model_dump(mode="json") for event in self.new_events],
            "broker_effect_count": self.broker_effect_count,
            "error": self.error,
        }


class BrokerInertScenarioObserver:
    """Observe one validated scenario without an execution capability."""

    def __init__(
        self,
        repository: ScenarioEventRepository,
        *,
        plan: ShadowPlan,
        quote_source: ReadOnlyOptionSnapshotSource | None = None,
        trigger_version: str = TRIGGER_VERSION,
    ) -> None:
        if trigger_version != TRIGGER_VERSION:
            raise ValueError(f"unsupported trigger version: {trigger_version!r}")
        self.repository = repository
        sealed_plan = validate_bundle(plan.model_dump(mode="json"))
        sealed_source = ensure_read_only_quote_source(quote_source)
        self.quote_snapshots = (
            tuple(sealed_source.snapshots) if sealed_source is not None else ()
        )
        self.exit_policy_registry = dict(sealed_plan.exit_policy_registry)
        self.plan_hash = sealed_plan.plan_hash
        self.run_manifest_hash = sealed_plan.run_manifest_hash.removeprefix("sha256:")
        self.treatment_manifest_hash = sealed_plan.treatment_manifest_hash.removeprefix(
            "sha256:"
        )
        self.run_input_hashes_hash = canonical_sha256(
            sealed_plan.run_manifest["input_hashes"]
        )
        self.cartographer_receipt_hash = (
            sealed_plan.cartographer_receipt_hash.removeprefix("sha256:")
        )
        self.cartographer_export_hash = (
            sealed_plan.cartographer_export_hash.removeprefix("sha256:")
        )
        self.observation_window_hash = (
            sealed_plan.target_session_window_hash.removeprefix("sha256:")
        )
        self.run_id = str(sealed_plan.run_manifest["run_id"])
        self.installed_scenario_hashes = {
            (item.arm_id.value, item.scenario_id): item.scenario_hash
            for item in sealed_plan.scenarios
        }
        self.expected_arms_by_candidate: dict[str, tuple[str, ...]] = {}
        for item in sealed_plan.scenarios:
            arms = set(self.expected_arms_by_candidate.get(item.candidate_id, ()))
            arms.add(item.arm_id.value)
            self.expected_arms_by_candidate[item.candidate_id] = tuple(sorted(arms))
        self.policy_registry_hash = canonical_sha256(
            {
                profile.value: policy.model_dump(mode="json")
                for profile, policy in sorted(
                    self.exit_policy_registry.items(), key=lambda item: item[0].value
                )
            }
        )
        self.cost_model = CostModel.model_validate(
            sealed_plan.cost_model.model_dump(mode="json")
        )
        self.quote_eligibility_policy = QuoteEligibilityPolicy.model_validate(
            sealed_plan.quote_eligibility_policy.model_dump(mode="json")
        )
        self.treatment_hash = canonical_sha256(
            {
                "policy_registry_hash": self.policy_registry_hash,
                "cost_model_hash": self.cost_model.content_hash,
                "quote_eligibility_policy_hash": self.quote_eligibility_policy.content_hash,
            }
        )
        self.trigger_version = trigger_version
        self.market_fact_proof: dict[str, Any] | None = None

    def observe_one(
        self,
        scenario: ChartScenarioSpec,
        *,
        bars: Sequence[CompletedBar | Mapping[str, Any]] = (),
        option_quote: OptionQuoteSnapshot | Mapping[str, Any] | None = None,
        quote_path: Sequence[OptionQuoteSnapshot | Mapping[str, Any]] = (),
        evaluated_at: datetime | str | None = None,
        market_observation_id: str | None = None,
        observation_slot_ordinal: int = 1,
    ) -> ObservationResult:
        """Replay one read-only cycle from supplied bars/quotes.

        ``quote_path`` is a supplied, immutable tape.  If an entry is made in
        this call, its first quote is used for the synthetic entry and later
        quotes are evaluated as the exit path.  A replay of the same cycle uses
        event identities and state rather than re-arming the scenario.  The
        legacy caller label is deliberately ignored; the installed run and
        ``observation_slot_ordinal`` own the canonical observation identity.
        """

        del market_observation_id

        if any(not isinstance(bar, Mapping) for bar in bars):
            raise TypeError("observer bars require exact raw mappings")
        if option_quote is not None and not isinstance(option_quote, Mapping):
            raise TypeError("observer option_quote requires an exact raw mapping")
        if any(not isinstance(quote, Mapping) for quote in quote_path):
            raise TypeError("observer quote_path requires exact raw mappings")
        normalized_bars = normalize_bars(bars)
        single_quote = (
            self._coerce_quote(option_quote) if option_quote is not None else None
        )
        normalized_path = [self._coerce_quote(quote) for quote in quote_path]
        timestamps = [bar.timestamp for bar in normalized_bars]
        timestamps.extend(quote.quote_time for quote in normalized_path)
        if single_quote is not None:
            timestamps.append(single_quote.quote_time)
        now = (
            as_utc(evaluated_at)
            if evaluated_at is not None
            else (
                max(timestamps) if timestamps else scenario.observation_window.start_at
            )
        )
        facts_payload = {
            "bars": [bar.to_dict() for bar in normalized_bars],
            "option_quote": single_quote.to_dict()
            if single_quote is not None
            else None,
            "quote_path": [quote.to_dict() for quote in normalized_path],
            "evaluated_at": timestamp_json(now),
        }
        self.market_facts_hash = canonical_sha256(facts_payload)
        new_events: list[Any] = []
        observation_id = canonical_observation_slot_id(
            run_manifest_hash=self.run_manifest_hash,
            ordinal=observation_slot_ordinal,
        )

        try:
            if scenario.run_id != self.run_id:
                raise ValueError("scenario run_id differs from installed plan run")
            if (
                self.installed_scenario_hashes.get(
                    (scenario.arm_id.value, scenario.scenario_id)
                )
                != scenario.scenario_hash
            ):
                raise ValueError("scenario is not an exact installed plan artifact")
            self._validate_scenario_policies(scenario)
            expected_arms = self.expected_arms_by_candidate.get(scenario.candidate_id)
            if expected_arms is None or scenario.arm_id.value not in expected_arms:
                raise ValueError("scenario is not installed in the observer plan")
            preexisting_state = self.repository.get_state(
                scenario, self.trigger_version
            )
            if preexisting_state is not None:
                if preexisting_state["terminal"]:
                    return self._result(scenario, preexisting_state, new_events)
                if self._recover_latched_primary_exit(
                    new_events, scenario, preexisting_state
                ):
                    recovered_state = (
                        self.repository.get_state(scenario, self.trigger_version)
                        or preexisting_state
                    )
                    return self._result(scenario, recovered_state, new_events)
            self.market_fact_proof = self.repository.bind_market_facts(
                scenario,
                observation_slot_ordinal=observation_slot_ordinal,
                run_manifest_hash=self.run_manifest_hash,
                plan_hash=self.plan_hash,
                treatment_manifest_hash=self.treatment_manifest_hash,
                run_input_hashes_hash=self.run_input_hashes_hash,
                cartographer_receipt_hash=self.cartographer_receipt_hash,
                cartographer_export_hash=self.cartographer_export_hash,
                observation_window_hash=self.observation_window_hash,
                expected_arm_ids=expected_arms,
                facts_hash=self.market_facts_hash,
                evaluated_at=timestamp_json(now),
            )
            observation_id = str(self.market_fact_proof["slot_id"])
            is_new = self.repository.register_scenario(
                scenario,
                self.trigger_version,
                plan_hash=self.plan_hash,
                policy_registry_hash=self.policy_registry_hash,
                treatment_hash=self.treatment_hash,
            )
            if is_new:
                self._record(
                    new_events,
                    scenario,
                    ShadowEventType.INSTALLED,
                    event_time=scenario.observation_window.start_at,
                    market_observation_id="install-" + scenario.scenario_hash[:24],
                    role="installed",
                    details=self._details(
                        scenario,
                        {"status": "installed", "reason": "validated_shadow_plan"},
                    ),
                    state_updates={"status": "installed"},
                )

            state = self.repository.get_state(scenario, self.trigger_version)
            if state is None:
                raise RuntimeError("scenario state disappeared after registration")
            if state["terminal"]:
                return self._result(scenario, state, new_events)

            if self._recover_latched_primary_exit(new_events, scenario, state):
                state = (
                    self.repository.get_state(scenario, self.trigger_version) or state
                )
                return self._result(scenario, state, new_events)

            if now < scenario.observation_window.start_at:
                return self._result(scenario, state, new_events)

            if not state.get("watching"):
                self._record(
                    new_events,
                    scenario,
                    ShadowEventType.WATCHING,
                    event_time=now,
                    market_observation_id=observation_id,
                    role="watching",
                    details=self._details(
                        scenario,
                        {
                            "status": "watching",
                            "observation_window": {
                                "start_at": timestamp_json(
                                    scenario.observation_window.start_at
                                ),
                                "end_at": timestamp_json(
                                    scenario.observation_window.end_at
                                ),
                            },
                        },
                    ),
                    state_updates={"status": "watching", "watching": True},
                )
                state = (
                    self.repository.get_state(scenario, self.trigger_version) or state
                )

            if now > scenario.observation_window.end_at:
                self._expire_if_open(
                    new_events,
                    scenario,
                    state,
                    now,
                    observation_id,
                    reason="window_expired",
                )
                state = (
                    self.repository.get_state(scenario, self.trigger_version) or state
                )
                return self._result(scenario, state, new_events)

            invalidation = evaluate_condition(
                scenario.invalidation_condition,
                normalized_bars,
                scenario.observation_window,
                evaluated_at=now,
            )
            if invalidation.triggered:
                self._record_terminal(
                    new_events,
                    scenario,
                    ShadowEventType.INVALIDATED,
                    now,
                    observation_id,
                    role="invalidated",
                    reason="typed_invalidation_condition",
                    details={"condition": invalidation.to_dict()},
                    status="invalidated",
                )
                state = (
                    self.repository.get_state(scenario, self.trigger_version) or state
                )
                return self._result(scenario, state, new_events)

            triggered = bool(state.get("triggered"))
            if not triggered:
                entry = evaluate_condition(
                    scenario.entry_condition,
                    normalized_bars,
                    scenario.observation_window,
                    evaluated_at=now,
                )
                if not entry.triggered:
                    self._record(
                        new_events,
                        scenario,
                        ShadowEventType.ENTRY_CONDITION_FALSE,
                        event_time=now,
                        market_observation_id=observation_id,
                        role="entry-condition-false:" + observation_id,
                        details=self._details(scenario, {"condition": entry.to_dict()}),
                        state_updates={"status": "watching"},
                    )
                    state = (
                        self.repository.get_state(scenario, self.trigger_version)
                        or state
                    )
                    if now >= scenario.observation_window.end_at:
                        self._expire_if_open(
                            new_events,
                            scenario,
                            state,
                            now,
                            observation_id,
                            reason="entry_not_triggered",
                        )
                        state = (
                            self.repository.get_state(scenario, self.trigger_version)
                            or state
                        )
                    return self._result(scenario, state, new_events)
                self._record(
                    new_events,
                    scenario,
                    ShadowEventType.ENTRY_TRIGGERED,
                    event_time=now,
                    market_observation_id=observation_id,
                    role="entry-triggered",
                    details=self._details(scenario, {"condition": entry.to_dict()}),
                    state_updates={"status": "triggered", "triggered": True},
                )
                state = (
                    self.repository.get_state(scenario, self.trigger_version) or state
                )

            if not state.get("validated"):
                validation = evaluate_condition(
                    scenario.validation_condition,
                    normalized_bars,
                    scenario.observation_window,
                    evaluated_at=now,
                )
                if not validation.triggered:
                    state = (
                        self.repository.get_state(scenario, self.trigger_version)
                        or state
                    )
                    if now >= scenario.observation_window.end_at:
                        self._expire_if_open(
                            new_events,
                            scenario,
                            state,
                            now,
                            observation_id,
                            reason="validation_not_passed",
                        )
                        state = (
                            self.repository.get_state(scenario, self.trigger_version)
                            or state
                        )
                    return self._result(scenario, state, new_events)
                self._record(
                    new_events,
                    scenario,
                    ShadowEventType.VALIDATION_PASSED,
                    event_time=now,
                    market_observation_id=observation_id,
                    role="validation-passed",
                    details=self._details(
                        scenario, {"condition": validation.to_dict()}
                    ),
                    state_updates={"status": "validated", "validated": True},
                )
                state = (
                    self.repository.get_state(scenario, self.trigger_version) or state
                )

            entry_quote = self._entry_quote_from_state(state)
            entry_created = entry_quote is None
            if entry_quote is None:
                entry_quote = single_quote or (
                    normalized_path[0] if normalized_path else None
                )
                entry_quote = entry_quote or self._quote_from_source(scenario, now)
                if entry_quote is None or not self._quote_matches_scenario(
                    entry_quote, scenario, now=now
                ):
                    self._record(
                        new_events,
                        scenario,
                        ShadowEventType.QUOTE_UNAVAILABLE,
                        event_time=now,
                        market_observation_id=observation_id,
                        role="quote-unavailable:" + observation_id,
                        details=self._details(
                            scenario,
                            {"reason": "quote_unavailable_or_contract_mismatch"},
                        ),
                        state_updates={
                            "status": "validated",
                            "quote_status": "unavailable",
                        },
                    )
                    state = (
                        self.repository.get_state(scenario, self.trigger_version)
                        or state
                    )
                    if now >= scenario.observation_window.end_at:
                        self._expire_if_open(
                            new_events,
                            scenario,
                            state,
                            now,
                            observation_id,
                            reason="quote_unavailable_at_expiry",
                        )
                        state = (
                            self.repository.get_state(scenario, self.trigger_version)
                            or state
                        )
                    return self._result(scenario, state, new_events)
                entry_observed_at = entry_quote.quote_time if normalized_path else now
                if (
                    not self.quote_eligibility_policy.eligible(
                        entry_quote, evaluated_at=entry_observed_at
                    )
                    or not entry_quote.is_selected
                ):
                    self._record(
                        new_events,
                        scenario,
                        ShadowEventType.QUOTE_UNAVAILABLE,
                        event_time=now,
                        market_observation_id=observation_id,
                        role="quote-unavailable:" + observation_id,
                        details=self._details(
                            scenario,
                            {
                                "reason": "quote_not_eligible_or_not_preselected",
                                "quote": entry_quote.quote_provenance(),
                            },
                        ),
                        state_updates={
                            "status": "validated",
                            "quote_status": "ineligible",
                        },
                    )
                    state = (
                        self.repository.get_state(scenario, self.trigger_version)
                        or state
                    )
                    return self._result(scenario, state, new_events)
                self._record(
                    new_events,
                    scenario,
                    ShadowEventType.OPTION_SELECTED,
                    event_time=now,
                    market_observation_id=observation_id,
                    role="option-selected",
                    details=self._details(
                        scenario,
                        {
                            "quote": entry_quote.quote_provenance(),
                            "selected_contract": entry_quote.option_symbol,
                        },
                    ),
                    state_updates={
                        "status": "option_selected",
                        "selected_option_symbol": entry_quote.option_symbol,
                    },
                )
                self._record(
                    new_events,
                    scenario,
                    ShadowEventType.SYNTHETIC_ENTRY,
                    event_time=entry_quote.quote_time,
                    market_observation_id=observation_id,
                    role="synthetic-entry",
                    details=self._details(
                        scenario,
                        {
                            "quote": entry_quote.quote_provenance(),
                            "synthetic_entry_mark": entry_quote.mark,
                            "price_basis": "mid_or_last",
                            "not_a_fill": True,
                            "entry_premium_is_corroboration": True,
                        },
                    ),
                    state_updates={
                        "status": "synthetic_entry",
                        "entry_quote": entry_quote.to_dict(),
                        "entry_time": timestamp_json(entry_quote.quote_time),
                        "entry_mark": entry_quote.mark,
                        "selected_option_symbol": entry_quote.option_symbol,
                        "synthetic_entry": True,
                    },
                )
                state = (
                    self.repository.get_state(scenario, self.trigger_version) or state
                )

            exit_quotes = list(normalized_path)
            if entry_created and exit_quotes:
                entry_ids = {entry_quote.snapshot_id}
                exit_quotes = [
                    quote for quote in exit_quotes if quote.snapshot_id not in entry_ids
                ]
            if not exit_quotes and not entry_created and single_quote is not None:
                exit_quotes = [single_quote]
            for quote in exit_quotes:
                if quote.quote_time < entry_quote.quote_time:
                    continue
                if (
                    quote.snapshot_id == entry_quote.snapshot_id
                    and quote.quote_time == entry_quote.quote_time
                ):
                    continue
                if quote.option_symbol != entry_quote.option_symbol:
                    self._record(
                        new_events,
                        scenario,
                        ShadowEventType.QUOTE_UNAVAILABLE,
                        event_time=quote.quote_time,
                        market_observation_id=observation_id,
                        role="quote-unavailable:"
                        + observation_id
                        + ":"
                        + quote.snapshot_id,
                        details=self._details(
                            scenario,
                            {
                                "reason": "selected_contract_changed",
                                "quote": quote.quote_provenance(),
                            },
                        ),
                        state_updates={
                            "status": "synthetic_entry",
                            "quote_status": "contract_mismatch",
                        },
                    )
                    continue
                state = (
                    self.repository.get_state(scenario, self.trigger_version) or state
                )
                profile_states = dict(state.get("profile_states") or {})
                primary_prior = profile_states.get(scenario.exit_profile.value) or {}
                if primary_prior.get("terminal"):
                    self._recover_latched_primary_exit(new_events, scenario, state)
                    state = (
                        self.repository.get_state(scenario, self.trigger_version)
                        or state
                    )
                    break
                observations: list[tuple[ExitProfile, ExitObservation]] = []
                for profile in scenario.compatible_exit_profiles:
                    prior = profile_states.get(profile.value) or {}
                    if prior.get("terminal"):
                        continue
                    result = evaluate_exit_profile(
                        profile,
                        entry_quote,
                        quote,
                        entry_time=entry_quote.quote_time,
                        evaluated_at=quote.quote_time,
                        management_policy=self.exit_policy_registry[profile],
                        cost_model=self.cost_model,
                        quote_eligibility_policy=self.quote_eligibility_policy,
                        prior_state=prior,
                    )
                    next_profile_state = dict(result.state)
                    if result.is_terminal:
                        next_profile_state["terminal"] = True
                        next_profile_state["terminal_rule"] = result.rule
                        next_profile_state["terminal_at"] = timestamp_json(
                            quote.quote_time
                        )
                        next_profile_state["terminal_observation"] = result.to_dict()
                        next_profile_state["terminal_quote_hash"] = quote.snapshot_hash
                        next_profile_state["market_fact_proof"] = dict(
                            self.market_fact_proof or {}
                        )
                    profile_states[profile.value] = next_profile_state
                    observations.append((profile, result))
                for profile, result in observations:
                    primary = profile is scenario.exit_profile
                    self._record(
                        new_events,
                        scenario,
                        ShadowEventType.EXIT_OBSERVATION,
                        event_time=quote.quote_time,
                        market_observation_id=observation_id,
                        role=f"exit-observation:{profile.value}:{quote.snapshot_id}",
                        details=self._details(
                            scenario,
                            {
                                "profile": profile.value,
                                "primary": primary,
                                "counterfactual": not primary,
                                "mark_not_fill": True,
                                "evaluated_exit_policy_id": result.state[
                                    "exit_policy_id"
                                ],
                                "evaluated_exit_policy_schema_version": result.state[
                                    "exit_policy_schema_version"
                                ],
                                "evaluated_exit_policy_hash": result.state[
                                    "exit_policy_hash"
                                ],
                                "quote": quote.quote_provenance(),
                                "observation": result.to_dict(),
                            },
                        ),
                        state_updates={
                            "status": "exit_observing",
                            "profile_states": profile_states,
                        },
                    )
                primary_result = (
                    next(
                        result
                        for profile, result in observations
                        if profile is scenario.exit_profile
                    )
                    if any(
                        profile is scenario.exit_profile for profile, _ in observations
                    )
                    else None
                )
                if primary_result is None:
                    continue
                if primary_result.is_terminal:
                    self._record_terminal(
                        new_events,
                        scenario,
                        ShadowEventType.SYNTHETIC_EXIT,
                        quote.quote_time,
                        observation_id,
                        role="synthetic-exit",
                        reason=primary_result.reason,
                        details={
                            "profile": scenario.exit_profile.value,
                            "primary": True,
                            "counterfactual": False,
                            "mark_not_fill": True,
                            "synthetic_exit_mark": primary_result.mark,
                            "primary_gross_r": primary_result.gross_r,
                            "primary_net_r": primary_result.net_r,
                            "rule": primary_result.rule,
                            "quote": quote.quote_provenance(),
                        },
                        status="synthetic_exit",
                        state_updates={
                            "terminal_profile": scenario.exit_profile.value,
                            "terminal_quote_hash": quote.snapshot_hash,
                            "primary_gross_r": primary_result.gross_r,
                            "primary_net_r": primary_result.net_r,
                            "terminal_reason": primary_result.reason,
                            "profile_states": profile_states,
                        },
                    )
                    state = (
                        self.repository.get_state(scenario, self.trigger_version)
                        or state
                    )
                    break
            else:
                state = (
                    self.repository.get_state(scenario, self.trigger_version) or state
                )

            state = self.repository.get_state(scenario, self.trigger_version) or state
            if now >= scenario.observation_window.end_at and not state["terminal"]:
                self._expire_if_open(
                    new_events,
                    scenario,
                    state,
                    now,
                    observation_id,
                    reason="primary_profile_not_terminal",
                )
                state = (
                    self.repository.get_state(scenario, self.trigger_version) or state
                )
            return self._result(scenario, state, new_events)
        except TerminalScenarioError:
            state = self.repository.get_state(scenario, self.trigger_version) or {
                "status": "terminal",
                "terminal": True,
            }
            return self._result(scenario, state, new_events)
        except Exception as exc:  # noqa: BLE001 - fail closed and persist a terminal receipt
            state = self.repository.get_state(scenario, self.trigger_version)
            if state is not None and not state["terminal"]:
                try:
                    self._record_terminal(
                        new_events,
                        scenario,
                        ShadowEventType.RUNTIME_ERROR,
                        now,
                        observation_id,
                        role="runtime-error:" + observation_id,
                        reason="observer_exception",
                        details={"error_type": type(exc).__name__, "error": str(exc)},
                        status="runtime_error",
                    )
                    state = (
                        self.repository.get_state(scenario, self.trigger_version)
                        or state
                    )
                except Exception:  # noqa: BLE001,S110 - preserve the original observer failure
                    pass
            return ObservationResult(
                scenario_id=scenario.scenario_id,
                status=(state or {}).get("status", "runtime_error"),
                terminal=bool((state or {}).get("terminal", False)),
                new_events=tuple(new_events),
                broker_effect_count=0,
                error=str(exc),
            )

    def _record(
        self,
        events: list[Any],
        scenario: ChartScenarioSpec,
        event_type: ShadowEventType,
        *,
        event_time: datetime,
        market_observation_id: str,
        role: str,
        details: Mapping[str, Any],
        state_updates: Mapping[str, Any] | None = None,
        terminal: bool = False,
    ) -> EventWrite:
        write = self.repository.append_event(
            scenario=scenario,
            trigger_version=self.trigger_version,
            event_type=event_type,
            event_time=event_time,
            market_observation_id=market_observation_id,
            details=details,
            role=role,
            state_updates=state_updates,
            terminal=terminal,
        )
        if write.created:
            events.append(write.event)
        return write

    def _record_terminal(
        self,
        events: list[Any],
        scenario: ChartScenarioSpec,
        event_type: ShadowEventType,
        event_time: datetime,
        market_observation_id: str,
        *,
        role: str,
        reason: str,
        details: Mapping[str, Any],
        status: str,
        state_updates: Mapping[str, Any] | None = None,
    ) -> EventWrite:
        updates = {"status": status, "terminal": True, "terminal_reason": reason}
        if state_updates:
            updates.update(state_updates)
        return self._record(
            events,
            scenario,
            event_type,
            event_time=event_time,
            market_observation_id=market_observation_id,
            role=role,
            details=self._details(
                scenario, {"reason": reason, **details, "terminal": True}
            ),
            state_updates=updates,
            terminal=True,
        )

    def _recover_latched_primary_exit(
        self,
        events: list[Any],
        scenario: ChartScenarioSpec,
        state: Mapping[str, Any],
    ) -> bool:
        """Materialize a primary exit already latched before a process death."""

        profile_states = state.get("profile_states")
        if not isinstance(profile_states, Mapping):
            return False
        primary = profile_states.get(scenario.exit_profile.value)
        if not isinstance(primary, Mapping) or not primary.get("terminal"):
            return False
        observation = primary.get("terminal_observation")
        if not isinstance(observation, Mapping):
            raise TypeError("latched primary exit is missing terminal observation")
        gross_r = observation.get("gross_r")
        net_r = observation.get("net_r")
        quote_hash = primary.get("terminal_quote_hash")
        if (
            isinstance(gross_r, bool)
            or not isinstance(gross_r, (int, float))
            or isinstance(net_r, bool)
            or not isinstance(net_r, (int, float))
            or not isinstance(quote_hash, str)
            or len(quote_hash.removeprefix("sha256:")) != 64
        ):
            raise ValueError("latched primary exit lacks canonical terminal fields")
        terminal_at = as_utc(primary.get("terminal_at"))
        prior_proof = primary.get("market_fact_proof")
        if not isinstance(prior_proof, Mapping):
            raise TypeError("latched primary exit lacks its observation-slot proof")
        slot_ordinal = prior_proof.get("slot_ordinal")
        persisted_slot_id = prior_proof.get("slot_id")
        if (
            isinstance(slot_ordinal, bool)
            or not isinstance(slot_ordinal, int)
            or persisted_slot_id
            != canonical_observation_slot_id(
                run_manifest_hash=self.run_manifest_hash,
                ordinal=slot_ordinal,
            )
            or str(prior_proof.get("plan_hash", "")).removeprefix("sha256:")
            != self.plan_hash
            or str(prior_proof.get("run_manifest_hash", "")).removeprefix("sha256:")
            != self.run_manifest_hash
            or str(prior_proof.get("treatment_manifest_hash", "")).removeprefix(
                "sha256:"
            )
            != self.treatment_manifest_hash
            or prior_proof.get("candidate_id") != scenario.candidate_id
        ):
            raise ValueError("latched primary exit observation-slot proof is invalid")
        self.market_fact_proof = dict(prior_proof)
        reason = str(observation.get("reason") or "recovered_latched_primary_exit")
        self._record_terminal(
            events,
            scenario,
            ShadowEventType.SYNTHETIC_EXIT,
            terminal_at,
            str(persisted_slot_id),
            role="synthetic-exit",
            reason=reason,
            details={
                "profile": scenario.exit_profile.value,
                "primary": True,
                "counterfactual": False,
                "mark_not_fill": True,
                "synthetic_exit_mark": observation.get("mark"),
                "primary_gross_r": float(gross_r),
                "primary_net_r": float(net_r),
                "rule": primary.get("terminal_rule"),
                "observation": dict(observation),
                "recovered_after_restart": True,
            },
            status="synthetic_exit",
            state_updates={
                "terminal_profile": scenario.exit_profile.value,
                "terminal_quote_hash": quote_hash.removeprefix("sha256:"),
                "primary_gross_r": float(gross_r),
                "primary_net_r": float(net_r),
                "terminal_reason": reason,
                "profile_states": dict(profile_states),
            },
        )
        return True

    def _expire_if_open(
        self,
        events: list[Any],
        scenario: ChartScenarioSpec,
        state: Mapping[str, Any],
        now: datetime,
        observation_id: str,
        *,
        reason: str,
    ) -> None:
        if not state.get("terminal"):
            self._record_terminal(
                events,
                scenario,
                ShadowEventType.EXPIRED,
                now,
                observation_id,
                role="expired",
                reason=reason,
                details={
                    "observation_window_end": timestamp_json(
                        scenario.observation_window.end_at
                    )
                },
                status="expired",
            )

    @staticmethod
    def _coerce_quote(value: Mapping[str, Any]) -> OptionQuoteSnapshot:
        return OptionQuoteSnapshot.from_mapping(value)

    def _quote_from_source(
        self, scenario: ChartScenarioSpec, at: datetime
    ) -> OptionQuoteSnapshot | None:
        if not self.quote_snapshots:
            return None
        return select_snapshot(self.quote_snapshots, scenario=scenario, at=at)

    @staticmethod
    def _quote_matches_scenario(
        quote: OptionQuoteSnapshot,
        scenario: ChartScenarioSpec,
        *,
        now: datetime | None = None,
    ) -> bool:
        if quote.underlying_symbol != scenario.symbol or not quote.option_symbol:
            return False
        expected_contract_type = "CALL" if scenario.direction.value == "long" else "PUT"
        if quote.contract_type != expected_contract_type:
            return False
        if quote.scenario_id is not None and quote.scenario_id != scenario.scenario_id:
            return False
        if quote.quote_time < scenario.observation_window.start_at:
            return False
        return not (now is not None and quote.quote_time > now)

    @staticmethod
    def _entry_quote_from_state(state: Mapping[str, Any]) -> OptionQuoteSnapshot | None:
        raw = state.get("entry_quote")
        if not isinstance(raw, Mapping):
            return None
        try:
            return OptionQuoteSnapshot.from_mapping(raw)
        except Exception:  # noqa: BLE001 - malformed persisted state is treated as absent
            return None

    def _validate_scenario_policies(self, scenario: ChartScenarioSpec) -> None:
        missing = set(scenario.compatible_exit_profiles) - set(
            self.exit_policy_registry
        )
        if missing:
            raise ValueError(
                "observer exit policy registry is missing profiles: "
                + ", ".join(sorted(profile.value for profile in missing))
            )
        selected = self.exit_policy_registry[scenario.exit_profile]
        if scenario.management_policy is None:
            raise ValueError("scenario selected management_policy is missing")
        if (
            selected.canonical_policy_json
            != scenario.management_policy.canonical_policy_json
        ):
            raise ValueError(
                "observer selected policy differs from scenario management_policy"
            )
        if selected.policy_hash != scenario.exit_policy_hash:
            raise ValueError(
                "observer selected policy hash differs from scenario exit_policy_hash"
            )
        if scenario.cost_model_hash != self.cost_model.content_hash:
            raise ValueError(
                "observer cost model differs from scenario cost_model_hash"
            )
        if (
            scenario.quote_eligibility_policy_hash
            != self.quote_eligibility_policy.content_hash
        ):
            raise ValueError(
                "observer quote policy differs from scenario quote_eligibility_policy_hash"
            )

    def _details(
        self, scenario: ChartScenarioSpec, details: Mapping[str, Any]
    ) -> dict[str, Any]:
        payload = {
            "broker_effect_count": 0,
            "identity": {
                "program_id": scenario.program_id,
                "experiment_family_id": scenario.experiment_family_id,
                "experiment_version": scenario.experiment_version,
                "campaign_id": scenario.campaign_id,
                "run_id": scenario.run_id,
                "arm_id": scenario.arm_id.value,
                "scenario_id": scenario.scenario_id,
                "candidate_id": scenario.candidate_id,
            },
            "scenario_coordinates": {
                "symbol": scenario.symbol,
                "direction": scenario.direction.value,
                "thesis_class": scenario.thesis_class.value,
            },
            "trigger_version": TRIGGER_VERSION,
            "plan_hash": self.plan_hash,
            "run_manifest_hash": self.run_manifest_hash,
            "treatment_manifest_hash": self.treatment_manifest_hash,
            "run_input_hashes_hash": self.run_input_hashes_hash,
            "cartographer_receipt_hash": self.cartographer_receipt_hash,
            "cartographer_export_hash": self.cartographer_export_hash,
            "observation_window_hash": self.observation_window_hash,
            "policy_registry_hash": self.policy_registry_hash,
            "cost_model_hash": self.cost_model.content_hash,
            "quote_eligibility_policy_hash": self.quote_eligibility_policy.content_hash,
            "market_facts_hash": self.market_facts_hash,
            "market_fact_proof": dict(self.market_fact_proof or {}),
            "scenario_hash": scenario.scenario_hash,
            "component_manifest_hash": scenario.component_manifest_hash,
            "candidate_pool_hash": scenario.candidate_pool_hash,
            "selection_packet_hash": scenario.selection_packet_hash,
            "candidate_hash": scenario.candidate_hash,
            "chart_evidence_hashes": [
                item.evidence_hash for item in scenario.chart_evidence_refs
            ],
            "exit_policy_hash": scenario.exit_policy_hash,
            "component_hashes": {
                "component_manifest": scenario.component_manifest_hash,
                "candidate_pool": scenario.candidate_pool_hash,
                "selection_packet": scenario.selection_packet_hash,
                "scenario": scenario.scenario_hash,
                "exit_policy": scenario.exit_policy_hash,
                "chart_evidence": [
                    item.evidence_hash for item in scenario.chart_evidence_refs
                ],
            },
            "authorization_mode": scenario.authorization_mode.value,
            "source_type": scenario.source_type.value,
        }
        payload.update(details)
        return payload

    def _result(
        self,
        scenario: ChartScenarioSpec,
        state: Mapping[str, Any],
        events: Sequence[Any],
    ) -> ObservationResult:
        return ObservationResult(
            scenario_id=scenario.scenario_id,
            status=str(state.get("status", "unknown")),
            terminal=bool(state.get("terminal", False)),
            new_events=tuple(events),
            broker_effect_count=0,
        )


ChartScenarioObserver = BrokerInertScenarioObserver
ChartScenarioShadowObserver = BrokerInertScenarioObserver


__all__ = [
    "BrokerInertScenarioObserver",
    "ChartScenarioObserver",
    "ChartScenarioShadowObserver",
    "ObservationResult",
]
