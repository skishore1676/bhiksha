"""
pricing.py

Helpers for price extraction and trading-related computations.
"""
import re
from typing import Optional, Dict
from config import Config

def get_price_for_entry(quote: Dict) -> Optional[float]:
    """
    For entry, use average of bid+ask prices with ENTRY_PRICE_OFFSET_PERCENTAGE applied.
    Places limit orders at midpoint × (1 - ENTRY_PRICE_OFFSET_PERCENTAGE/100).
    Returns a float or None when no valid bid/ask prices exist.
    """
    if not quote:
        return None
    
    bid = quote.get("bid")
    ask = quote.get("ask")
    
    if bid is not None and ask is not None:
        try:
            bid_price = float(bid)
            ask_price = float(ask)
            # Calculate midpoint of bid and ask
            midpoint = (bid_price + ask_price) / 2.0
            # Apply offset below midpoint for limit order
            offset_pct = Config.ENTRY_PRICE_OFFSET_PERCENTAGE / 100.0
            entry_price = midpoint * (1.0 - offset_pct)
            return entry_price
        except Exception:
            pass
    elif bid is not None:
        try:
            # Fallback to bid-only if ask is unavailable
            bid_price = float(bid)
            offset_pct = Config.ENTRY_PRICE_OFFSET_PERCENTAGE / 100.0
            entry_price = bid_price * (1.0 - offset_pct)
            return entry_price
        except Exception:
            pass
    
    return None

def get_current_price(quote: Dict, fallback: Optional[float] = None) -> Optional[float]:
    """
    For monitoring, prefer last, else mid of bid/ask when both present, else fallback.
    """
    if not quote:
        return fallback
    last = quote.get("last")
    bid = quote.get("bid")
    ask = quote.get("ask")
    try:
        if last is not None:
            return float(last)
        if bid is not None and ask is not None:
            return (float(bid) + float(ask)) / 2.0
    except Exception:
        pass
    return fallback

def round_price(price: float, decimals: int = 2) -> float:
    return round(float(price), decimals)

def compute_allocated_capital(available_capital: float, usage_pct: float) -> float:
    """Allocate a percentage of available capital (usage_pct in percent)."""
    try:
        return float(available_capital) * (float(usage_pct) / 100.0)
    except Exception:
        return 0.0

def compute_option_quantity(capital: float, option_price: float, multiplier: int = 100) -> int:
    """
    DEPRECATED: Use compute_option_quantity_with_limits() instead for capital usage controls.
    
    Compute whole-contract quantity given capital, option price, and contract multiplier.
    This function does NOT apply capital usage percentage limits.
    """
    try:
        if option_price and option_price > 0 and multiplier > 0:
            return int(float(capital) / (float(option_price) * int(multiplier)))
    except Exception:
        pass
    return 0

def compute_option_quantity_with_limits(available_capital: float, option_price: float, 
                                      capital_usage_pct: float, multiplier: int = 100,
                                      logger=None) -> int:
    """
    ⭐ SINGLE SOURCE OF TRUTH for option quantity calculation with capital usage limits.
    
    This is the preferred function for all option quantity calculations as it enforces
    capital usage limits and provides consistent behavior across the application.
    
    Args:
        available_capital: Total available capital
        option_price: Price per option contract
        capital_usage_pct: Percentage of capital to use (e.g., 10.0 for 10%)
        multiplier: Option contract multiplier (default 100)
        logger: Optional logger for warnings
        
    Returns:
        Number of contracts to purchase (0 if insufficient capital)
    """
    try:
        if option_price <= 0:
            return 0
            
        # Calculate capital allocated for this trade
        capital_to_use = compute_allocated_capital(available_capital, capital_usage_pct)
        cost_per_contract = float(option_price) * int(multiplier)
        
        if cost_per_contract > capital_to_use:
            if logger:
                logger.warning(f"Insufficient capital to purchase even one contract. "
                             f"Required: ${cost_per_contract:.2f}, "
                             f"Available for this trade ({capital_usage_pct}% of total): ${capital_to_use:.2f}")
            return 0
            
        quantity = int(capital_to_use / cost_per_contract)
        if logger:
            logger.debug(f"Calculated quantity: {quantity} contracts using {capital_usage_pct}% of capital")
        return quantity
        
    except Exception as e:
        if logger:
            logger.error(f"Error calculating option quantity: {e}")
        return 0

def compute_stop_price(entry_price: float, stop_loss_pct: float, decimals: int = 2) -> float:
    """Compute stop price below entry by stop_loss_pct percent."""
    return round_price(float(entry_price) * (1.0 - float(stop_loss_pct) / 100.0), decimals)

def compute_target_price(entry_price: float, target_pct: float, decimals: int = 2) -> float:
    """Compute target price above entry by target_pct percent."""
    return round_price(float(entry_price) * (1.0 + float(target_pct) / 100.0), decimals)

def get_option_type_from_symbol(symbol: str) -> Optional[str]:
    """
    Extracts the option type ('CALL' or 'PUT') from an OCC option symbol.
    Example: 'IWM250829P00220000' -> 'PUT'
    """
    # Regex to find the P or C before the strike price
    match = re.search(r'([PC])\d{8}$', symbol)
    if match:
        option_code = match.group(1)
        if option_code == 'P':
            return 'PUT'
        elif option_code == 'C':
            return 'CALL'
    return None

def round_to_increment(price: float, increment: float) -> float:
    """
    Round a price to the nearest multiple of the given increment.

    Args:
        price: The price to round
        increment: The increment (tick size) to round to

    Returns:
        The price rounded to the nearest increment
    """
    if increment <= 0:
        raise ValueError("Increment must be positive")

    # Calculate how many increments fit in the price
    increments = round(price / increment)
    # Return the rounded price
    return increments * increment
