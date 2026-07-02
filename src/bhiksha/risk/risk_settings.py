"""Resolve Bhiksha risk-manager thresholds: env > operator-sheet > default.

Mirrors the resolution style already used for cash-guard knobs
(``bhiksha.risk.cash_guard.cash_guard_mode`` /
``cash_guard_buffer_pct``): a plain ``os.getenv`` read with a typed default,
no framework. The one addition here is a documented ``SettingsSource`` seam
for the operator-sheet layer, plus a single ``resolve_risk_settings()``
call so the runtime can log the resolved values once at startup (see
``risk_manager_startup`` event emitted by callers of this module).

Operator-sheet layer: the concrete implementation is
``bhiksha.risk.plan_operator_defaults_source.PlanOperatorDefaultsSource``. It
does NOT add a new network read at live-session start -- it wraps the
``operator_defaults`` dict already carried on the compiled ``active_plan.json``
(populated from the ``Operator_Defaults_v1`` Google Sheet tab -- see
``load_operator_defaults_sheet_rows`` / ``ActivePlan.operator_defaults`` in
``bhiksha.active_plan.compiler`` / ``bhiksha.config.models``), which is
already synced well before the session starts. See that module's docstring
for the exact sheet key names the operator adds as rows.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Protocol


class SettingsSource(Protocol):
    """Operator-sheet settings layer consulted between env and default.

    Precedence is env > operator-sheet > default. The concrete
    implementation, ``PlanOperatorDefaultsSource`` (see
    ``bhiksha.risk.plan_operator_defaults_source``), wraps the
    ``operator_defaults`` dict already carried on the compiled active plan --
    a flat "section=default" projection of the ``Operator_Defaults_v1``
    worksheet -- and returns values for ``BHIKSHA_RISK_*``-shaped keys
    (without the env prefix, lowercased) for ``resolve_risk_settings`` to
    consult between env and the hardcoded default.
    """

    def get(self, key: str) -> str | None:
        ...


@dataclass(slots=True, frozen=True)
class RiskSettings:
    rail_a_enabled: bool
    rail_b_enabled: bool
    max_daily_drawdown_pct: float
    flatten_daily_drawdown_pct: float
    demote_window: int
    demote_min_n: int
    demote_threshold_usd: float
    # Audit fix (2026-07-02): every rejected/clamped input is surfaced here and
    # carried into the ``risk_manager_startup`` event — a silent fallback hid
    # two reproducible live bugs (tier inversion; negative pct flipping the
    # threshold sign and flattening a healthy book).
    validation_warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, float | int | bool | list[str]]:
        return {
            "rail_a_enabled": self.rail_a_enabled,
            "rail_b_enabled": self.rail_b_enabled,
            "max_daily_drawdown_pct": self.max_daily_drawdown_pct,
            "flatten_daily_drawdown_pct": self.flatten_daily_drawdown_pct,
            "demote_window": self.demote_window,
            "demote_min_n": self.demote_min_n,
            "demote_threshold_usd": self.demote_threshold_usd,
            "validation_warnings": list(self.validation_warnings),
        }


_DEFAULT_MAX_DAILY_DRAWDOWN_PCT = 2.0
_DEFAULT_FLATTEN_DAILY_DRAWDOWN_PCT = 3.0
_DEFAULT_DEMOTE_WINDOW = 10
_DEFAULT_DEMOTE_MIN_N = 10
_DEFAULT_DEMOTE_THRESHOLD_USD = 0.0


def resolve_risk_settings(*, settings_source: SettingsSource | None = None) -> RiskSettings:
    """Resolve every risk-manager knob once: env > operator-sheet > default.

    ``settings_source`` is the ``SettingsSource`` hook described above. The
    live runtime passes a ``PlanOperatorDefaultsSource`` built from the
    session's compiled active plan (see ``BhikshaRuntime.run_session`` in
    ``bhiksha.app.runtime``); other callers (tests, one-off scripts) may
    still omit it, which degrades this to env > default -- identical to the
    rest of the repo's env-resolved knobs.

    VALIDATION (audit fix 2026-07-02): values are range-checked and
    cross-checked; anything rejected falls back to the safe default and is
    reported in ``validation_warnings`` (never silently). Invariants enforced:
    percentages are strictly positive; ``flatten >= halt`` (tier-2 may never
    fire before tier-1); ``1 <= demote_min_n <= demote_window``.
    """
    warnings: list[str] = []
    rail_a_enabled = _resolve_bool("BHIKSHA_RISK_RAIL_A_ENABLED", True, settings_source, warnings)
    rail_b_enabled = _resolve_bool("BHIKSHA_RISK_RAIL_B_ENABLED", True, settings_source, warnings)
    max_dd = _resolve_float(
        "BHIKSHA_RISK_MAX_DAILY_DRAWDOWN_PCT", _DEFAULT_MAX_DAILY_DRAWDOWN_PCT, settings_source, warnings
    )
    flatten_dd = _resolve_float(
        "BHIKSHA_RISK_FLATTEN_DAILY_DRAWDOWN_PCT", _DEFAULT_FLATTEN_DAILY_DRAWDOWN_PCT, settings_source, warnings
    )
    demote_window = _resolve_int("BHIKSHA_RISK_DEMOTE_WINDOW", _DEFAULT_DEMOTE_WINDOW, settings_source, warnings)
    demote_min_n = _resolve_int("BHIKSHA_RISK_DEMOTE_MIN_N", _DEFAULT_DEMOTE_MIN_N, settings_source, warnings)
    demote_threshold_usd = _resolve_float(
        "BHIKSHA_RISK_DEMOTE_THRESHOLD_USD", _DEFAULT_DEMOTE_THRESHOLD_USD, settings_source, warnings
    )

    if max_dd <= 0:
        warnings.append(
            f"max_daily_drawdown_pct={max_dd} must be > 0; using default {_DEFAULT_MAX_DAILY_DRAWDOWN_PCT}"
        )
        max_dd = _DEFAULT_MAX_DAILY_DRAWDOWN_PCT
    if flatten_dd <= 0:
        warnings.append(
            f"flatten_daily_drawdown_pct={flatten_dd} must be > 0; using default {_DEFAULT_FLATTEN_DAILY_DRAWDOWN_PCT}"
        )
        flatten_dd = _DEFAULT_FLATTEN_DAILY_DRAWDOWN_PCT
    if flatten_dd < max_dd:
        warnings.append(
            f"flatten_daily_drawdown_pct={flatten_dd} < max_daily_drawdown_pct={max_dd} "
            f"(tier-2 may never fire before tier-1); clamping flatten to {max_dd}"
        )
        flatten_dd = max_dd
    if demote_window < 1:
        warnings.append(f"demote_window={demote_window} must be >= 1; using default {_DEFAULT_DEMOTE_WINDOW}")
        demote_window = _DEFAULT_DEMOTE_WINDOW
    if demote_min_n < 1:
        warnings.append(f"demote_min_n={demote_min_n} must be >= 1; using default {_DEFAULT_DEMOTE_MIN_N}")
        demote_min_n = _DEFAULT_DEMOTE_MIN_N
    if demote_min_n > demote_window:
        warnings.append(
            f"demote_min_n={demote_min_n} > demote_window={demote_window}; clamping min_n to the window"
        )
        demote_min_n = demote_window

    return RiskSettings(
        rail_a_enabled=rail_a_enabled,
        rail_b_enabled=rail_b_enabled,
        max_daily_drawdown_pct=max_dd,
        flatten_daily_drawdown_pct=flatten_dd,
        demote_window=demote_window,
        demote_min_n=demote_min_n,
        demote_threshold_usd=demote_threshold_usd,
        validation_warnings=tuple(warnings),
    )


def _raw_value(key: str, settings_source: SettingsSource | None) -> str | None:
    env_value = os.getenv(key)
    if env_value is not None and env_value.strip() != "":
        return env_value
    if settings_source is not None:
        sheet_key = key.removeprefix("BHIKSHA_RISK_").lower()
        sheet_value = settings_source.get(sheet_key)
        if sheet_value is not None and str(sheet_value).strip() != "":
            return str(sheet_value)
    return None


def _resolve_bool(key: str, default: bool, settings_source: SettingsSource | None, warnings: list[str]) -> bool:
    raw = _raw_value(key, settings_source)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    warnings.append(f"{key}={raw!r} is not a valid boolean; using default {default}")
    return default


def _resolve_float(key: str, default: float, settings_source: SettingsSource | None, warnings: list[str]) -> float:
    raw = _raw_value(key, settings_source)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        warnings.append(f"{key}={raw!r} is not a valid number; using default {default}")
        return default


def _resolve_int(key: str, default: int, settings_source: SettingsSource | None, warnings: list[str]) -> int:
    raw = _raw_value(key, settings_source)
    if raw is None:
        return default
    try:
        return int(float(raw))
    except ValueError:
        warnings.append(f"{key}={raw!r} is not a valid integer; using default {default}")
        return default
