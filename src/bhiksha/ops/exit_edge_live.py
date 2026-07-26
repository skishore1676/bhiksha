"""Nonblocking live recorder for the prospective Exit Edge Lab.

The recorder is an observational sidecar.  The runtime thread only performs
``Queue.put_nowait`` plus tiny in-memory bookkeeping; SQLite and report writes
run on a daemon worker.  Quotes arrive exclusively from the existing
``OrderManager.get_option_quote`` result observer, so enabling this module does
not create a quote poller or consume additional broker quota.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from typing import Any, Callable
from zoneinfo import ZoneInfo

from bhiksha.execution.profile_exit import ProfileExitFields
from bhiksha.ops.exit_edge_lab import (
    EVALUATOR_VERSION,
    FILL_MODEL_VERSION,
    ProspectiveQuoteTapeRepository,
    QuoteTapeMark,
    ShadowEnvelopeState,
    analyze_cases,
    build_risk_envelope_experiment,
    experiment_spec_hash,
)

ET = ZoneInfo("America/New_York")
QUOTE_SOURCE = "public_api"
QUOTE_FEED = "order_manager_reused_quote_v1"


@dataclass(frozen=True, slots=True)
class _Register:
    attempt: dict[str, Any]
    payload: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class _ObservedQuote:
    option_symbol: str
    quote_at: datetime | None
    quote_timestamp_field: str | None
    received_at: datetime
    bid: float | None
    ask: float | None
    last: float | None


class ExitEdgeLiveRecorder:
    """Bounded, best-effort bridge from live facts to the lab repository."""

    def __init__(
        self,
        *,
        db_path: str | Path,
        status_path: str | Path,
        queue_capacity: int = 512,
        fill_latency_ms: int = 0,
        max_freshness_ms: int = 2_000,
        max_sequence_gap: int = 1,
        repository_factory: Callable[[str | Path], ProspectiveQuoteTapeRepository]
        | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.status_path = Path(status_path)
        self.fill_latency_ms = int(fill_latency_ms)
        self.max_freshness_ms = int(max_freshness_ms)
        self.max_sequence_gap = int(max_sequence_gap)
        self._queue: Queue[_Register | _ObservedQuote] = Queue(
            maxsize=max(int(queue_capacity), 1)
        )
        self._repository_factory = repository_factory or ProspectiveQuoteTapeRepository
        self._stop_after_drain = Event()
        self._lock = Lock()
        self._active_by_option: dict[str, set[str]] = {}
        self._pending_censors: dict[str, str] = {}
        self._pending_registration_attempts: dict[str, dict[str, Any]] = {}
        self._health: dict[str, Any] = {
            "schema_version": 2,
            "enabled": True,
            "mode": "observational_shadow_only",
            "enforcement_authority": False,
            "promotion_eligible": False,
            "inference_eligible": False,
            "inference_blockers": ["guarded_repository_report_required"],
            "broker_calls_added": 0,
            "post_exit_quote_continuation": (
                "not_enabled_protection_priority_unproved"
            ),
            "quote_source": QUOTE_SOURCE,
            "quote_feed": QUOTE_FEED,
            "observed_quote_timestamp_fields": {},
            "db_path": str(self.db_path),
            "queue_capacity": self._queue.maxsize,
            "queued": 0,
            "cohorts_registered": 0,
            "confirmed_fill_attempts": 0,
            "ineligible_fill_attempts": 0,
            "missing_registration_attempts": 0,
            "cohorts_recovered": 0,
            "active_cohorts": 0,
            "paired_cohorts": 0,
            "censored_cohorts": 0,
            "dropped_observations": 0,
            "storage_failures": 0,
            "registration_failures": 0,
            "last_error": None,
            "worker_alive": False,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        self._thread = Thread(
            target=self._run,
            name="bhiksha-exit-edge-recorder",
            daemon=True,
        )

    def start(self) -> None:
        """Start the persistence worker; safe to call once per runtime."""
        if self._thread.is_alive():
            return
        self._thread.start()

    def try_register_entry(
        self,
        *,
        deployment: Any,
        trade_id: str,
        option_symbol: str,
        entry_timestamp: datetime | None,
        entry_premium: float | None,
        quantity: int | None,
        entry_context: dict[str, Any] | None = None,
    ) -> bool:
        """Freeze actual entry identity and both policies without waiting on I/O."""
        try:
            attempt, payload = self._registration_payloads(
                deployment=deployment, trade_id=trade_id,
                option_symbol=option_symbol, entry_timestamp=entry_timestamp,
                entry_premium=entry_premium, quantity=quantity,
                entry_context=entry_context,
            )
            self._increment_health("confirmed_fill_attempts")
            if payload is None:
                self._increment_health("ineligible_fill_attempts")
            self._queue.put_nowait(_Register(attempt, payload))
            if payload is not None:
                # Make queue-overflow censoring race-free: the identity is known
                # before the persistence worker activates the cohort.
                self._activate(str(payload["option_symbol"]), str(payload["cohort_id"]))
            self._set_health(queued=self._queue.qsize())
            return payload is not None
        except Full:
            attempt["outcome"] = "registration_queue_full"
            attempt["reason"] = "cohort_registration_queue_full"
            with self._lock:
                self._pending_registration_attempts[str(trade_id)] = attempt
            self._increment_health("missing_registration_attempts")
            self._record_drop(option_symbol, "cohort_registration_queue_full")
            return False
        except Exception as exc:
            self._increment_health("registration_failures", error=f"registration_payload:{exc}")
            return False

    def observe_quote(self, option_symbol: str, quote: Any, received_at: datetime) -> None:
        """Enqueue a completed existing quote fetch; never raise or wait."""
        try:
            timestamp_field = getattr(quote, "quote_timestamp_field", None)
            with self._lock:
                fields = self._health["observed_quote_timestamp_fields"]
                key = str(timestamp_field or "missing")
                fields[key] = int(fields.get(key, 0)) + 1
            observed = _ObservedQuote(
                option_symbol=_normalize_option_symbol(option_symbol),
                quote_at=_parse_provider_timestamp(getattr(quote, "quote_timestamp", None)),
                quote_timestamp_field=timestamp_field,
                received_at=_aware_utc(received_at),
                bid=_maybe_float(getattr(quote, "bid", None)),
                ask=_maybe_float(getattr(quote, "ask", None)),
                last=_maybe_float(getattr(quote, "last", None)),
            )
            self._queue.put_nowait(observed)
            self._set_health(queued=self._queue.qsize())
        except Full:
            self._record_drop(option_symbol, "quote_queue_full")
        except Exception as exc:
            self._record_drop(option_symbol, f"quote_observer_error:{type(exc).__name__}")

    def close(self, *, join_timeout_seconds: float = 1.0) -> None:
        """Drain queued facts and censor unfinished cohorts at session shutdown."""
        self._stop_after_drain.set()
        if self._thread.is_alive():
            self._thread.join(timeout=max(float(join_timeout_seconds), 0.0))
        self._set_health(worker_alive=self._thread.is_alive())
        self._write_status_best_effort()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            snapshot = dict(self._health)
            snapshot["observed_quote_timestamp_fields"] = dict(
                self._health["observed_quote_timestamp_fields"]
            )
            snapshot["inference_blockers"] = list(self._health["inference_blockers"])
            return snapshot

    def _run(self) -> None:
        repository = self._repository_factory(self.db_path)
        self._set_health(worker_alive=True)
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            if not repository.try_initialize():
                self._increment_health("storage_failures", error="repository_initialize_failed")
                return
            denominator = repository.registration_summary()
            current_health = self.snapshot()
            self._set_health(
                confirmed_fill_attempts=max(
                    int(current_health["confirmed_fill_attempts"]),
                    denominator["confirmed_fill_attempts"],
                ),
                missing_registration_attempts=max(
                    int(current_health["missing_registration_attempts"]),
                    denominator["missing_or_ineligible_registrations"],
                ),
            )
            self._recover(repository)
            while True:
                try:
                    item = self._queue.get(timeout=0.05)
                except Empty:
                    self._flush_pending_registration_attempts(repository)
                    self._flush_pending_censors(repository)
                    if self._stop_after_drain.is_set():
                        break
                    continue
                try:
                    if isinstance(item, _Register):
                        self._persist_registration(repository, item.attempt, item.payload)
                    else:
                        self._persist_quote(repository, item)
                finally:
                    self._queue.task_done()
                    self._set_health(queued=self._queue.qsize())
                self._flush_pending_registration_attempts(repository)
                self._flush_pending_censors(repository)
            self._flush_pending_registration_attempts(repository)
            self._flush_pending_censors(repository)
            self._censor_all_active(
                repository,
                "no_post_exit_quote_source_session_shutdown_before_virtual_arms_terminal",
            )
        except Exception as exc:  # the sidecar must never take down trading
            self._increment_health("storage_failures", error=f"worker_crash:{type(exc).__name__}:{exc}")
        finally:
            self._set_health(worker_alive=False)
            self._write_status_best_effort()

    def _recover(self, repository: ProspectiveQuoteTapeRepository) -> None:
        for cohort_id in repository.list_cohort_ids():
            try:
                case = repository.load_case(cohort_id)
                row = analyze_cases([case])["cases"][0]
                states = tuple(
                    ShadowEnvelopeState(**state)
                    for state in row.get("shadow_envelope_states", [])
                )
                if states:
                    repository.persist_shadow_envelope_states(states)
                if row["status"] == "paired":
                    self._increment_health("paired_cohorts")
                    continue
                if case.persisted_censor_reason:
                    self._increment_health("censored_cohorts")
                    continue
                # A process restart creates an unobserved interval in the tape.
                # Never silently resume with a sequence-contiguous fiction.
                self._censor(repository, cohort_id, "restart_gap_unobserved_quotes")
                self._increment_health("cohorts_recovered")
            except Exception as exc:
                self._increment_health("storage_failures", error=f"recovery:{cohort_id}:{exc}")
        self._write_status_best_effort()

    def _persist_registration(
        self, repository: ProspectiveQuoteTapeRepository,
        attempt: dict[str, Any], payload: dict[str, Any] | None,
    ) -> None:
        if payload is None:
            if not repository.try_record_registration_attempt(attempt):
                self._retain_registration_attempt(attempt)
                self._increment_health("storage_failures", error="ineligible_attempt_persist_failed")
            self._write_status_best_effort()
            return
        cohort_id = str(payload["cohort_id"])
        if not repository.try_register_cohort(payload):
            attempt["outcome"] = "registration_persistence_failure"
            attempt["reason"] = "cohort_repository_rejected"
            if not repository.try_record_registration_attempt(attempt):
                self._retain_registration_attempt(attempt)
            self._deactivate(str(payload["option_symbol"]), cohort_id)
            self._increment_health("registration_failures", error=f"register_failed:{cohort_id}")
            return
        attempt["outcome"] = "registered"
        attempt["reason"] = None
        if not repository.try_record_registration_attempt(attempt):
            self._retain_registration_attempt(attempt)
            self._increment_health("storage_failures", error=f"attempt_persist_failed:{cohort_id}")
        self._activate(str(payload["option_symbol"]), cohort_id)
        self._increment_health("cohorts_registered")
        self._write_status_best_effort()

    def _persist_quote(
        self, repository: ProspectiveQuoteTapeRepository, observed: _ObservedQuote
    ) -> None:
        cohort_ids = self._active_for(observed.option_symbol)
        for cohort_id in cohort_ids:
            if observed.quote_timestamp_field != "quoteTimestamp":
                self._censor(
                    repository,
                    cohort_id,
                    "unproven_bid_ask_quote_timestamp_lineage",
                )
                continue
            if observed.quote_at is None:
                self._censor(repository, cohort_id, "missing_provider_quote_timestamp")
                continue
            try:
                sequence = repository.latest_sequence(cohort_id) + 1
                mark = QuoteTapeMark(
                    sequence=sequence,
                    source=QUOTE_SOURCE,
                    feed=QUOTE_FEED,
                    quote_at=observed.quote_at,
                    received_at=observed.received_at,
                    bid=observed.bid,
                    ask=observed.ask,
                    last=observed.last,
                )
                if not repository.try_append_quote(cohort_id, mark):
                    self._increment_health("storage_failures", error=f"append_failed:{cohort_id}")
                    self._censor(repository, cohort_id, "quote_persistence_failure")
                    continue
                case = repository.load_case(cohort_id)
                row = analyze_cases([case])["cases"][0]
                states = tuple(
                    ShadowEnvelopeState(**state)
                    for state in row.get("shadow_envelope_states", [])
                )
                if states:
                    repository.persist_shadow_envelope_states(states)
                if row["status"] == "paired":
                    self._deactivate(case.option_symbol, cohort_id)
                    self._increment_health("paired_cohorts")
                else:
                    reason = str(row.get("insufficient_reason") or "")
                    if reason and not (
                        reason == "quote_tape_too_short_for_next_tick_fill"
                        or reason.startswith("right_censored:")
                    ):
                        self._censor(repository, cohort_id, reason)
            except Exception as exc:
                self._increment_health("storage_failures", error=f"quote_processing:{cohort_id}:{exc}")
                self._censor(repository, cohort_id, "quote_processing_failure")
        self._write_status_best_effort()

    def _flush_pending_censors(self, repository: ProspectiveQuoteTapeRepository) -> None:
        with self._lock:
            pending = dict(self._pending_censors)
            self._pending_censors.clear()
        for cohort_id, reason in pending.items():
            self._censor(repository, cohort_id, reason)

    def _flush_pending_registration_attempts(
        self, repository: ProspectiveQuoteTapeRepository
    ) -> None:
        with self._lock:
            pending = dict(self._pending_registration_attempts)
            self._pending_registration_attempts.clear()
        for trade_id, attempt in pending.items():
            if not repository.try_record_registration_attempt(attempt):
                self._retain_registration_attempt(attempt)
                self._increment_health(
                    "storage_failures", error=f"registration_attempt_persist_failed:{trade_id}"
                )

    def _retain_registration_attempt(self, attempt: dict[str, Any]) -> None:
        with self._lock:
            self._pending_registration_attempts.setdefault(str(attempt["trade_id"]), attempt)

    def _censor_all_active(
        self, repository: ProspectiveQuoteTapeRepository, reason: str
    ) -> None:
        with self._lock:
            cohort_ids = {
                cohort_id
                for values in self._active_by_option.values()
                for cohort_id in values
            }
        for cohort_id in cohort_ids:
            self._censor(repository, cohort_id, reason)

    def _censor(
        self, repository: ProspectiveQuoteTapeRepository, cohort_id: str, reason: str
    ) -> bool:
        try:
            case = repository.load_case(cohort_id)
            if case.persisted_censor_reason:
                self._deactivate(case.option_symbol, cohort_id)
                return True
            if repository.try_record_censor(cohort_id, reason):
                self._deactivate(case.option_symbol, cohort_id)
                self._increment_health("censored_cohorts")
                return True
            else:
                self._increment_health("storage_failures", error=f"censor_failed:{cohort_id}:{reason}")
        except Exception as exc:
            self._increment_health("storage_failures", error=f"censor_processing:{cohort_id}:{exc}")
        with self._lock:
            self._pending_censors.setdefault(cohort_id, reason)
        return False

    def _record_drop(self, option_symbol: str | None, reason: str) -> None:
        normalized = _normalize_option_symbol(option_symbol) if option_symbol else None
        with self._lock:
            self._health["dropped_observations"] += 1
            self._health["last_error"] = reason
            self._health["updated_at"] = datetime.now(UTC).isoformat()
            if normalized:
                for cohort_id in self._active_by_option.get(normalized, set()):
                    self._pending_censors.setdefault(cohort_id, reason)

    def _activate(self, option_symbol: str, cohort_id: str) -> None:
        normalized = _normalize_option_symbol(option_symbol)
        with self._lock:
            self._active_by_option.setdefault(normalized, set()).add(cohort_id)
            self._health["active_cohorts"] = sum(
                len(values) for values in self._active_by_option.values()
            )

    def _deactivate(self, option_symbol: str, cohort_id: str) -> None:
        normalized = _normalize_option_symbol(option_symbol)
        with self._lock:
            values = self._active_by_option.get(normalized)
            if values is not None:
                values.discard(cohort_id)
                if not values:
                    self._active_by_option.pop(normalized, None)
            self._health["active_cohorts"] = sum(
                len(active) for active in self._active_by_option.values()
            )

    def _active_for(self, option_symbol: str) -> tuple[str, ...]:
        normalized = _normalize_option_symbol(option_symbol)
        with self._lock:
            return tuple(self._active_by_option.get(normalized, ()))

    def _increment_health(self, key: str, *, error: str | None = None) -> None:
        with self._lock:
            self._health[key] = int(self._health.get(key, 0)) + 1
            if error is not None:
                self._health["last_error"] = error
            self._health["updated_at"] = datetime.now(UTC).isoformat()

    def _set_health(self, **values: Any) -> None:
        with self._lock:
            self._health.update(values)
            self._health["updated_at"] = datetime.now(UTC).isoformat()

    def _write_status_best_effort(self) -> None:
        try:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            payload = self.snapshot()
            temporary = self.status_path.with_suffix(self.status_path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            temporary.replace(self.status_path)
        except OSError as exc:
            self._increment_health("storage_failures", error=f"status_write:{exc}")

    def _registration_payloads(
        self,
        *,
        deployment: Any,
        trade_id: str,
        option_symbol: str,
        entry_timestamp: datetime | None,
        entry_premium: float | None,
        quantity: int | None,
        entry_context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        symbol = str(deployment.symbol).upper()
        profile_id = getattr(deployment.exit, "profile_exit_id", None)
        reasons = []
        if entry_timestamp is None:
            reasons.append("missing_broker_filled_at")
        if entry_premium is None or entry_premium <= 0:
            reasons.append("missing_broker_average_fill_price")
        if quantity is None or quantity <= 0:
            reasons.append("missing_broker_filled_quantity")
        if not profile_id:
            reasons.append("deployment_has_no_frozen_profile")
        control_policy = getattr(deployment.exit, "exit_policy_snapshot", None)
        control_policy_hash = getattr(deployment.exit, "exit_policy_hash", None)
        control_policy_id = getattr(deployment.exit, "exit_policy_id", None)
        if not isinstance(control_policy, dict) or not control_policy:
            reasons.append("missing_canonical_exit_policy_snapshot")
        if not control_policy_hash or not control_policy_id:
            reasons.append("missing_canonical_exit_policy_identity")
        elif isinstance(control_policy, dict) and str(
            control_policy.get("policy_id") or ""
        ) != str(control_policy_id):
            reasons.append("canonical_exit_policy_id_mismatch")
        cohort_id = f"exit-edge:{trade_id}"
        attempt = {
            "trade_id": str(trade_id), "deployment_id": str(deployment.deployment_id),
            "symbol": symbol, "option_symbol": _normalize_option_symbol(option_symbol),
            "observed_at": datetime.now(UTC).isoformat(), "eligible": not reasons,
            "cohort_id": cohort_id if not reasons else None,
            "outcome": "ineligible" if reasons else "queued",
            "reason": ",".join(reasons) if reasons else None,
        }
        if reasons:
            return attempt, None
        entry_at = _aware_utc(entry_timestamp)
        fields = ProfileExitFields.from_exit_spec(deployment.exit)
        profile = asdict(fields)
        profile["profile_exit_id"] = profile.pop("profile_id")
        profile["stop_loss_pct"] = profile.pop("option_stop_fallback_pct")
        profile_to_policy_fields = {
            "target_1_r": "target_1_r",
            "target_2_r": "target_2_r",
            "target_1_quantity": "target_1_quantity",
            "initial_stop_pct": "initial_stop_pct",
            "premium_disaster_stop_pct": "premium_disaster_stop_pct",
            "no_progress_seconds": "no_progress_seconds",
            "max_hold_seconds": "max_hold_seconds",
            "high_water_giveback_policy": "high_water_giveback_policy",
            "explicit_giveback_arm_r": "giveback_arm_r",
            "explicit_giveback_retrace_fraction": "giveback_retrace_fraction",
            "breakeven_after_t1": "breakeven_after_t1",
            "eod_flat": "eod_flat",
            "hard_flat_time_et": "hard_flat_time_et",
        }
        mismatches = [
            profile_key
            for profile_key, policy_key in profile_to_policy_fields.items()
            if profile.get(profile_key) != control_policy.get(policy_key)
        ]
        policy_parameters = control_policy.get("parameters") or {}
        if not isinstance(policy_parameters, dict):
            policy_parameters = {}
        if profile.get("no_progress_favorable_floor_r") != policy_parameters.get(
            "no_progress_favorable_floor_r", 0.25
        ):
            mismatches.append("no_progress_favorable_floor_r")
        if mismatches:
            attempt["eligible"] = False
            attempt["cohort_id"] = None
            attempt["outcome"] = "ineligible"
            attempt["reason"] = (
                "profile_and_canonical_policy_mismatch:"
                + ",".join(sorted(mismatches))
            )
            return attempt, None
        target_pct = None
        if bool(deployment.exit.use_profit_target):
            if deployment.exit.option_profit_target_pct is not None:
                target_pct = float(deployment.exit.option_profit_target_pct)
            elif deployment.exit.profit_target_multiple is not None:
                target_pct = float(deployment.exit.stop_loss_pct) * float(
                    deployment.exit.profit_target_multiple
                )
        legacy = {
            "comparator_version": "bhiksha-native-premium-stop-full-target-eod-v1",
            "stop_loss_pct": float(deployment.exit.stop_loss_pct),
            "profit_target_pct": target_pct,
            "hard_flat_time_et": str(deployment.exit.hard_flat_time_et),
        }
        experiment = {
            "fill_latency_ms": self.fill_latency_ms,
            "max_freshness_ms": self.max_freshness_ms,
            "max_sequence_gap": self.max_sequence_gap,
            "evaluator_version": EVALUATOR_VERSION,
            "fill_model_version": FILL_MODEL_VERSION,
            "quote_source": QUOTE_SOURCE,
            "quote_feed": QUOTE_FEED,
        }
        try:
            experiment["risk_envelope"] = build_risk_envelope_experiment(
                dict(control_policy),
                control_policy_hash=str(control_policy_hash),
            )
        except (KeyError, TypeError, ValueError) as exc:
            attempt["eligible"] = False
            attempt["cohort_id"] = None
            attempt["outcome"] = "ineligible"
            attempt["reason"] = f"invalid_canonical_exit_policy:{exc}"
            return attempt, None
        return attempt, {
            "cohort_id": cohort_id,
            "trade_id": str(trade_id),
            "cluster_id": f"session-et-underlying-v1:{entry_at.astimezone(ET).date()}:{symbol}",
            "deployment_id": str(deployment.deployment_id),
            "symbol": symbol,
            "option_symbol": _normalize_option_symbol(option_symbol),
            "entry_timestamp": entry_at.isoformat(),
            "entry_premium": float(entry_premium),
            "quantity": int(quantity),
            "cohort_dimensions": {
                "selected_dte": (entry_context or {}).get("selected_dte"),
                "selected_abs_delta": (entry_context or {}).get(
                    "selected_abs_delta"
                ),
                "entry_spread_pct": (entry_context or {}).get(
                    "selected_spread_pct"
                ),
                "dte_fallback_policy": (entry_context or {}).get(
                    "dte_fallback_policy"
                )
                or getattr(
                    getattr(deployment, "execution", None),
                    "dte_fallback_policy",
                    None,
                ),
                "configured_dte_min": getattr(
                    getattr(deployment, "execution", None), "dte_min", None
                ),
                "configured_dte_max": getattr(
                    getattr(deployment, "execution", None), "dte_max", None
                ),
                "strategy_policy_hash": str(control_policy_hash),
                "runtime_mode": getattr(
                    getattr(deployment, "execution", None),
                    "runtime_mode",
                    None,
                ),
                "authorization_mode": (
                    "shadow"
                    if bool(
                        getattr(
                            getattr(deployment, "execution", None),
                            "shadow_only",
                            False,
                        )
                    )
                    else "live"
                ),
                "authorization_id": getattr(
                    deployment.exit,
                    "risk_envelope_live_authorization_id",
                    None,
                ),
            },
            "profile": profile,
            "legacy": legacy,
            "experiment": experiment,
            "experiment_spec_hash": experiment_spec_hash(profile, legacy, experiment),
        }


def _parse_provider_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _aware_utc(value)
    raw = str(value).strip()
    if not raw:
        return None
    try:
        numeric = float(raw)
    except ValueError:
        numeric = None
    if numeric is not None:
        if numeric > 10_000_000_000:
            numeric /= 1_000.0
        return datetime.fromtimestamp(numeric, tz=UTC)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware_utc(parsed)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _maybe_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _normalize_option_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper().replace(" ", "")
    return symbol[:-7] if symbol.endswith("-OPTION") else symbol


__all__ = ["ExitEdgeLiveRecorder", "QUOTE_FEED", "QUOTE_SOURCE"]
