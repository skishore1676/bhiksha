"""Read-only experiment status for Sheet-configured Bhiksha deployments.

The Google Sheet and the compiled active plan remain the authority for row
identity, configuration, and stage.  This module only joins those facts with
already-persisted Bhiksha observations.  It deliberately does not import the
runtime bootstrap, a broker adapter, a Sheet client, or any report writer.

The resulting envelope is the small cross-application boundary consumed by
TradeLab:: ``tradelab.app_experiment_status.v1``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from contextlib import closing
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from bhiksha.ops.shadow_evidence import build_shadow_evidence

STATUS_SCHEMA = "tradelab.app_experiment_status.v1"
SOURCE_STATUSES = frozenset({"ok", "partial", "stale", "unavailable"})
HEALTH_STATES = frozenset({"collecting", "inconclusive", "ready_for_review"})
EFFECT_KEYS = ("sheet_write", "stage_change", "broker_action", "order_action")
STATUS_EVENT_TYPES = (
    "shadow_entry_assumed",
    "shadow_mark",
    "shadow_exit_assumed",
    "signal_decision",
    "trade_plan",
)


class ExperimentStatusError(ValueError):
    """Raised when a status envelope cannot satisfy the shared contract."""


def canonical_hash(value: Any) -> str:
    """Return a stable identity for a JSON-compatible configuration payload."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def build_app_experiment_status(
    active_plan: Mapping[str, Any] | Any,
    *,
    facts_by_deployment: Mapping[str, Mapping[str, Any]] | None = None,
    source_status: str = "ok",
    as_of: str | datetime | None = None,
) -> dict[str, Any]:
    """Build a status envelope from an active plan and factual app inputs.

    ``facts_by_deployment`` is intentionally a mapping of facts rather than a
    second experiment configuration.  A missing deployment entry is a normal
    no-data state and still emits the Sheet-derived deployment identity.
    """

    plan = _as_mapping(active_plan)
    if source_status not in SOURCE_STATUSES:
        raise ExperimentStatusError(f"unsupported source_status: {source_status}")
    resolved_as_of = _iso_timestamp(as_of) or _iso_timestamp(
        plan.get("generated_at")
    )
    if not resolved_as_of:
        resolved_as_of = datetime.now(UTC).isoformat()

    facts = facts_by_deployment or {}
    experiments: list[dict[str, Any]] = []
    for raw_deployment in plan.get("deployments") or []:
        deployment = _as_mapping(raw_deployment)
        if deployment.get("enabled", True) is False:
            continue
        deployment_id = str(deployment.get("deployment_id") or "").strip()
        if not deployment_id:
            raise ExperimentStatusError("active plan deployment has no deployment_id")
        raw_facts = facts.get(deployment_id) or {}
        experiments.append(
            _experiment_row(
                deployment,
                raw_facts,
                source_status=source_status,
                as_of=resolved_as_of,
                plan=plan,
            )
        )

    envelope = {
        "schema": STATUS_SCHEMA,
        "app": "bhiksha",
        "as_of": resolved_as_of,
        "source_status": source_status,
        "active_plan_id": plan.get("active_plan_id"),
        "plan_revision_id": plan.get("plan_revision_id"),
        "experiments": experiments,
        "effects": {key: False for key in EFFECT_KEYS},
    }
    validate_app_experiment_status(envelope)
    return envelope


def validate_app_experiment_status(payload: Mapping[str, Any]) -> None:
    """Fail closed on malformed status or any claimed side effect."""

    if not isinstance(payload, Mapping):
        raise ExperimentStatusError("status envelope must be an object")
    if payload.get("schema") != STATUS_SCHEMA:
        raise ExperimentStatusError("invalid app experiment status schema")
    if payload.get("app") != "bhiksha":
        raise ExperimentStatusError("status envelope app must be bhiksha")
    if not isinstance(payload.get("as_of"), str) or not payload["as_of"].strip():
        raise ExperimentStatusError("status envelope requires as_of")
    if payload.get("source_status") not in SOURCE_STATUSES:
        raise ExperimentStatusError("status envelope has invalid source_status")
    experiments = payload.get("experiments")
    if not isinstance(experiments, list):
        raise ExperimentStatusError("status envelope experiments must be a list")
    effects = payload.get("effects")
    if not isinstance(effects, Mapping):
        raise ExperimentStatusError("status envelope requires effects")
    for key in EFFECT_KEYS:
        if effects.get(key) is not False:
            raise ExperimentStatusError(f"status envelope effect is not false: {key}")

    for experiment in experiments:
        if not isinstance(experiment, Mapping):
            raise ExperimentStatusError("experiment entry must be an object")
        for key in (
            "experiment_id",
            "stage",
            "configuration_identity",
            "observation_window",
            "observations",
            "opportunities",
            "entries",
            "closed",
            "metrics",
            "health",
            "limitations",
        ):
            if key not in experiment:
                raise ExperimentStatusError(f"experiment entry is missing {key}")
        if not str(experiment["experiment_id"]).strip():
            raise ExperimentStatusError("experiment_id must be non-empty")
        if not str(experiment["stage"]).strip():
            raise ExperimentStatusError("experiment stage must be non-empty")
        if not str(experiment["configuration_identity"]).strip():
            raise ExperimentStatusError("configuration_identity must be non-empty")
        window = experiment["observation_window"]
        if (
            not isinstance(window, Mapping)
            or not {"start", "end"} <= set(window)
            or not isinstance(window["start"], str)
            or not isinstance(window["end"], str)
        ):
            raise ExperimentStatusError("observation_window requires start and end")
        for key in ("observations", "opportunities", "entries", "closed"):
            value = experiment[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ExperimentStatusError(f"{key} must be a non-negative integer")
        if not isinstance(experiment["metrics"], Mapping):
            raise ExperimentStatusError("metrics must be an object")
        if experiment["health"] not in HEALTH_STATES:
            raise ExperimentStatusError("experiment has invalid health")
        limitations = experiment["limitations"]
        if not isinstance(limitations, list) or not all(
            isinstance(item, str) and item for item in limitations
        ):
            raise ExperimentStatusError("limitations must be a list of strings")


def collect_read_only_facts(
    db_path: str | Path | None = None,
    *,
    observation_reports: Iterable[Mapping[str, Any]] = (),
    scorecards: Iterable[Mapping[str, Any]] = (),
    weekly_decisions: Iterable[Mapping[str, Any]] = (),
    through: str | datetime | None = None,
) -> tuple[dict[str, dict[str, Any]], str]:
    """Collect existing app facts without creating or mutating any artifact.

    The returned source status is ``ok`` when a readable SQLite source or a
    supplied JSON fact source exists, ``partial`` when no factual source was
    available, and ``stale`` when any supplied packet explicitly says it is
    stale.  The active plan is intentionally not treated as an observation.
    """

    by_deployment: dict[str, dict[str, Any]] = {}
    source_seen = False
    source_status = "partial"
    if db_path is not None and Path(db_path).expanduser().exists():
        source_seen = True
        _merge_db_facts(
            by_deployment,
            Path(db_path).expanduser(),
            through=_date_cutoff(through),
        )
        source_status = "ok"
    for payload in (*observation_reports, *scorecards, *weekly_decisions):
        source_seen = True
        if str(payload.get("source_status") or "").lower() == "stale":
            source_status = "stale"
        if payload.get("deployment_id") or payload.get("experiment_id"):
            _merge_report(by_deployment, payload)
        _merge_json_facts(by_deployment, payload)
    if not source_seen:
        source_status = "partial"
    return by_deployment, source_status


def _experiment_row(
    deployment: Mapping[str, Any],
    facts: Mapping[str, Any],
    *,
    source_status: str,
    as_of: str,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    deployment_id = str(deployment["deployment_id"])
    metadata = _nested_mapping(deployment, "source", "metadata")
    execution = _nested_mapping(deployment, "execution")
    stage = str(
        metadata.get("stage")
        or metadata.get("authorization_mode")
        or ("shadow" if execution.get("shadow_only") else "live")
    )
    counts = _counts(facts)
    limitations = _string_list(facts.get("limitations"))
    local_source_status = str(facts.get("source_status") or source_status)
    if local_source_status == "partial":
        limitations.append("partial_source")
    elif local_source_status == "stale":
        limitations.append("stale_source")
    elif local_source_status == "unavailable":
        limitations.append("source_unavailable")
    if counts["observations"] == 0:
        limitations.append("no_observations")
    if counts["opportunities"] == 0:
        limitations.append("no_opportunities")
    if counts["entries"] == 0:
        limitations.append("no_entries")
    if counts["closed"] == 0:
        limitations.append("no_closed_sample")

    health = str(facts.get("health") or "")
    if health not in HEALTH_STATES:
        if local_source_status in {"stale", "unavailable", "partial"}:
            health = "inconclusive"
        elif counts["closed"] == 0:
            health = "collecting"
        else:
            health = "ready_for_review"

    observation_window = facts.get("observation_window")
    if not isinstance(observation_window, Mapping):
        window_start = (
            facts.get("window_start")
            or plan.get("trading_date")
            or plan.get("generated_at")
            or as_of
        )
        window_end = facts.get("window_end") or facts.get("latest_observed_at") or as_of
        observation_window = {"start": window_start, "end": window_end}
    else:
        observation_window = {
            "start": observation_window.get("start"),
            "end": observation_window.get("end"),
        }

    strategy_name = _strategy_name(metadata)
    row = {
        "experiment_id": deployment_id,
        "display_name": _display_name(metadata, strategy_name=strategy_name),
        "strategy_name": strategy_name,
        "stage": stage,
        "paper_live": (
            "paper"
            if execution.get("shadow_only") or stage.lower() in {"shadow", "paper"}
            else "live"
        ),
        "configuration_identity": _configuration_identity(deployment, metadata),
        "observation_window": observation_window,
        "observations": counts["observations"],
        "opportunities": counts["opportunities"],
        "entries": counts["entries"],
        "closed": counts["closed"],
        "metrics": _metrics(facts),
        "health": health,
        "limitations": _dedupe(limitations),
        "provenance": {
            "active_plan_id": plan.get("active_plan_id"),
            "plan_revision_id": plan.get("plan_revision_id"),
            "source_origin": _nested_mapping(deployment, "source").get("origin"),
        },
    }
    return {key: value for key, value in row.items() if value is not None}


def _strategy_name(metadata: Mapping[str, Any]) -> str | None:
    direct = metadata.get("strategy_name")
    if direct:
        return str(direct)
    summary = metadata.get("playbook_summary")
    if isinstance(summary, Mapping):
        mala_evidence = summary.get("mala_evidence")
        if isinstance(mala_evidence, Mapping) and mala_evidence.get("strategy_name"):
            return str(mala_evidence["strategy_name"])
    return None


def _display_name(
    metadata: Mapping[str, Any], *, strategy_name: str | None
) -> str | None:
    for key in ("display_name", "playbook_name", "name"):
        if metadata.get(key):
            return str(metadata[key])
    symbol = str(metadata.get("catalog_symbol") or "").strip().upper()
    direction = str(metadata.get("direction") or "").strip().title()
    prefix = " ".join(part for part in (symbol, direction) if part)
    if strategy_name and prefix:
        return f"{prefix} — {strategy_name}"
    return strategy_name or prefix or None


def _counts(facts: Mapping[str, Any]) -> dict[str, int]:
    raw_counts = facts.get("counts")
    counts = raw_counts if isinstance(raw_counts, Mapping) else facts
    values: dict[str, int] = {}
    for key in ("observations", "opportunities", "entries", "closed"):
        try:
            value = int(counts.get(key, 0) or 0)
        except (TypeError, ValueError):
            value = 0
        scalar_value = _number(facts.get(key))
        values[key] = max(value, int(scalar_value or 0), 0)
    values["observations"] = max(
        values["observations"],
        values["opportunities"],
        values["entries"],
        values["closed"],
    )
    return values


def _metrics(facts: Mapping[str, Any]) -> dict[str, Any]:
    metrics = facts.get("metrics")
    result = dict(metrics) if isinstance(metrics, Mapping) else {}
    for key in (
        "closed_net_r",
        "net_r",
        "realized_pnl_usd",
        "net_pnl_usd",
        "total_pnl_usd",
        "win_rate",
        "wins",
    ):
        if key in facts and key not in result:
            result[key] = facts[key]
    return result


def _configuration_identity(
    deployment: Mapping[str, Any], metadata: Mapping[str, Any]
) -> str:
    for key in (
        "configuration_identity",
        "config_fingerprint",
        "deployment_contract_sha256",
        "experiment_fingerprint",
        "evidence_binding_sha256",
    ):
        value = metadata.get(key) or deployment.get(key)
        if value:
            return str(value)
    return canonical_hash(
        {
            "deployment_id": deployment.get("deployment_id"),
            "strategy": deployment.get("strategy"),
            "execution": deployment.get("execution"),
            "exit": deployment.get("exit"),
        }
    )


def _merge_db_facts(
    target: dict[str, dict[str, Any]],
    db_path: Path,
    *,
    through: str | None = None,
) -> None:
    events: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    try:
        with closing(
            sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
        ) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "events" in tables:
                placeholders = ", ".join("?" for _ in STATUS_EVENT_TYPES)
                event_query = (
                    "SELECT created_at, event_type, payload FROM events "
                    f"WHERE event_type IN ({placeholders})"
                )
                event_params: tuple[str, ...] = STATUS_EVENT_TYPES
                if through is not None:
                    event_query += " AND created_at < ?"
                    event_params = (*event_params, f"{through}T23:59:59.999999+00:00")
                event_query += " ORDER BY id"
                for created_at, event_type, payload_text in connection.execute(
                    event_query, event_params
                ):
                    try:
                        payload = json.loads(payload_text)
                    except (TypeError, json.JSONDecodeError):
                        payload = {}
                    events.append(
                        {
                            "created_at": created_at,
                            "event_type": event_type,
                            "payload": payload if isinstance(payload, dict) else {},
                        }
                    )
            if "trade_sessions" in tables:
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(trade_sessions)"
                    ).fetchall()
                }
                selected = [
                    column
                    for column in (
                        "trade_id",
                        "deployment_id",
                        "status",
                        "entry_timestamp",
                        "exit_filled_at",
                        "entry_price",
                        "exit_price",
                        "exit_filled_quantity",
                        "quantity",
                    )
                    if column in columns
                ]
                if "deployment_id" in selected:
                    query = f"SELECT {', '.join(selected)} FROM trade_sessions"
                    params: tuple[str, ...] = ()
                    time_columns = [
                        column
                        for column in ("entry_timestamp", "exit_filled_at")
                        if column in selected
                    ]
                    if through is not None and time_columns:
                        time_expression = "COALESCE(" + ", ".join(
                            f"{column}" for column in time_columns
                        ) + ", '')"
                        query += (
                            " WHERE substr(replace("
                            f"{time_expression}, ' ', 'T'), 1, 10) <= ?"
                        )
                        params = (through,)
                    cursor = connection.execute(query, params)
                    rows = [dict(zip(selected, row)) for row in cursor.fetchall()]
    except sqlite3.Error:
        # A readable path with an incomplete schema is a partial source, not a
        # reason to fail the trading app or to invent an empty success.
        return

    for deployment_id, summary in build_shadow_evidence(events).items():
        fact = target.setdefault(deployment_id, {})
        _merge_fact_values(
            fact,
            {
                "counts": {
                    "entries": summary.get("entry_count", 0),
                    "closed": summary.get("exit_count", 0),
                    "observations": len(summary.get("trades") or []),
                },
                "metrics": {
                    "realized_pnl_usd": summary.get("realized_pnl_usd", 0.0),
                },
                "window_start": _first_timestamp(summary.get("trades")),
                "window_end": _last_timestamp(summary.get("trades")),
            },
        )

    event_counts: dict[str, int] = defaultdict(int)
    event_windows: dict[str, list[str]] = defaultdict(list)
    for event in events:
        payload = event.get("payload") or {}
        if not isinstance(payload, Mapping):
            continue
        deployment_id = str(payload.get("deployment_id") or "").strip()
        if not deployment_id:
            continue
        event_type = str(event.get("event_type") or "")
        if event_type == "signal_decision" and payload.get("signal") is True:
            event_counts[f"{deployment_id}:opportunities"] += 1
        elif event_type == "trade_plan":
            event_counts[f"{deployment_id}:opportunities"] += 1
        created_at = str(event.get("created_at") or "")
        if created_at:
            event_windows[deployment_id].append(created_at)
    for row in rows:
        if through is not None and _date_part(row.get("exit_filled_at")) > through:
            # An entry can be live at the cutoff while its later exit is already
            # present in the database. Keep the entry, but do not leak that
            # future close or P&L into the dated status packet.
            row["status"] = "open"
            row["exit_filled_quantity"] = None
            row["exit_price"] = None
        deployment_id = str(row.get("deployment_id") or "").strip()
        if not deployment_id:
            continue
        fact = target.setdefault(deployment_id, {})
        entry_id = str(row.get("trade_id") or "")
        if row.get("entry_timestamp") or row.get("entry_price") is not None:
            _increment_unique(fact, "_entry_ids", entry_id)
        if str(row.get("status") or "").lower() == "closed" and (
            row.get("exit_filled_quantity") or row.get("exit_price") is not None
        ):
            _increment_unique(fact, "_closed_ids", entry_id)
            entry = _number(row.get("entry_price"))
            exit_price = _number(row.get("exit_price"))
            quantity = _number(row.get("exit_filled_quantity")) or _number(
                row.get("quantity")
            )
            if entry is not None and exit_price is not None and quantity is not None:
                fact["realized_pnl_usd"] = float(
                    fact.get("realized_pnl_usd") or 0.0
                ) + (exit_price - entry) * quantity * 100.0
        timestamp = row.get("exit_filled_at") or row.get("entry_timestamp")
        if timestamp:
            event_windows[deployment_id].append(str(timestamp))

    for key, value in event_counts.items():
        deployment_id, metric = key.split(":", 1)
        fact = target.setdefault(deployment_id, {})
        fact[metric] = max(int(fact.get(metric, 0) or 0), value)
    for deployment_id, timestamps in event_windows.items():
        if timestamps:
            fact = target.setdefault(deployment_id, {})
            fact["window_start"] = min(timestamps)
            fact["window_end"] = max(timestamps)
    for fact in target.values():
        entry_ids = fact.pop("_entry_ids", set())
        closed_ids = fact.pop("_closed_ids", set())
        if entry_ids:
            fact["entries"] = max(int(fact.get("entries", 0) or 0), len(entry_ids))
        if closed_ids:
            fact["closed"] = max(int(fact.get("closed", 0) or 0), len(closed_ids))
        fact["observations"] = max(
            int(fact.get("observations", 0) or 0),
            int(fact.get("opportunities", 0) or 0),
            int(fact.get("entries", 0) or 0),
            int(fact.get("closed", 0) or 0),
        )


def _merge_json_facts(
    target: dict[str, dict[str, Any]], payload: Mapping[str, Any]
) -> None:
    """Merge known app-owned report shapes; ignore unknown fields."""

    if isinstance(payload.get("reports"), list):
        for report in payload["reports"]:
            if isinstance(report, Mapping):
                _merge_report(target, report)
    if isinstance(payload.get("facts"), list):
        for row in payload["facts"]:
            if isinstance(row, Mapping):
                _merge_report(target, row)
    if isinstance(payload.get("observations"), list):
        for row in payload["observations"]:
            if isinstance(row, Mapping):
                _merge_report(target, row)
    if isinstance(payload.get("lanes"), list):
        for row in payload["lanes"]:
            if isinstance(row, Mapping):
                _merge_report(target, row)
    for nested_key in ("scorecard", "weekly_decisions", "report"):
        nested = payload.get(nested_key)
        if isinstance(nested, Mapping):
            _merge_json_facts(target, nested)
    by_deployment = payload.get("by_deployment")
    if isinstance(by_deployment, Mapping):
        for deployment_id, row in by_deployment.items():
            if isinstance(row, Mapping):
                _merge_fact_values(
                    target.setdefault(str(deployment_id), {}), row
                )


def _merge_report(target: dict[str, dict[str, Any]], report: Mapping[str, Any]) -> None:
    deployment_id = str(
        report.get("deployment_id")
        or report.get("experiment_id")
        or report.get("id")
        or ""
    ).strip()
    if not deployment_id:
        return
    fact: dict[str, Any] = target.setdefault(deployment_id, {})
    shadow = report.get("shadow_evidence")
    shadow = shadow if isinstance(shadow, Mapping) else {}
    counts = {
        "observations": report.get("observations", report.get("observation_count")),
        "opportunities": report.get(
            "opportunities",
            report.get("signal_true_count", report.get("trade_plan_count")),
        ),
        "entries": report.get("entries", shadow.get("entry_count")),
        "closed": report.get(
            "closed", shadow.get("exit_count", report.get("exit_count"))
        ),
    }
    metrics = dict(report.get("metrics") or {}) if isinstance(report.get("metrics"), Mapping) else {}
    if "realized_pnl_usd" not in metrics:
        metrics["realized_pnl_usd"] = shadow.get(
            "realized_pnl_usd", report.get("realized_pnl_usd")
        )
    values = {
        "counts": counts,
        "metrics": {key: value for key, value in metrics.items() if value is not None},
        "limitations": report.get("limitations") or [],
        "health": report.get("health"),
        "source_status": report.get("source_status"),
        "observation_window": report.get("observation_window"),
        "window_start": report.get("window_start") or report.get("as_of"),
        "window_end": report.get("window_end") or report.get("as_of"),
    }
    _merge_fact_values(fact, values)


def _merge_fact_values(target: dict[str, Any], values: Mapping[str, Any]) -> None:
    raw_counts = values.get("counts")
    if isinstance(raw_counts, Mapping):
        counts = target.setdefault("counts", {})
        for key in ("observations", "opportunities", "entries", "closed"):
            value = _number(raw_counts.get(key))
            if value is not None:
                counts[key] = max(int(counts.get(key, 0) or 0), int(value))
    raw_metrics = values.get("metrics")
    if isinstance(raw_metrics, Mapping):
        metrics = target.setdefault("metrics", {})
        for key, value in raw_metrics.items():
            if value is not None:
                metrics[key] = value
    for key in ("limitations", "health", "source_status", "observation_window", "window_start", "window_end"):
        value = values.get(key)
        if value not in (None, "", []):
            if key == "limitations":
                target[key] = _dedupe(
                    [*_string_list(target.get(key)), *_string_list(value)]
                )
            else:
                target[key] = value
    # Keep the scalar aliases available to the final normalizer for DB facts.
    for key in ("observations", "opportunities", "entries", "closed", "realized_pnl_usd"):
        if key in values and values[key] is not None:
            target[key] = values[key]


def _increment_unique(target: dict[str, Any], key: str, value: str) -> None:
    values = target.setdefault(key, set())
    if value:
        values.add(value)


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        result = model_dump(mode="json")
        if isinstance(result, Mapping):
            return dict(result)
    raise ExperimentStatusError("expected an active plan or mapping")


def _date_cutoff(value: str | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    text = str(value).strip()
    if not text:
        return None
    return text[:10]


def _date_part(value: Any) -> str:
    return str(value or "")[:10]


def _nested_mapping(value: Mapping[str, Any], *keys: str) -> dict[str, Any]:
    current: Any = value
    for key in keys:
        if not isinstance(current, Mapping):
            return {}
        current = current.get(key)
    return dict(current) if isinstance(current, Mapping) else {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    return [str(item) for item in value if str(item)]


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value)))


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso_timestamp(value: str | datetime | None) -> str | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_timestamp(rows: Any) -> str | None:
    timestamps = [
        str(row.get("entry_timestamp") or row.get("exit_created_at") or "")
        for row in rows or []
        if isinstance(row, Mapping)
    ]
    timestamps = [value for value in timestamps if value]
    return min(timestamps) if timestamps else None


def _last_timestamp(rows: Any) -> str | None:
    timestamps = [
        str(row.get("exit_created_at") or row.get("entry_timestamp") or "")
        for row in rows or []
        if isinstance(row, Mapping)
    ]
    timestamps = [value for value in timestamps if value]
    return max(timestamps) if timestamps else None


__all__ = [
    "EFFECT_KEYS",
    "ExperimentStatusError",
    "HEALTH_STATES",
    "SOURCE_STATUSES",
    "STATUS_SCHEMA",
    "build_app_experiment_status",
    "canonical_hash",
    "collect_read_only_facts",
    "validate_app_experiment_status",
]
