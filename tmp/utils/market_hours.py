"""
market_hours.py

Central utility for handling market hours and timezone conversions.
Ensures all trading logic respects US/Eastern time regardless of server location.
"""
from datetime import datetime, time, timedelta, timezone
import pytz
from config import Config

# Constants
MARKET_TZ = pytz.timezone('US/Eastern')
MARKET_OPEN_TIME = time(9, 30)
MARKET_CLOSE_TIME = time(16, 00)

class MarketHours:
    """
    Utility for checking market status and handling timezones.
    """
    
    @staticmethod
    def get_market_time() -> datetime:
        """Returns the current time in US/Eastern timezone."""
        return datetime.now(MARKET_TZ)

    @staticmethod
    def is_market_open(use_buffer: bool = False) -> bool:
        """
        Check if the market is currently open.
        
        Args:
            use_buffer: If True, applies the configured start/end buffers.
                        (e.g., skip first 5 mins and last 5 mins)
        
        Returns:
            True if market is open (and within buffers if requested), False otherwise.
        """
        now = MarketHours.get_market_time()
        
        # 1. Check Weekend
        if now.weekday() >= 5:  # 5=Saturday, 6=Sunday
            return False
            
        # 2. Check Time Boundaries
        current_time = now.time()
        
        start_time = MARKET_OPEN_TIME
        end_time = MARKET_CLOSE_TIME
        
        if use_buffer:
            # Add buffers if requested (e.g., for new entries)
            # Default to 0 if not configured
            start_buffer = getattr(Config, 'MARKET_OPEN_BUFFER_SECONDS', 0)
            end_buffer = getattr(Config, 'MARKET_CLOSE_BUFFER_SECONDS', 0)
            
            # Adjust start time
            if start_buffer > 0:
                # Naive addition to time object requires dummy datetime
                dummy_dt = datetime.combine(datetime.today(), start_time) + timedelta(seconds=start_buffer)
                start_time = dummy_dt.time()
                
            # Adjust end time
            if end_buffer > 0:
                dummy_dt = datetime.combine(datetime.today(), end_time) - timedelta(seconds=end_buffer)
                end_time = dummy_dt.time()
        
        # Check if within hours
        return start_time <= current_time <= end_time

    @staticmethod
    def seconds_until_open() -> int:
        """
        Calculate seconds until the next market open.
        """
        now = MarketHours.get_market_time()
        today_open = MARKET_TZ.localize(datetime.combine(now.date(), MARKET_OPEN_TIME))
        
        if now < today_open:
            # Market opens later today
            return int((today_open - now).total_seconds())
        else:
            # Market already opened today, calculate for tomorrow (or Monday)
            next_day = now + timedelta(days=1)
            # Skip weekends
            while next_day.weekday() >= 5:
                next_day += timedelta(days=1)
            
            next_open = MARKET_TZ.localize(datetime.combine(next_day.date(), MARKET_OPEN_TIME))
            return int((next_open - now).total_seconds())
