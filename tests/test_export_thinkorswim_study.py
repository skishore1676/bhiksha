from pathlib import Path
import sqlite3

from bhiksha.tools.export_thinkorswim_study import load_trades, render_study, write_studies


def test_load_trades_filters_by_symbol_and_date(tmp_path: Path) -> None:
    db_path = tmp_path / "bhiksha.db"
    _make_trade_db(db_path)

    trades = load_trades(db_path, symbols={"TSLA"})

    assert len(trades) == 1
    assert trades[0].symbol == "TSLA"
    assert trades[0].option_symbol == "TSLA260515P00365000"
    assert trades[0].entry_price == 6.0
    assert trades[0].exit_price == 5.25


def test_render_study_marks_entry_and_exit_in_et() -> None:
    db_path = Path(__file__).parent / "unused.db"
    del db_path
    trade = load_trades(_fixture_db())[0]

    study = render_study("TSLA", [trade])

    assert "declare upper;" in study
    assert 'def symbolOk = GetSymbol() == "TSLA";' in study
    assert "GetYYYYMMDD() == 20260430" in study
    assert "SecondsFromTime(1132) == 0" in study
    assert "SecondsFromTime(1438) == 0" in study
    assert "ENTRY SHORT" in study
    assert "EXIT strategy" in study
    assert "P/L $-75" in study


def test_write_studies_outputs_one_file_per_symbol(tmp_path: Path) -> None:
    db_path = tmp_path / "bhiksha.db"
    _make_trade_db(db_path)
    trades = load_trades(db_path)

    written = write_studies(trades, tmp_path / "tos")

    assert [path.name for path in written] == ["bhiksha_trades_AMD.ts", "bhiksha_trades_TSLA.ts"]
    assert "Bhiksha TSLA trades: 1" in (tmp_path / "tos" / "bhiksha_trades_TSLA.ts").read_text(
        encoding="utf-8"
    )


def _fixture_db() -> Path:
    path = Path("/tmp/bhiksha_tos_export_test.db")
    if path.exists():
        path.unlink()
    _make_trade_db(path, include_amd=False)
    return path


def _make_trade_db(path: Path, *, include_amd: bool = True) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE trade_sessions (
                trade_id TEXT PRIMARY KEY,
                deployment_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                option_symbol TEXT,
                quantity INTEGER NOT NULL,
                entry_price REAL,
                underlying_entry_price REAL,
                entry_timestamp TEXT,
                status TEXT NOT NULL,
                stop_price REAL,
                target_price REAL,
                exit_mode TEXT,
                exit_price REAL,
                exit_filled_at TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO trade_sessions (
                trade_id, deployment_id, symbol, option_symbol, quantity, entry_price,
                underlying_entry_price, entry_timestamp, status, stop_price, target_price,
                exit_mode, exit_price, exit_filled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "e4052971-6ee7-495a-bbd3-eac917081a9a",
                "strategy_elastic_band_reversion_tsla_short_74e3f56b682a_live_row_4",
                "TSLA",
                "TSLA260515P00365000",
                1,
                6.0,
                379.29,
                "2026-04-30T15:32:11.672000+00:00",
                "closed",
                3.9,
                None,
                "strategy",
                5.25,
                "2026-04-30T18:38:07.686000+00:00",
            ),
        )
        if include_amd:
            conn.execute(
                """
                INSERT INTO trade_sessions (
                    trade_id, deployment_id, symbol, option_symbol, quantity, entry_price,
                    underlying_entry_price, entry_timestamp, status, stop_price, target_price,
                    exit_mode, exit_price, exit_filled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "fb71f7af-a56a-49bf-a167-d0d7ae37085f",
                    "strategy_jerk_pivot_current_basket_discovery_amd_short_live_row_9",
                    "AMD",
                    "AMD260424P00277500",
                    1,
                    7.3,
                    278.48,
                    "2026-04-17T18:10:00+00:00",
                    "closed",
                    4.7,
                    None,
                    "hard_flat",
                    7.25,
                    "2026-04-17T19:56:03.827000+00:00",
                ),
            )
