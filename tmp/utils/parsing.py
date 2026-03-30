"""
parsing.py

Helpers for parsing shorthand notations and selecting contracts from chains.
"""
from typing import Optional, Dict
import re
from datetime import datetime, timedelta, timezone

from utils.holidays import next_trading_day

class OptionInputParser:
    """
    Parses a simplified option input string (e.g., "aapl1p5" or "spy2c1.5").
    Format: <symbol><weeks_to_expiration><option_type><target_percentage_change>
    - symbol: e.g., 'aapl', 'spy'
    - weeks_to_expiration: integer, e.g., '1' for 1 week from now.
    - option_type: 'c' for call, 'p' for put (case insensitive).
    - target_percentage_change: number (integer or decimal), e.g., '5' for 5% change, '1.3' for 1.3% change, '.7' for 0.7% change.
    """

    def parse_option_string(self, input_string: str) -> Optional[dict]:
        """
        Parses the input string and returns a dictionary with extracted details, or None if parsing fails.
        """
        match = re.match(r"^([a-zA-Z]+)(\d+)([cCpP])(\d+(?:\.\d*)?)([dD]?)$", input_string)
        if not match:
            return None

        symbol, duration_str, option_type, percent_str, time_unit_char = match.groups()

        duration = int(duration_str)
        target_percentage_change = float(percent_str) / 100.0 # 5 -> 0.05, 1.3 -> 0.013, .7 -> 0.007

        # Determine time unit and calculate expiration date
        today = datetime.now(timezone.utc)
        time_unit = "days" if time_unit_char.upper() == "D" else "weeks"

        if time_unit == "days":
            # Calculate expiration for days - skips weekends AND NYSE holidays
            current_date = today.date() if hasattr(today, 'date') else today
            for _ in range(duration):
                current_date = next_trading_day(current_date)

            target_expiration_date = datetime.combine(current_date, datetime.min.time()).replace(tzinfo=timezone.utc)
        else:
            # Original weeks logic: Calculate the target expiration date as Friday of the specified week
            # Find the next upcoming Friday (if today is Friday, it finds next Friday, not today)
            days_until_next_friday = (4 - today.weekday() + 7) % 7
            if days_until_next_friday == 0: # If today is Friday, target next Friday
                days_until_next_friday = 7

            target_friday = today + timedelta(days=days_until_next_friday)
            # Add weeks to expiration (subtract 1 because target_friday is already the first week)
            target_expiration_date = target_friday + timedelta(weeks=duration - 1)

        # Map single-character option type to full word (case insensitive)
        option_type_map = {'c': 'call', 'p': 'put', 'C': 'call', 'P': 'put'}
        full_option_type = option_type_map.get(option_type)
        if not full_option_type:
            raise ValueError(f"Invalid option type: {option_type}. Must be 'c', 'C', 'p', or 'P'.")

        return {
            "symbol": symbol.upper(),
            "expiration_date": target_expiration_date.strftime("%Y-%m-%d"),
            "option_type": full_option_type,
            "target_percentage_change": target_percentage_change,
            "time_unit": time_unit  # Include time unit for clarity
        }

def select_contract_from_chain(chain: Dict, option_type: str, underlying_price: float, target_pct: float) -> Optional[str]:
    """
    From an option chain response, pick the contract symbol closest to a target move from the underlying.
    - option_type: 'call' or 'put'
    - target_pct: decimal (0.10 == 10%) move from underlying for strike selection
    Strategy:
      - For call: target_strike = underlying_price * (1 + target_pct)
      - For put:  target_strike = underlying_price * (1 - target_pct)
      - Choose the contract with strike closest to target_strike.
    Returns OCC symbol string or None.
    """
    if not chain:
        return None
    side_key = 'calls' if option_type == 'call' else 'puts'
    contracts = chain.get(side_key) or []
    if not contracts:
        return None
    try:
        target_strike = underlying_price * (1 + target_pct) if option_type == 'call' else underlying_price * (1 - target_pct)
        best = None
        best_diff = None
        for c in contracts:
            instr = (c or {}).get('instrument', {})
            sym = instr.get('symbol')
            # Strike can be inferred from OCC symbol's last 8 digits/1000
            # e.g., ...00115000 -> 115.000
            if not sym or len(sym) < 15:
                continue
            strike_str = sym[-8:]
            try:
                strike = int(strike_str) / 1000.0
            except Exception:
                continue
            diff = abs(strike - target_strike)
            if best is None or diff < best_diff:
                best = sym
                best_diff = diff
        return best
    except Exception:
        return None
