"""Concrete ``SettingsSource``: risk knobs read from the compiled active plan.

Implements the seam documented in ``bhiksha.risk.risk_settings.SettingsSource``.
Rather than adding a new network read at live-session start, this wraps the
``operator_defaults`` dict already carried on the compiled ``active_plan.json``
(see ``ActivePlan.operator_defaults`` in ``bhiksha.config.models`` and
``load_operator_defaults_sheet_rows`` / ``compile_active_plan_from_rows`` in
``bhiksha.active_plan.compiler``). The plan is synced from the
``Operator_Defaults_v1`` Google Sheet tab at plan-compile time, well before
the live session starts, so this source needs no I/O of its own.

Sheet key convention
---------------------
The operator adds rows to the ``Operator_Defaults_v1`` tab with
``section=default`` and one of the ``key`` values below (``value`` is the
knob's raw value, same format as the equivalent env var):

    risk-manager knob                          Operator_Defaults_v1 "key"
    ------------------------------------------ ---------------------------
    BHIKSHA_RISK_MAX_DAILY_DRAWDOWN_PCT      -> max_daily_drawdown_pct
    BHIKSHA_RISK_FLATTEN_DAILY_DRAWDOWN_PCT  -> flatten_daily_drawdown_pct
    BHIKSHA_RISK_DEMOTE_WINDOW               -> demote_window
    BHIKSHA_RISK_DEMOTE_MIN_N                -> demote_min_n
    BHIKSHA_RISK_DEMOTE_THRESHOLD_USD        -> demote_threshold_usd
    BHIKSHA_RISK_RAIL_A_ENABLED              -> rail_a_enabled
    BHIKSHA_RISK_RAIL_B_ENABLED              -> rail_b_enabled

These keys are exactly ``key.removeprefix("BHIKSHA_RISK_").lower()`` applied
to each env var name -- the same derivation
``bhiksha.risk.risk_settings._raw_value`` already performs when it consults a
``SettingsSource``. Do not rename these keys without updating that mapping;
``resolve_risk_settings`` and this module must agree on the exact string.

Precedence (unchanged, enforced by ``resolve_risk_settings``): env var wins
if set and non-blank; otherwise this sheet-backed source is consulted;
otherwise the hardcoded default. Values from either layer still flow through
the same validation/clamping in ``resolve_risk_settings`` -- an invalid sheet
value degrades to the default plus a ``validation_warnings`` entry, exactly
like an invalid env var.
"""

from __future__ import annotations

from typing import Any


class PlanOperatorDefaultsSource:
    """``SettingsSource`` backed by a compiled active plan's operator_defaults.

    Construct with the flat ``operator_defaults`` dict from a compiled plan
    (``ActivePlan.operator_defaults`` / ``active_plan["operator_defaults"]``).
    Only the top-level ("default"-section) keys are consulted -- risk knobs
    are not nested under a sub-section in the sheet.
    """

    __slots__ = ("_defaults",)

    def __init__(self, operator_defaults: dict[str, Any] | None) -> None:
        self._defaults = dict(operator_defaults or {})

    def get(self, key: str) -> str | None:
        if key not in self._defaults:
            return None
        value = self._defaults[key]
        if value is None:
            return None
        return str(value)

    @classmethod
    def from_active_plan(cls, active_plan: dict[str, Any] | None) -> "PlanOperatorDefaultsSource":
        """Build directly from ``BhikshaRuntime.active_plan`` (a raw dict, or
        ``None`` when the runtime was built without a compiled plan path)."""
        if not active_plan:
            return cls({})
        operator_defaults = active_plan.get("operator_defaults")
        return cls(operator_defaults if isinstance(operator_defaults, dict) else {})
