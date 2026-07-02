"""Startup budget prefetch (operator audit P2, 2026-07-03 finding).

Between 08:31-08:37 CT the runtime repeatedly logged
risk_manager_decision: no_cash_budget_day while a live entry (SMH) was
ALLOWED, because the cash_budget_days row was only created lazily by the
entry/cash-guard path. ``prefetch_cash_budget_day`` (called from
``BhikshaRuntime.run_session`` after broker/health init, before the bar
loop) closes that window by upserting today's row at session start using
the SAME computation ``CashGuard.ensure_day`` already uses.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import sqlite3

from bhiksha.app.runtime import prefetch_cash_budget_day
from bhiksha.persistence.sqlite import SQLiteBackend, SQLiteCashBudgetRepository, SQLiteEventRepository
from bhiksha.risk.cash_guard import CashGuard


class StubOrderManager:
    def __init__(self, *, account_type: str = "CASH", cash_only_buying_power: str = "1000.00") -> None:
        self.account_type = account_type
        self.cash_only_buying_power = cash_only_buying_power

    async def get_account_info(self):
        return {"brokerageAccountType": self.account_type}

    async def get_portfolio(self):
        return {"buyingPower": {"cashOnlyBuyingPower": self.cash_only_buying_power}}


class BoomOrderManager:
    """Simulates a broker call failing at startup."""

    async def get_account_info(self):
        raise RuntimeError("broker unreachable")

    async def get_portfolio(self):
        raise RuntimeError("broker unreachable")


def _events(db_path: str, event_type: str | None = None) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        table_exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchone()
        if not table_exists:
            return []
        if event_type:
            rows = conn.execute(
                "SELECT event_type, payload FROM events WHERE event_type = ? ORDER BY id", (event_type,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT event_type, payload FROM events ORDER BY id").fetchall()
    return [{"event_type": row[0], "payload": json.loads(row[1])} for row in rows]


NOW = datetime(2026, 7, 3, 12, 31, tzinfo=UTC)  # 08:31 ET-ish, matches the audit finding's window


def test_prefetch_upserts_todays_budget_row_using_cash_guard_computation(tmp_path) -> None:
    db_path = str(tmp_path / "bhiksha.db")
    backend = SQLiteBackend(db_path)
    cash_budget_repository = SQLiteCashBudgetRepository(db_path, backend=backend)
    event_repository = SQLiteEventRepository(db_path, backend=backend)
    cash_guard = CashGuard(
        order_manager=StubOrderManager(account_type="CASH", cash_only_buying_power="2000.00"),
        repository=cash_budget_repository,
    )

    asyncio.run(
        prefetch_cash_budget_day(cash_guard, event_repository=event_repository, now=NOW, output=lambda *_: None)
    )

    day = asyncio.run(cash_budget_repository.get_day("2026-07-03"))
    assert day is not None
    assert day.account_type == "CASH"
    assert day.broker_cash_only_buying_power == 2000.0
    # Reuses CashGuard's own default buffer (5%): usable_budget = 2000 * 0.95.
    assert day.usable_budget == 1900.0

    # No failure event on the happy path.
    assert _events(db_path, "cash_budget_prefetch_failed") == []


def test_prefetch_is_the_row_allow_entry_then_finds(tmp_path) -> None:
    """End-to-end proof: after prefetch runs, RiskManager.allow_entry no
    longer blocks on risk_rail_a_budget_unavailable for today's trade date."""
    from bhiksha.persistence.sqlite import SQLiteTradeStateRepository
    from bhiksha.risk.risk_manager import RiskManager, BUDGET_UNAVAILABLE_REASON
    from bhiksha.risk.risk_settings import RiskSettings

    db_path = str(tmp_path / "bhiksha.db")
    backend = SQLiteBackend(db_path)
    cash_budget_repository = SQLiteCashBudgetRepository(db_path, backend=backend)
    event_repository = SQLiteEventRepository(db_path, backend=backend)
    trade_state_repository = SQLiteTradeStateRepository(db_path, backend=backend)
    cash_guard = CashGuard(
        order_manager=StubOrderManager(account_type="CASH", cash_only_buying_power="2000.00"),
        repository=cash_budget_repository,
    )
    settings = RiskSettings(
        rail_a_enabled=True,
        rail_b_enabled=True,
        max_daily_drawdown_pct=2.0,
        flatten_daily_drawdown_pct=3.0,
        demote_window=10,
        demote_min_n=10,
        demote_threshold_usd=0.0,
    )
    risk_manager = RiskManager(
        settings=settings,
        cash_budget_repository=cash_budget_repository,
        trade_state_repository=trade_state_repository,
        event_repository=event_repository,
        now_fn=lambda: NOW,
    )

    # Before prefetch: no row yet -> blocked (this is the 08:31-08:37 window).
    before = asyncio.run(risk_manager.allow_entry("dep1"))
    assert before.allowed is False
    assert before.reason == BUDGET_UNAVAILABLE_REASON

    asyncio.run(
        prefetch_cash_budget_day(cash_guard, event_repository=event_repository, now=NOW, output=lambda *_: None)
    )

    after = asyncio.run(risk_manager.allow_entry("dep1"))
    assert after.allowed is True
    assert after.reason == "approved"


def test_prefetch_is_idempotent_when_row_already_exists(tmp_path) -> None:
    db_path = str(tmp_path / "bhiksha.db")
    backend = SQLiteBackend(db_path)
    cash_budget_repository = SQLiteCashBudgetRepository(db_path, backend=backend)
    event_repository = SQLiteEventRepository(db_path, backend=backend)
    order_manager = StubOrderManager(account_type="CASH", cash_only_buying_power="2000.00")
    cash_guard = CashGuard(order_manager=order_manager, repository=cash_budget_repository)

    asyncio.run(
        prefetch_cash_budget_day(cash_guard, event_repository=event_repository, now=NOW, output=lambda *_: None)
    )
    # Change what the broker would report; a second prefetch must NOT
    # overwrite the existing row (ensure_day is idempotent per trade date).
    order_manager.cash_only_buying_power = "999999.00"
    asyncio.run(
        prefetch_cash_budget_day(cash_guard, event_repository=event_repository, now=NOW, output=lambda *_: None)
    )

    day = asyncio.run(cash_budget_repository.get_day("2026-07-03"))
    assert day.broker_cash_only_buying_power == 2000.0


def test_prefetch_broker_failure_warns_and_does_not_raise(tmp_path) -> None:
    db_path = str(tmp_path / "bhiksha.db")
    backend = SQLiteBackend(db_path)
    cash_budget_repository = SQLiteCashBudgetRepository(db_path, backend=backend)
    event_repository = SQLiteEventRepository(db_path, backend=backend)
    cash_guard = CashGuard(order_manager=BoomOrderManager(), repository=cash_budget_repository)

    logged = []

    # Must not raise -- a broker failure at startup is a warning, not a
    # crash (the Rail-A unknown-budget block in allow_entry is the backstop).
    asyncio.run(
        prefetch_cash_budget_day(cash_guard, event_repository=event_repository, now=NOW, output=logged.append)
    )

    day = asyncio.run(cash_budget_repository.get_day("2026-07-03"))
    assert day is None

    warnings = _events(db_path, "cash_budget_prefetch_failed")
    assert len(warnings) == 1
    assert warnings[0]["payload"]["trade_date"] == "2026-07-03"

    assert any("CASH_BUDGET_PREFETCH_FAILED" in line for line in logged)
