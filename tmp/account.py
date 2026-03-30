import json
import os
import logging
from typing import Optional
from src.api_client import PublicAPIClient
from utils.cache_manager import get_cache_manager, CacheType

logger = logging.getLogger(__name__)

# Construct a robust, absolute path to the account info file
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
ACCOUNT_INFO_FILE = os.path.join(PROJECT_ROOT, 'config', 'account_info.json')

async def _read_cached_account_id() -> Optional[str]:
    """
    Reads the cached account ID from the account info file.
    Returns the account ID if found and valid, otherwise None.
    """
    if not os.path.exists(ACCOUNT_INFO_FILE):
        return None
    try:
        with open(ACCOUNT_INFO_FILE, 'r') as f:
            data = json.load(f)
            return data.get("accountId")
    except (json.JSONDecodeError, KeyError):
        logger.warning("Account info file is corrupted or invalid. Will re-fetch.")
        return None
    except Exception as e:
        logger.error(f"An unexpected error occurred while reading cached account ID: {e}")
        return None

async def _write_cached_account_id(account_id: str):
    """
    Writes the primary account ID to the account info file.
    """
    try:
        with open(ACCOUNT_INFO_FILE, 'w') as f:
            json.dump({"accountId": account_id}, f, indent=4)
        logger.debug(f"Cached account ID {account_id} to {ACCOUNT_INFO_FILE}")
    except Exception as e:
        logger.error(f"Failed to write cached account ID to file: {e}")

async def get_primary_account_id() -> str:
    """
    Retrieves the primary account ID, using a cached value if available and valid.
    If not cached, fetches it from the API and caches it.
    """
    account_id = await _read_cached_account_id()
    if account_id:
        logger.debug(f"Using cached primary account ID: {account_id}")
        return account_id

    logger.debug("Cached account ID not found or invalid. Fetching from API...")
    try:
        client = PublicAPIClient()
        # This endpoint returns a list of accounts, we take the first one.
        # The structure is {"accounts": [{"accountId": "...", ...}]}
        all_accounts_data = await client.get("/userapigateway/trading/account")
        
        accounts = all_accounts_data.get("accounts", [])
        if not accounts:
            raise ValueError("No accounts found for this API key.")
        
        primary_account_id = accounts[0].get("accountId")
        if not primary_account_id:
            raise ValueError("Primary account ID not found in API response.")

        await _write_cached_account_id(primary_account_id)
        logger.debug(f"Fetched and cached primary account ID: {primary_account_id}")
        return primary_account_id
    except Exception as e:
        logger.error(f"Failed to retrieve primary account ID from API: {e}")
        raise

async def get_account_details() -> dict:
    """
    Fetches the comprehensive account details for the authenticated user.
    This function now uses the cached primary account ID.
    """
    try:
        primary_account_id = await get_primary_account_id()
        return await get_account_portfolio(primary_account_id)
    except Exception as e:
        logger.error(f"Failed to retrieve account details: {e}")
        raise

async def get_account_portfolio(account_id: str) -> dict:
    """
    Fetches the entire portfolio for a given account ID using the server-side cache.

    Uses CacheType.BACKEND_DECISIONS so trading logic gets fresher data (short TTL)
    while reducing duplicate broker API calls.
    """
    cache_manager = get_cache_manager()
    cache_key = f"portfolio_{account_id}"

    async def _fetch_portfolio():
        client = PublicAPIClient()
        logger.debug(f"Fetching portfolio for account {account_id} from broker API...")
        portfolio_data = await client.get(f"/userapigateway/trading/{account_id}/portfolio/v2")
        logger.debug("Successfully fetched portfolio from broker API.")
        return portfolio_data

    try:
        return await cache_manager.get_or_fetch(
            cache_key=cache_key,
            fetch_func=_fetch_portfolio,
            cache_type=CacheType.BACKEND_DECISIONS,
            force_refresh=False,
        )
    except Exception as e:
        logger.error(f"Failed to retrieve portfolio for account {account_id}: {e}")
        raise

def get_options_buying_power(portfolio: dict) -> float:
    """
    Extracts the options buying power from a portfolio object.
    """
    options_bp_str = portfolio.get("buyingPower", {}).get("optionsBuyingPower")
    if options_bp_str is not None:
        return float(options_bp_str)
    return 0.0

def get_positions(portfolio: dict) -> list:
    """
    Extracts the list of positions from a portfolio object.
    """
    return portfolio.get("positions", [])

def get_open_orders(portfolio: dict) -> list:
    """
    Extracts the list of open orders from a portfolio object.
    """
    return portfolio.get("orders", [])

async def get_available_capital():
    """
    Fetches the available capital (total equity) from the user's account.
    """
    try:
        available_capital = await get_total_equity()
        logger.debug(f"Available capital (total equity): ${available_capital:.2f}")
        return available_capital
    except Exception as e:
        logger.error(f"Error fetching available capital: {e}")
        return 0

from config import Config
from utils.pricing import compute_option_quantity_with_limits

async def get_total_equity() -> float:
    """
    Fetches the total portfolio equity from the account data.

    Returns:
        float: The total portfolio equity value
    """
    try:
        portfolio = await get_account_details()
        equity_list = portfolio.get("equity", [])

        for entry in equity_list:
            if entry.get("type") == "CASH":
                total_equity = float(entry.get("value"))
                logger.debug(f"Returning total portfolio equity: {total_equity}")
                return total_equity

        raise ValueError("CASH equity entry not found in portfolio data")
    except Exception as e:
        logger.error(f"Failed to retrieve total equity: {e}")
        raise

async def calculate_order_quantity(available_capital: float, option_price: float):
    """
    Calculate option order quantity using configured capital usage limits.

    This is a convenience wrapper around the unified pricing utility function
    that automatically applies the CAPITAL_USAGE_PERCENTAGE from config.

    For direct control over capital usage percentage, use:
    utils.pricing.compute_option_quantity_with_limits() directly.

    Args:
        available_capital: Total available capital
        option_price: Price per option contract

    Returns:
        Number of contracts to purchase (0 if insufficient capital)
    """
    return compute_option_quantity_with_limits(
        available_capital=available_capital,
        option_price=option_price,
        capital_usage_pct=Config.CAPITAL_USAGE_PERCENTAGE,
        logger=logger
    )
