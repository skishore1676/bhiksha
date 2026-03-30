import json
import asyncio
from typing import List, Optional, TYPE_CHECKING
from sqlalchemy import create_engine, Column, String, Text, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import text
from src.domain.trading.models import Greeks, TradeState
from config import Config
from datetime import datetime
import logging

if TYPE_CHECKING:
    from src.trading import Trade

logger = logging.getLogger(__name__)

Base = declarative_base()

class TradeModel(Base):
    __tablename__ = 'trades'
    
    trade_id = Column(String, primary_key=True)
    option_string = Column(String, nullable=False)
    state = Column(String, nullable=False)
    last_state = Column(String, nullable=True)
    position_data = Column(Text, nullable=True)  # JSON string
    entry_order_id = Column(String, nullable=True)
    stop_loss_order_id = Column(String, nullable=True)
    target_1_order_id = Column(String, nullable=True)
    target_2_order_id = Column(String, nullable=True)
    square_off_order_id = Column(String, nullable=True)
    error = Column(Text, nullable=True)
    initial_greeks = Column(Text, nullable=True)  # JSON string
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class DbTradeRepository:
    def __init__(self):
        self.database_url = Config.DATABASE_URL
        # For SQLite, we need to convert to async URL
        if self.database_url.startswith("sqlite:///"):
            # For aiosqlite, the URL should be sqlite+aiosqlite:///
            self.async_database_url = self.database_url.replace("sqlite:///", "sqlite+aiosqlite:///")
        else:
            self.async_database_url = self.database_url
            
        self.engine = create_async_engine(self.async_database_url)
        self.async_session = async_sessionmaker(self.engine, expire_on_commit=False)
        
        # Create tables synchronously
        self._init_db()
        
    def _init_db(self):
        # Create a synchronous engine for table creation
        sync_database_url = self.database_url
        if sync_database_url.startswith("sqlite+aiosqlite:///"):
            sync_database_url = sync_database_url.replace("sqlite+aiosqlite:///", "sqlite:///")
            
        sync_engine = create_engine(sync_database_url)
        Base.metadata.create_all(sync_engine)
        sync_engine.dispose()

    async def save(self, trade: 'Trade'):
        """Save a trade to the database."""
        try:
            async with self.async_session() as session:
                # Check if trade already exists
                result = await session.execute(
                    text("SELECT trade_id FROM trades WHERE trade_id = :trade_id"),
                    {"trade_id": trade.trade_id}
                )
                existing = result.fetchone()
                
                # Convert trade to model
                trade_model = TradeModel(
                    trade_id=trade.trade_id,
                    option_string=trade.option_string,
                    state=trade.state.name,
                    last_state=trade.last_state,
                    position_data=json.dumps(trade.position_data) if trade.position_data else None,
                    entry_order_id=trade.entry_order_id,
                    stop_loss_order_id=trade.stop_loss_order_id,
                    target_1_order_id=trade.target_1_order_id,
                    target_2_order_id=trade.target_2_order_id,
                    square_off_order_id=trade.square_off_order_id,
                    error=trade.error,
                    initial_greeks=json.dumps(trade.initial_greeks.to_dict()) if trade.initial_greeks else None
                )
                
                if existing:
                    # Update existing record
                    await session.merge(trade_model)
                else:
                    # Insert new record
                    session.add(trade_model)
                
                await session.commit()
                symbol = trade.option_string or trade.trade_id
                logger.debug(f"Trade for {symbol} saved to database")
        except Exception as e:
            logger.error(f"Failed to save trade for {trade.option_string or trade.trade_id} to database: {e}")
            raise

    async def load(self, trade_id: str) -> Optional['Trade']:
        """Load a trade from the database."""
        try:
            async with self.async_session() as session:
                result = await session.execute(
                    text("SELECT * FROM trades WHERE trade_id = :trade_id"),
                    {"trade_id": trade_id}
                )
                row = result.fetchone()
                
                if not row:
                    return None
                
                # Convert row to Trade object
                from src.trading import Trade  # Import here to avoid circular import
                trade = Trade(
                    option_string=row.option_string,
                    trade_id=row.trade_id,
                    state=TradeState[row.state],
                    initial_greeks=None  # Will set this separately
                )
                
                # Set attributes that aren't in the constructor
                trade.last_state = row.last_state
                trade.position_data = json.loads(row.position_data) if row.position_data else {}
                trade.entry_order_id = row.entry_order_id
                trade.stop_loss_order_id = row.stop_loss_order_id
                trade.target_1_order_id = row.target_1_order_id
                trade.target_2_order_id = row.target_2_order_id
                trade.square_off_order_id = row.square_off_order_id
                trade.error = row.error
                
                # Restore initial_greeks if available
                if row.initial_greeks:
                    greeks_dict = json.loads(row.initial_greeks)
                    trade.initial_greeks = Greeks(**greeks_dict)
                
                return trade
        except Exception as e:
            logger.error(f"Failed to load trade {trade_id} from database: {e}")
            return None

    async def archive(self, trade: 'Trade'):
        """Archive a trade (in this implementation, we'll just keep it in the same table)."""
        # In a more sophisticated implementation, you might move archived trades to a separate table
        # For now, we'll just ensure it's saved with its final state
        await self.save(trade)
        symbol = trade.option_string or trade.trade_id
        logger.info(f"Trade for {symbol} archived (state: {trade.state.name})")

    async def list_active(self) -> List['Trade']:
        """List all active trades."""
        try:
            async with self.async_session() as session:
                # Get all trades that are not in terminal states
                terminal_states = [TradeState.POSITION_CLOSED.name, TradeState.ERROR.name, TradeState.CANCELLED.name]
                result = await session.execute(
                    text("SELECT * FROM trades WHERE state NOT IN :terminal_states"),
                    {"terminal_states": tuple(terminal_states)}
                )
                rows = result.fetchall()
                
                from src.trading import Trade  # Import here to avoid circular import
                trades = []
                for row in rows:
                    trade = Trade(
                        option_string=row.option_string,
                        trade_id=row.trade_id,
                        state=TradeState[row.state],
                        initial_greeks=None  # Will set this separately
                    )
                    
                    # Set attributes that aren't in the constructor
                    trade.last_state = row.last_state
                    trade.position_data = json.loads(row.position_data) if row.position_data else {}
                    trade.entry_order_id = row.entry_order_id
                    trade.stop_loss_order_id = row.stop_loss_order_id
                    trade.target_1_order_id = row.target_1_order_id
                    trade.target_2_order_id = row.target_2_order_id
                    trade.square_off_order_id = row.square_off_order_id
                    trade.error = row.error
                    if row.initial_greeks:
                        greeks_dict = json.loads(row.initial_greeks)
                        trade.initial_greeks = Greeks(**greeks_dict)
                    
                    trades.append(trade)
                
                return trades
        except Exception as e:
            logger.error(f"Failed to list active trades from database: {e}")
            return []
            
    def list_active_ids(self) -> List[str]:
        """List all active trade IDs."""
        try:
            # Create a synchronous engine for this operation
            sync_database_url = self.database_url
            if sync_database_url.startswith("sqlite+aiosqlite:///"):
                sync_database_url = sync_database_url.replace("sqlite+aiosqlite:///", "sqlite:///")
                
            sync_engine = create_engine(sync_database_url)
            with sync_engine.connect() as conn:
                # Include ERROR trades so they can be retried from the frontend
                result = conn.execute(text("SELECT trade_id FROM trades WHERE state NOT IN ('POSITION_CLOSED', 'CANCELLED')"))
                ids = [row[0] for row in result.fetchall()]
            sync_engine.dispose()
            return ids
        except Exception as e:
            logger.error(f"Failed to list active trade IDs from database: {e}")
            return []
    
    async def get_trades_by_date(self, target_date) -> List['Trade']:
        """
        Get all trades closed on a specific date (in local timezone).
        Used by Risk Manager to calculate daily statistics.
        
        Args:
            target_date: date object to filter by (assumes local timezone)
        
        Returns:
            List of closed Trade objects that were updated (closed) on the specified date
        """
        try:
            from datetime import datetime
            import pytz
            
            # Use local timezone (America/Chicago)
            local_tz = pytz.timezone("America/Chicago")
            
            # Create start and end of day in local timezone
            start_of_day_local = local_tz.localize(datetime.combine(target_date, datetime.min.time()))
            end_of_day_local = local_tz.localize(datetime.combine(target_date, datetime.max.time()))
            
            # Convert to UTC for database query (database stores in UTC)
            start_of_day_utc = start_of_day_local.astimezone(pytz.utc)
            end_of_day_utc = end_of_day_local.astimezone(pytz.utc)
            
            async with self.async_session() as session:
                # Filter by updated_at (when trade was closed) AND state = POSITION_CLOSED
                result = await session.execute(
                    text("SELECT * FROM trades WHERE state = 'POSITION_CLOSED' AND updated_at >= :start_date AND updated_at <= :end_date ORDER BY updated_at ASC"),
                    {"start_date": start_of_day_utc.replace(tzinfo=None), "end_date": end_of_day_utc.replace(tzinfo=None)}
                )
                rows = result.fetchall()
                
                from src.trading import Trade  # Import here to avoid circular import
                trades = []
                for row in rows:
                    trade = Trade(
                        option_string=row.option_string,
                        trade_id=row.trade_id,
                        state=TradeState[row.state],
                        initial_greeks=None
                    )
                    
                    # Set attributes
                    trade.last_state = row.last_state
                    trade.position_data = json.loads(row.position_data) if row.position_data else {}
                    trade.entry_order_id = row.entry_order_id
                    trade.stop_loss_order_id = row.stop_loss_order_id
                    trade.target_1_order_id = row.target_1_order_id
                    trade.target_2_order_id = row.target_2_order_id
                    trade.square_off_order_id = row.square_off_order_id
                    trade.error = row.error
                    
                    if row.initial_greeks:
                        greeks_dict = json.loads(row.initial_greeks)
                        trade.initial_greeks = Greeks(**greeks_dict)
                    
                    trades.append(trade)
                
                logger.debug(f"Retrieved {len(trades)} trades for date {target_date}")
                return trades
                
        except Exception as e:
            logger.error(f"Failed to get trades by date {target_date}: {e}", exc_info=True)
            return []
    
    async def get_active_trades(self) -> List['Trade']:
        """
        Get all currently active (non-closed) trades with real broker positions.
        Used by Risk Manager to count open positions.
        
        Excludes:
        - IDLE (never sent to broker)
        - ERROR (failed trades)
        - POSITION_CLOSED (completed)
        
        Returns:
            List of Trade objects in active states with broker positions
        """
        try:
            async with self.async_session() as session:
                # Only count trades that have active broker positions
                # Exclude: IDLE (not started), ERROR (failed), POSITION_CLOSED (done), CANCELLED
                inactive_states = ('IDLE', 'AWAITING_CAPITAL','ERROR', 'POSITION_CLOSED', 'CANCELLED')
                
                # Use IN with explicit values for SQLite compatibility
                result = await session.execute(
                    text("SELECT * FROM trades WHERE state NOT IN ('IDLE','AWAITING_CAPITAL', 'ERROR', 'POSITION_CLOSED', 'CANCELLED')")
                )
                rows = result.fetchall()
                
                from src.trading import Trade  # Import here to avoid circular import
                trades = []
                for row in rows:
                    trade = Trade(
                        option_string=row.option_string,
                        trade_id=row.trade_id,
                        state=TradeState[row.state],
                        initial_greeks=None
                    )
                    
                    # Set attributes
                    trade.last_state = row.last_state
                    trade.position_data = json.loads(row.position_data) if row.position_data else {}
                    trade.entry_order_id = row.entry_order_id
                    trade.stop_loss_order_id = row.stop_loss_order_id
                    trade.target_1_order_id = row.target_1_order_id
                    trade.target_2_order_id = row.target_2_order_id
                    trade.square_off_order_id = row.square_off_order_id
                    trade.error = row.error
                    
                    if row.initial_greeks:
                        greeks_dict = json.loads(row.initial_greeks)
                        trade.initial_greeks = Greeks(**greeks_dict)
                    
                    trades.append(trade)
                
                logger.debug(f"Retrieved {len(trades)} active trades")
                return trades
                
        except Exception as e:
            logger.error(f"Failed to get active trades: {e}", exc_info=True)
            return []
