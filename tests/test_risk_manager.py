from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import sqlite3

from bhiksha.domain.models import CashBudgetDay, TradeRecord
from bhiksha.persistence.sqlite import SQLiteBackend, SQLiteCashBudgetRepository, SQLiteEventRepository, SQLiteTradeStateRepository
from bhiksha.risk.demotion_store import DemotionStore
from bhiksha.risk.risk_manager import RiskManager, TIER1_HALT_REASON, TIER2_FLATTEN_REASON, RAIL_B_DEMOTED_REASON
from bhiksha.risk.risk_settings import RiskSettings, resolve_risk_settings


class _ManualClock:
    """A settable ``now_fn`` for tests that need to move wall-clock time
    across multiple ``book_actions()`` calls without sleeping."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now = self._now + timedelta(**kwargs)


def _settings(**overrides) -> RiskSettings:
    base = dict(
        rail_a_enabled=True,
        rail_b_enabled=True,
        max_daily_drawdown_pct=2.0,
        flatten_daily_drawdown_pct=3.0,
        demote_window=10,
        demote_min_n=10,
        demote_threshold_usd=0.0,
    )
    base.update(overrides)
    return RiskSettings(**base)


def _manager(tmp_path, *, settings=None, now=None, alert_mode="off"):
    db_path = str(tmp_path / "bhiksha.db")
    backend = SQLiteBackend(db_path)
    return RiskManager(
        settings=settings or _settings(),
        cash_budget_repository=SQLiteCashBudgetRepository(db_path, backend=backend),
        trade_state_repository=SQLiteTradeStateRepository(db_path, backend=backend),
        event_repository=SQLiteEventRepository(db_path, backend=backend),
        demotion_store=DemotionStore(tmp_path / "demoted_deployments.json"),
        alert_mode=alert_mode,
        now_fn=(lambda: now) if now is not None else None,
    ), db_path


def _events(db_path: str, event_type: str | None = None) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        if event_type:
            rows = conn.execute(
                "SELECT event_type, payload FROM events WHERE event_type = ? ORDER BY id", (event_type,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT event_type, payload FROM events ORDER BY id").fetchall()
    return [{"event_type": row[0], "payload": json.loads(row[1])} for row in rows]


def _closed_live_trade(trade_id: str, *, deployment_id: str, entry: float, exit_: float, exit_at: datetime, quantity: int = 1) -> TradeRecord:
    return TradeRecord(
        trade_id=trade_id,
        deployment_id=deployment_id,
        symbol="QQQ",
        option_symbol="QQQ260101C00500000",
        quantity=quantity,
        entry_price=entry,
        entry_timestamp=exit_at,
        status="closed",
        entry_order_id="LIVE123",
        exit_price=exit_,
        exit_filled_quantity=quantity,
        exit_filled_at=exit_at,
    )


NOW = datetime(2026, 4, 20, 15, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Rail A
# --------------------------------------------------------------------------


def test_rail_a_missing_budget_day_is_inactive_and_warns(tmp_path) -> None:
    manager, db_path = _manager(tmp_path, now=NOW)

    result = asyncio.run(manager.book_actions())

    assert result.rail_a.active is False
    assert result.rail_a.reason == "no_cash_budget_day"
    assert result.should_flatten is False
    # Exactly one warning event for the missing-data case -- book_actions()
    # does not double-log an "ok" wrapper on top of the inactive warning.
    decisions = _events(db_path, "risk_manager_decision")
    assert len(decisions) == 1
    assert decisions[-1]["payload"]["decision"] == "inactive"
    assert decisions[-1]["payload"]["severity"] == "warning"
    assert decisions[-1]["payload"]["reason"] == "no_cash_budget_day"


def test_rail_a_pnl_query_failure_is_inactive_not_halt(tmp_path, monkeypatch) -> None:
    manager, db_path = _manager(tmp_path, now=NOW)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=9500.0, buffer_pct=0.05)
        )
    )

    async def boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(manager, "_realized_live_pnl_today", boom)

    result = asyncio.run(manager.book_actions())

    assert result.rail_a.active is False
    assert result.rail_a.reason == "pnl_query_failed"
    assert result.should_flatten is False


def test_rail_a_tier1_halts_new_entries_without_flatten(tmp_path) -> None:
    manager, db_path = _manager(tmp_path, now=NOW)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )
    # -2.5% of 10000 = -250, breaches tier1 (-200) but not tier2 (-300)
    trade = _closed_live_trade("T1", deployment_id="dep1", entry=5.0, exit_=2.5, exit_at=NOW)
    asyncio.run(manager.trade_state_repository.upsert_trade(trade))
    asyncio.run(manager.trade_state_repository.mark_closed("T1", exit_price=2.5, exit_filled_quantity=1, exit_filled_at=NOW))

    book_result = asyncio.run(manager.book_actions())
    assert book_result.rail_a.halted is True
    assert book_result.rail_a.flatten is False
    assert book_result.should_flatten is False

    entry_decision = asyncio.run(manager.allow_entry("dep1"))
    assert entry_decision.allowed is False
    assert entry_decision.reason == TIER1_HALT_REASON
    assert entry_decision.rail == "A"


def test_rail_a_tier1_halt_is_session_sticky(tmp_path) -> None:
    """A tier-1 halt must not silently clear mid-session if a later P&L
    recompute happens to land back above threshold (e.g. a late fill
    correction moves the day's realized P&L). Once halted, stays halted for
    the rest of the session -- the same posture as tier-2 flatten and Rail B.
    """
    manager, db_path = _manager(tmp_path, now=NOW)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )
    trade = _closed_live_trade("T1", deployment_id="dep1", entry=5.0, exit_=2.5, exit_at=NOW)
    asyncio.run(manager.trade_state_repository.upsert_trade(trade))
    asyncio.run(manager.trade_state_repository.mark_closed("T1", exit_price=2.5, exit_filled_quantity=1, exit_filled_at=NOW))

    first = asyncio.run(manager.allow_entry("dep1"))
    assert first.allowed is False
    assert first.reason == TIER1_HALT_REASON

    # Correct the trade's exit price so realized P&L is now well ABOVE
    # threshold -- a naive re-check would allow entries again.
    asyncio.run(manager.trade_state_repository.mark_closed("T1", exit_price=5.0, exit_filled_quantity=1, exit_filled_at=NOW))

    second = asyncio.run(manager.allow_entry("dep1"))
    assert second.allowed is False
    assert second.reason == TIER1_HALT_REASON


def test_rail_a_tier2_breach_reports_should_flatten(tmp_path) -> None:
    manager, db_path = _manager(tmp_path, now=NOW)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )
    # -4% of 10000 = -400, breaches both tier1 (-200) and tier2 (-300)
    trade = _closed_live_trade("T1", deployment_id="dep1", entry=8.0, exit_=4.0, exit_at=NOW)
    asyncio.run(manager.trade_state_repository.upsert_trade(trade))
    asyncio.run(manager.trade_state_repository.mark_closed("T1", exit_price=4.0, exit_filled_quantity=1, exit_filled_at=NOW))

    result = asyncio.run(manager.book_actions())

    assert result.rail_a.halted is True
    assert result.rail_a.flatten is True
    assert result.should_flatten is True
    assert result.flatten_reason == TIER2_FLATTEN_REASON

    decisions = _events(db_path, "risk_manager_decision")
    assert decisions[-1]["payload"]["decision"] == "flatten"


def test_rail_a_ignores_shadow_trades_in_pnl(tmp_path) -> None:
    manager, db_path = _manager(tmp_path, now=NOW)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )
    shadow_trade = TradeRecord(
        trade_id="S1",
        deployment_id="dep1",
        symbol="QQQ",
        option_symbol="QQQ260101C00500000",
        quantity=1,
        entry_price=8.0,
        entry_timestamp=NOW,
        status="closed",
        entry_order_id="SHADOW_ENTRY",
        exit_price=1.0,
        exit_filled_quantity=1,
        exit_filled_at=NOW,
    )
    asyncio.run(manager.trade_state_repository.upsert_trade(shadow_trade))
    asyncio.run(manager.trade_state_repository.mark_closed("S1", exit_price=1.0, exit_filled_quantity=1, exit_filled_at=NOW))

    result = asyncio.run(manager.book_actions())

    assert result.rail_a.realized_live_pnl_usd == 0.0
    assert result.rail_a.halted is False


def test_rail_a_disabled_via_settings_is_inactive(tmp_path) -> None:
    manager, db_path = _manager(tmp_path, now=NOW, settings=_settings(rail_a_enabled=False))
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )

    result = asyncio.run(manager.book_actions())
    assert result.rail_a.active is False
    assert result.rail_a.reason == "rail_a_disabled"


# --------------------------------------------------------------------------
# Rail B
# --------------------------------------------------------------------------


def test_rail_b_fires_exactly_at_min_n_with_negative_expectancy(tmp_path) -> None:
    manager, db_path = _manager(tmp_path, now=NOW, settings=_settings(demote_window=10, demote_min_n=10, demote_threshold_usd=0.0))
    for i in range(9):
        trade = _closed_live_trade(f"T{i}", deployment_id="dep1", entry=5.0, exit_=4.0, exit_at=NOW)
        asyncio.run(manager.trade_state_repository.upsert_trade(trade))
        asyncio.run(manager.trade_state_repository.mark_closed(f"T{i}", exit_price=4.0, exit_filled_quantity=1, exit_filled_at=NOW))

    # 9 losing trades: not demoted yet (below min_n)
    decision = asyncio.run(manager.allow_entry("dep1"))
    assert decision.allowed is True
    assert manager.demotion_store.is_demoted("dep1") is False

    # 10th losing trade crosses min_n with negative mean -> demoted
    trade10 = _closed_live_trade("T9", deployment_id="dep1", entry=5.0, exit_=4.0, exit_at=NOW)
    asyncio.run(manager.trade_state_repository.upsert_trade(trade10))
    asyncio.run(manager.trade_state_repository.mark_closed("T9", exit_price=4.0, exit_filled_quantity=1, exit_filled_at=NOW))

    decision = asyncio.run(manager.allow_entry("dep1"))
    assert decision.allowed is False
    assert decision.reason == RAIL_B_DEMOTED_REASON
    assert decision.rail == "B"
    assert manager.demotion_store.is_demoted("dep1") is True

    demotion_events = _events(db_path, "risk_manager_demotion")
    assert len(demotion_events) == 1
    assert demotion_events[0]["payload"]["deployment_id"] == "dep1"
    assert demotion_events[0]["payload"]["window_n"] == 10
    assert demotion_events[0]["payload"]["mean_pnl_usd"] == -100.0


def test_rail_b_does_not_fire_with_positive_expectancy(tmp_path) -> None:
    manager, db_path = _manager(tmp_path, now=NOW, settings=_settings(demote_window=10, demote_min_n=10, demote_threshold_usd=0.0))
    for i in range(10):
        # winning trades: exit > entry
        trade = _closed_live_trade(f"T{i}", deployment_id="dep1", entry=4.0, exit_=5.0, exit_at=NOW)
        asyncio.run(manager.trade_state_repository.upsert_trade(trade))
        asyncio.run(manager.trade_state_repository.mark_closed(f"T{i}", exit_price=5.0, exit_filled_quantity=1, exit_filled_at=NOW))

    decision = asyncio.run(manager.allow_entry("dep1"))
    assert decision.allowed is True
    assert manager.demotion_store.is_demoted("dep1") is False


def test_rail_b_is_one_way_no_flap(tmp_path) -> None:
    manager, db_path = _manager(tmp_path, now=NOW, settings=_settings(demote_window=10, demote_min_n=10, demote_threshold_usd=0.0))
    for i in range(10):
        trade = _closed_live_trade(f"T{i}", deployment_id="dep1", entry=5.0, exit_=4.0, exit_at=NOW)
        asyncio.run(manager.trade_state_repository.upsert_trade(trade))
        asyncio.run(manager.trade_state_repository.mark_closed(f"T{i}", exit_price=4.0, exit_filled_quantity=1, exit_filled_at=NOW))

    first = asyncio.run(manager.allow_entry("dep1"))
    assert first.allowed is False

    # Even if subsequent (hypothetical) trades would look better, add 10 winners
    # and confirm the deployment STAYS demoted (no re-promote from data).
    for i in range(10, 20):
        trade = _closed_live_trade(f"T{i}", deployment_id="dep1", entry=4.0, exit_=6.0, exit_at=NOW)
        asyncio.run(manager.trade_state_repository.upsert_trade(trade))
        asyncio.run(manager.trade_state_repository.mark_closed(f"T{i}", exit_price=6.0, exit_filled_quantity=1, exit_filled_at=NOW))

    second = asyncio.run(manager.allow_entry("dep1"))
    assert second.allowed is False
    assert second.reason == RAIL_B_DEMOTED_REASON

    demotion_events = _events(db_path, "risk_manager_demotion")
    assert len(demotion_events) == 1  # only demoted once, never re-fires


def test_rail_b_only_counts_live_closed_trades_for_this_deployment(tmp_path) -> None:
    manager, db_path = _manager(tmp_path, now=NOW, settings=_settings(demote_window=10, demote_min_n=3, demote_threshold_usd=0.0))
    # Losing trades for a DIFFERENT deployment should not affect dep1.
    for i in range(5):
        trade = _closed_live_trade(f"OTHER{i}", deployment_id="dep2", entry=5.0, exit_=1.0, exit_at=NOW)
        asyncio.run(manager.trade_state_repository.upsert_trade(trade))
        asyncio.run(manager.trade_state_repository.mark_closed(f"OTHER{i}", exit_price=1.0, exit_filled_quantity=1, exit_filled_at=NOW))
    # An open (not closed) live trade for dep1 should not count.
    open_trade = TradeRecord(
        trade_id="OPEN1",
        deployment_id="dep1",
        symbol="QQQ",
        option_symbol="QQQ260101C00500000",
        quantity=1,
        entry_price=5.0,
        entry_timestamp=NOW,
        status="open_unprotected",
        entry_order_id="LIVE999",
    )
    asyncio.run(manager.trade_state_repository.upsert_trade(open_trade))
    # 3 winning closed live trades for dep1.
    for i in range(3):
        trade = _closed_live_trade(f"WIN{i}", deployment_id="dep1", entry=4.0, exit_=6.0, exit_at=NOW)
        asyncio.run(manager.trade_state_repository.upsert_trade(trade))
        asyncio.run(manager.trade_state_repository.mark_closed(f"WIN{i}", exit_price=6.0, exit_filled_quantity=1, exit_filled_at=NOW))

    decision = asyncio.run(manager.allow_entry("dep1"))
    assert decision.allowed is True


def test_rail_b_disabled_via_settings_never_demotes(tmp_path) -> None:
    manager, db_path = _manager(tmp_path, now=NOW, settings=_settings(rail_b_enabled=False, demote_min_n=1))
    trade = _closed_live_trade("T0", deployment_id="dep1", entry=5.0, exit_=0.0, exit_at=NOW)
    asyncio.run(manager.trade_state_repository.upsert_trade(trade))
    asyncio.run(manager.trade_state_repository.mark_closed("T0", exit_price=0.0, exit_filled_quantity=1, exit_filled_at=NOW))

    decision = asyncio.run(manager.allow_entry("dep1"))
    assert decision.allowed is True
    assert manager.demotion_store.is_demoted("dep1") is False


# --------------------------------------------------------------------------
# Proof surface: risk_manager_decision fires on every consult
# --------------------------------------------------------------------------


def test_allow_entry_always_emits_decision_event_even_when_allowed(tmp_path) -> None:
    manager, db_path = _manager(tmp_path, now=NOW)

    decision = asyncio.run(manager.allow_entry("dep1"))

    assert decision.allowed is True
    assert decision.reason == "approved"
    events = _events(db_path, "risk_manager_decision")
    # One internal Rail-A status decision (no budget day -> inactive) plus one
    # deployment-scoped final entry decision: the production proof surface
    # fires on every consult, even a fully "allowed, nothing blocked" one.
    entry_scoped = [event for event in events if event["payload"].get("deployment_id") == "dep1"]
    assert len(entry_scoped) == 1
    assert entry_scoped[0]["payload"]["decision"] == "allowed"


def test_startup_log_emits_resolved_settings(tmp_path) -> None:
    manager, db_path = _manager(tmp_path, now=NOW, settings=_settings(max_daily_drawdown_pct=1.5))

    asyncio.run(manager.startup_log())

    events = _events(db_path, "risk_manager_startup")
    assert len(events) == 1
    assert events[0]["payload"]["max_daily_drawdown_pct"] == 1.5
    assert events[0]["payload"]["rail_a_enabled"] is True


# --------------------------------------------------------------------------
# Settings resolution: env precedence
# --------------------------------------------------------------------------


def test_resolve_risk_settings_env_overrides_default(monkeypatch) -> None:
    monkeypatch.setenv("BHIKSHA_RISK_MAX_DAILY_DRAWDOWN_PCT", "1.25")
    monkeypatch.setenv("BHIKSHA_RISK_FLATTEN_DAILY_DRAWDOWN_PCT", "2.5")
    monkeypatch.setenv("BHIKSHA_RISK_DEMOTE_WINDOW", "20")
    monkeypatch.setenv("BHIKSHA_RISK_DEMOTE_MIN_N", "15")
    monkeypatch.setenv("BHIKSHA_RISK_DEMOTE_THRESHOLD_USD", "-5")
    monkeypatch.setenv("BHIKSHA_RISK_RAIL_A_ENABLED", "false")
    monkeypatch.setenv("BHIKSHA_RISK_RAIL_B_ENABLED", "0")

    settings = resolve_risk_settings()

    assert settings.max_daily_drawdown_pct == 1.25
    assert settings.flatten_daily_drawdown_pct == 2.5
    assert settings.demote_window == 20
    assert settings.demote_min_n == 15
    assert settings.demote_threshold_usd == -5.0
    assert settings.rail_a_enabled is False
    assert settings.rail_b_enabled is False


def test_resolve_risk_settings_defaults_when_no_env(monkeypatch) -> None:
    for key in (
        "BHIKSHA_RISK_MAX_DAILY_DRAWDOWN_PCT",
        "BHIKSHA_RISK_FLATTEN_DAILY_DRAWDOWN_PCT",
        "BHIKSHA_RISK_DEMOTE_WINDOW",
        "BHIKSHA_RISK_DEMOTE_MIN_N",
        "BHIKSHA_RISK_DEMOTE_THRESHOLD_USD",
        "BHIKSHA_RISK_RAIL_A_ENABLED",
        "BHIKSHA_RISK_RAIL_B_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = resolve_risk_settings()

    assert settings.max_daily_drawdown_pct == 2.0
    assert settings.flatten_daily_drawdown_pct == 3.0
    assert settings.demote_window == 10
    assert settings.demote_min_n == 10
    assert settings.demote_threshold_usd == 0.0
    assert settings.rail_a_enabled is True
    assert settings.rail_b_enabled is True


# --------------------------------------------------------------------------
# 2026-07-02 adversarial-audit fixes
# --------------------------------------------------------------------------


def test_allow_entry_blocked_when_only_tier2_breached(tmp_path) -> None:
    """Audit finding #1: a flattening book must never accept new entries.

    With inverted tiers (flatten smaller than halt — reachable pre-validation
    or via a future settings source), a loss can breach tier-2 without
    breaching tier-1. Entries must still be blocked.
    """
    inverted = _settings(max_daily_drawdown_pct=3.0, flatten_daily_drawdown_pct=2.0)
    manager, db_path = _manager(tmp_path, settings=inverted, now=NOW)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )
    # -2.5% of 10000 = -250: breaches flatten (-200) but not halt (-300).
    trade = _closed_live_trade("T1", deployment_id="dep1", entry=5.0, exit_=2.5, exit_at=NOW)
    asyncio.run(manager.trade_state_repository.upsert_trade(trade))
    asyncio.run(manager.trade_state_repository.mark_closed("T1", exit_price=2.5, exit_filled_quantity=1, exit_filled_at=NOW))

    book = asyncio.run(manager.book_actions())
    assert book.rail_a.flatten is True
    assert book.rail_a.halted is False

    decision = asyncio.run(manager.allow_entry("dep1"))
    assert decision.allowed is False


def test_resolve_settings_clamps_inverted_tiers(monkeypatch) -> None:
    monkeypatch.setenv("BHIKSHA_RISK_MAX_DAILY_DRAWDOWN_PCT", "3.0")
    monkeypatch.setenv("BHIKSHA_RISK_FLATTEN_DAILY_DRAWDOWN_PCT", "2.0")
    settings = resolve_risk_settings()
    assert settings.flatten_daily_drawdown_pct == 3.0
    assert any("clamping flatten" in w for w in settings.validation_warnings)


def test_resolve_settings_rejects_negative_percentages(monkeypatch) -> None:
    """Audit finding #2: a negative pct flipped the threshold sign and would
    flatten a healthy, flat book. Negative values must fall back to defaults
    with a visible warning."""
    monkeypatch.setenv("BHIKSHA_RISK_FLATTEN_DAILY_DRAWDOWN_PCT", "-3.0")
    settings = resolve_risk_settings()
    assert settings.flatten_daily_drawdown_pct == 3.0
    assert any("must be > 0" in w for w in settings.validation_warnings)


def test_resolve_settings_warns_on_unparseable_env(monkeypatch) -> None:
    monkeypatch.setenv("BHIKSHA_RISK_MAX_DAILY_DRAWDOWN_PCT", "two percent")
    settings = resolve_risk_settings()
    assert settings.max_daily_drawdown_pct == 2.0
    assert any("not a valid number" in w for w in settings.validation_warnings)


def test_resolve_settings_clamps_min_n_to_window(monkeypatch) -> None:
    monkeypatch.setenv("BHIKSHA_RISK_DEMOTE_WINDOW", "5")
    monkeypatch.setenv("BHIKSHA_RISK_DEMOTE_MIN_N", "10")
    settings = resolve_risk_settings()
    assert settings.demote_min_n == 5
    assert any("clamping min_n" in w for w in settings.validation_warnings)


def test_startup_log_carries_validation_warnings(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BHIKSHA_RISK_FLATTEN_DAILY_DRAWDOWN_PCT", "-1")
    settings = resolve_risk_settings()
    manager, db_path = _manager(tmp_path, settings=settings, now=NOW)
    asyncio.run(manager.startup_log())
    startup = _events(db_path, "risk_manager_startup")
    assert startup and startup[-1]["payload"]["validation_warnings"]


def test_demotion_store_default_path_is_absolute(monkeypatch) -> None:
    """Audit finding #4: the default store path must not depend on cwd."""
    monkeypatch.delenv("BHIKSHA_RISK_DEMOTION_STORE_PATH", raising=False)
    store = DemotionStore()
    assert store.path.is_absolute()
    assert store.path.name == "demoted_deployments.json"


def test_demotion_store_env_path_override(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BHIKSHA_RISK_DEMOTION_STORE_PATH", str(tmp_path / "alt.json"))
    store = DemotionStore()
    assert store.path == tmp_path / "alt.json"


def test_trade_sessions_updated_at_index_created(tmp_path) -> None:
    """Audit perf finding: get_recent_trades must not full-table-scan."""
    db_path = str(tmp_path / "bhiksha.db")
    backend = SQLiteBackend(db_path)
    repo = SQLiteTradeStateRepository(db_path, backend=backend)
    asyncio.run(repo.get_recent_trades(limit=1))  # triggers lazy _init_db
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_trade_sessions_updated_at'"
        ).fetchall()
    assert rows


# --------------------------------------------------------------------------
# 2026-07-02 risk-noise: book_actions() evaluate-throttle + ok-event policy
#
# book_actions() is called once per SYMBOL-bar from BhikshaRuntime (13
# symbols/minute in production) but Rail A is a book-level check. These
# tests cover: (1) the per-minute evaluate cache returns the SAME result
# without recomputing within a minute and DOES recompute across minutes;
# (2) non-ok decisions and decision/state changes always emit a
# risk_manager_decision row uncapped; (3) repeated "ok" decisions are
# capped to one heartbeat row per 10 minutes.
# --------------------------------------------------------------------------


def _manager_with_clock(tmp_path, clock: _ManualClock, *, settings=None, alert_mode="off"):
    db_path = str(tmp_path / "bhiksha.db")
    backend = SQLiteBackend(db_path)
    return RiskManager(
        settings=settings or _settings(),
        cash_budget_repository=SQLiteCashBudgetRepository(db_path, backend=backend),
        trade_state_repository=SQLiteTradeStateRepository(db_path, backend=backend),
        event_repository=SQLiteEventRepository(db_path, backend=backend),
        demotion_store=DemotionStore(tmp_path / "demoted_deployments.json"),
        alert_mode=alert_mode,
        now_fn=clock,
    ), db_path


def test_book_actions_returns_cached_result_within_same_minute(tmp_path) -> None:
    clock = _ManualClock(NOW)
    manager, db_path = _manager_with_clock(tmp_path, clock)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )

    first = asyncio.run(manager.book_actions())
    # 12 more calls within the SAME minute -- simulating the 13-symbol tick
    # fan-out this is meant to collapse.
    for _ in range(12):
        clock.advance(seconds=1)
        result = asyncio.run(manager.book_actions())
        assert result is first  # literally the cached object, not recomputed

    # Exactly one "ok" decision event was emitted for all 13 calls.
    decisions = _events(db_path, "risk_manager_decision")
    assert len(decisions) == 1
    assert decisions[0]["payload"]["decision"] == "ok"


def test_book_actions_reevaluates_across_minute_boundary(tmp_path) -> None:
    clock = _ManualClock(NOW)
    manager, db_path = _manager_with_clock(tmp_path, clock)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )

    first = asyncio.run(manager.book_actions())
    clock.advance(minutes=1)
    second = asyncio.run(manager.book_actions())

    assert second is not first
    assert second.rail_a.realized_live_pnl_usd == first.rail_a.realized_live_pnl_usd  # same book, recomputed fresh


def test_book_actions_flatten_latch_unaffected_by_cache(tmp_path) -> None:
    """The should_flatten session latch must stay fired even though the
    triggering evaluation itself is now cached per-minute."""
    clock = _ManualClock(NOW)
    manager, db_path = _manager_with_clock(tmp_path, clock)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )
    # -4% of 10000 = -400, breaches both tier1 (-200) and tier2 (-300)
    trade = _closed_live_trade("T1", deployment_id="dep1", entry=8.0, exit_=4.0, exit_at=NOW)
    asyncio.run(manager.trade_state_repository.upsert_trade(trade))
    asyncio.run(manager.trade_state_repository.mark_closed("T1", exit_price=4.0, exit_filled_quantity=1, exit_filled_at=NOW))

    result = asyncio.run(manager.book_actions())
    assert result.should_flatten is True

    # A "late fill correction" within the SAME minute that would look
    # healthy again must not un-flatten -- but this call also hits the
    # evaluate cache, so it returns the SAME cached flattened result.
    asyncio.run(manager.trade_state_repository.mark_closed("T1", exit_price=8.0, exit_filled_quantity=1, exit_filled_at=NOW))
    still_cached = asyncio.run(manager.book_actions())
    assert still_cached.should_flatten is True

    # Advance past the minute boundary: recompute happens, P&L now looks
    # healthy, but the session latch must still hold (once flattened, never
    # un-flattened this session).
    clock.advance(minutes=1)
    recomputed = asyncio.run(manager.book_actions())
    assert recomputed.should_flatten is True


def test_book_actions_non_ok_decisions_always_emit_uncapped(tmp_path) -> None:
    clock = _ManualClock(NOW)
    manager, db_path = _manager_with_clock(tmp_path, clock)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )
    # -4% of 10000 = -400, breaches both tiers -> every re-evaluation reports "flatten".
    trade = _closed_live_trade("T1", deployment_id="dep1", entry=8.0, exit_=4.0, exit_at=NOW)
    asyncio.run(manager.trade_state_repository.upsert_trade(trade))
    asyncio.run(manager.trade_state_repository.mark_closed("T1", exit_price=4.0, exit_filled_quantity=1, exit_filled_at=NOW))

    # 5 evaluations, each in a DIFFERENT minute (so the evaluate-cache does
    # not collapse them) -- every one must emit, well under the 10-minute
    # "ok" heartbeat window that would otherwise cap them.
    for _ in range(5):
        asyncio.run(manager.book_actions())
        clock.advance(minutes=1)

    decisions = _events(db_path, "risk_manager_decision")
    assert len(decisions) == 5
    assert all(d["payload"]["decision"] == "flatten" for d in decisions)


def test_book_actions_ok_decision_emits_on_state_change(tmp_path) -> None:
    """A halt->ok or ok->halt transition must emit immediately even if it
    happens well inside the 10-minute ok-heartbeat window."""
    clock = _ManualClock(NOW)
    manager, db_path = _manager_with_clock(tmp_path, clock)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )

    # Minute 0: healthy book -> "ok".
    first = asyncio.run(manager.book_actions())
    assert first.rail_a.reason is None

    # Minute 1: book now breaches tier1 -> "halt". Must emit even though
    # only 1 minute has passed since the last "ok" heartbeat.
    clock.advance(minutes=1)
    trade = _closed_live_trade("T1", deployment_id="dep1", entry=5.0, exit_=2.5, exit_at=NOW)
    asyncio.run(manager.trade_state_repository.upsert_trade(trade))
    asyncio.run(manager.trade_state_repository.mark_closed("T1", exit_price=2.5, exit_filled_quantity=1, exit_filled_at=NOW))
    second = asyncio.run(manager.book_actions())
    assert second.rail_a.halted is True

    decisions = _events(db_path, "risk_manager_decision")
    assert [d["payload"]["decision"] for d in decisions] == ["ok", "halt"]


def test_book_actions_ok_heartbeat_emits_once_per_ten_minutes(tmp_path) -> None:
    clock = _ManualClock(NOW)
    manager, db_path = _manager_with_clock(tmp_path, clock)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )

    # 15 one-minute ticks, all steady "ok" -- expect a heartbeat at minute 0
    # and minute 10 only (the 10-minute window), i.e. 2 rows, not 15.
    for _ in range(15):
        asyncio.run(manager.book_actions())
        clock.advance(minutes=1)

    decisions = _events(db_path, "risk_manager_decision")
    assert all(d["payload"]["decision"] == "ok" for d in decisions)
    assert len(decisions) == 2


def test_book_actions_net_daily_ok_row_volume_is_low(tmp_path) -> None:
    """Sanity check on the net effect: ~6.5 evaluation hours (13 symbols x 1
    call/min for 390 minutes each, collapsed by the minute cache to 390
    evaluations) of a steady healthy book should produce ~40 ok-rows/day
    (one per 10-minute heartbeat), not ~5000."""
    clock = _ManualClock(NOW)
    manager, db_path = _manager_with_clock(tmp_path, clock)
    asyncio.run(
        manager.cash_budget_repository.upsert_day(
            CashBudgetDay(trade_date="2026-04-20", account_type="CASH", broker_cash_only_buying_power=10000.0, usable_budget=10000.0, buffer_pct=0.0)
        )
    )

    trading_day_minutes = 390  # one 6.5h US equity/options session
    symbols_per_minute = 13
    for _minute in range(trading_day_minutes):
        for _symbol in range(symbols_per_minute):
            asyncio.run(manager.book_actions())
        clock.advance(minutes=1)

    decisions = _events(db_path, "risk_manager_decision")
    assert all(d["payload"]["decision"] == "ok" for d in decisions)
    # 390 minutes / 10-minute heartbeat -> 39 rows (minute 0, 10, ..., 380).
    assert len(decisions) == 39
