import sqlite3
import json
import logging
import asyncio
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

class GdsRepository:
    def __init__(self, db_path: str = 'gds_history.db'):
        self.db_path = db_path
        self._initialized = False
        # Cache last GDS score per trade for slope calculation
        self._last_gds: Dict[str, float] = {}

    def _init_db_sync(self):
        try:
            with sqlite3.connect(self.db_path) as db:
                db.execute('''
                    CREATE TABLE IF NOT EXISTS gds_snapshots (
                        snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        trade_id TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        state TEXT NOT NULL,
                        timestamp INTEGER NOT NULL,
                        current_price REAL,
                        gds_score REAL,
                        delta REAL,
                        gamma REAL,
                        theta REAL,
                        vega REAL,
                        implied_volatility REAL,
                        delta_dev REAL,
                        gamma_dev REAL,
                        theta_dev REAL,
                        vega_dev REAL,
                        entry_price REAL,
                        pnl_pct REAL,
                        gds_slope REAL,
                        underlying_price REAL
                    )
                ''')
                # Index for fast querying by trade_id
                db.execute('CREATE INDEX IF NOT EXISTS idx_gds_trade_id ON gds_snapshots(trade_id)')
                # Index for timeseries queries
                db.execute('CREATE INDEX IF NOT EXISTS idx_gds_timestamp ON gds_snapshots(timestamp)')
                db.commit()

            # Run migrations for existing databases
            self._migrate_schema_sync()
            logger.debug("Initialized GDS Telemetry database schema.")
        except Exception as e:
            logger.error(f"Failed to initialize GDS database: {e}")
            raise

    def _migrate_schema_sync(self):
        """Add new columns to existing databases that don't have them yet."""
        new_columns = [
            ('entry_price', 'REAL'),
            ('pnl_pct', 'REAL'),
            ('gds_slope', 'REAL'),
            ('underlying_price', 'REAL'),
        ]
        with sqlite3.connect(self.db_path) as db:
            # Get existing columns
            cursor = db.execute('PRAGMA table_info(gds_snapshots)')
            existing_cols = {row[1] for row in cursor.fetchall()}

            for col_name, col_type in new_columns:
                if col_name not in existing_cols:
                    db.execute(f'ALTER TABLE gds_snapshots ADD COLUMN {col_name} {col_type}')
                    logger.info(f"Migrated GDS schema: added column '{col_name}'")
            db.commit()

    async def initialize(self):
        """Initialize the database schema if it doesn't exist."""
        if self._initialized:
            return
        await asyncio.to_thread(self._init_db_sync)
        self._initialized = True

    def _log_snapshot_sync(self, trade_id: str, symbol: str, state: str, gds_data: Dict[str, Any]):
        current_greeks = gds_data.get('current_greeks', {})
        deviations = gds_data.get('greek_deviations', {})

        # Calculate PnL % if entry_price is available
        # Defensive cast: broker API may return string values
        try:
            entry_price = float(gds_data['entry_price']) if gds_data.get('entry_price') is not None else None
        except (ValueError, TypeError):
            entry_price = None
        try:
            current_price = float(gds_data['current_price']) if gds_data.get('current_price') is not None else None
        except (ValueError, TypeError):
            current_price = None
        underlying_price = gds_data.get('underlying_price')
        pnl_pct = None
        if entry_price and entry_price > 0 and current_price is not None:
            pnl_pct = ((current_price - entry_price) / entry_price) * 100

        # Calculate GDS slope (change from previous snapshot for this trade)
        gds_score = gds_data.get('gds_score')
        gds_slope = None
        if gds_score is not None and trade_id in self._last_gds:
            gds_slope = gds_score - self._last_gds[trade_id]
        if gds_score is not None:
            self._last_gds[trade_id] = gds_score

        with sqlite3.connect(self.db_path) as db:
            db.execute('''
                INSERT INTO gds_snapshots (
                    trade_id, symbol, state, timestamp, current_price, gds_score,
                    delta, gamma, theta, vega, implied_volatility,
                    delta_dev, gamma_dev, theta_dev, vega_dev,
                    entry_price, pnl_pct, gds_slope, underlying_price
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                trade_id,
                symbol,
                state,
                gds_data.get('timestamp', 0),
                current_price,
                gds_score,
                current_greeks.get('delta'),
                current_greeks.get('gamma'),
                current_greeks.get('theta'),
                current_greeks.get('vega'),
                current_greeks.get('impliedVolatility'),
                deviations.get('delta'),
                deviations.get('gamma'),
                deviations.get('theta'),
                deviations.get('vega'),
                entry_price,
                pnl_pct,
                gds_slope,
                underlying_price,
            ))
            db.commit()

    async def log_snapshot(self, trade_id: str, symbol: str, state: str, gds_data: Dict[str, Any]):
        """
        Record a snapshot of the current GDS and Greek state.
        Fails silently to avoid breaking the main engine.
        """
        try:
            if not self._initialized:
                await self.initialize()

            await asyncio.to_thread(self._log_snapshot_sync, trade_id, symbol, state, gds_data)
            logger.debug(f"Logged GDS snapshot for {symbol}")
        except Exception as e:
            logger.error(f"Non-fatal error logging GDS snapshot for {trade_id}: {e}")

    def clear_slope_cache(self, trade_id: str):
        """Remove a trade from the slope cache (e.g., after trade closes)."""
        self._last_gds.pop(trade_id, None)

# Global instance for easy import
GDS_REPO = GdsRepository()
