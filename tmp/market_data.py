from src.api_client import PublicAPIClient
import logging
from src.account import get_primary_account_id
from utils.symbols import normalize_option_symbol, normalize_equity_symbol, is_occ_option_symbol
from src.domain.trading.models import Greeks # New import

logger = logging.getLogger(__name__)

async def get_stock_quote(symbol: str) -> float:
    """
    Fetches the current price (last trade price) for a given stock symbol.
    """
    try:
        client = PublicAPIClient()
        account_id = await get_primary_account_id()  # Get account ID here
        logger.debug(f"Attempting to fetch quote for symbol: {symbol}...")

        payload = {
            "instruments": [
                {
                    "symbol": normalize_equity_symbol(symbol),
                    "type": "EQUITY",
                }
            ]
        }

        # Corrected endpoint with accountId in path
        quote_data = await client.post(
            f"/userapigateway/marketdata/{account_id}/quotes", json_data=payload
        )

        if quote_data and quote_data.get("quotes"):
            # Find the quote for the requested symbol
            for quote in quote_data["quotes"]:
                if (
                    quote.get("instrument", {}).get("symbol")
                    == normalize_equity_symbol(symbol)
                ):
                    last_price = quote.get("last")
                    if last_price is not None:
                        logger.debug(
                            f"Successfully fetched quote for {symbol}: {last_price}"
                        )
                        return float(last_price)
            logger.warning(f"Quote for {symbol} not found in response.")
            return None
        else:
            logger.warning(f"No quotes data in response for {symbol}.")
            return None
    except Exception as e:
        logger.error(f"Failed to retrieve quote for {symbol}: {e}")
        raise

async def get_option_quote(symbol: str) -> dict:
    """
    Fetches the current bid/ask/last prices for a given option symbol.
    Returns a dictionary with bid, ask, last, and other quote data.
    """
    try:
        client = PublicAPIClient()
        account_id = await get_primary_account_id()
        logger.debug(f"Attempting to fetch option quote for symbol: {symbol}...")

        sym = normalize_option_symbol(symbol)
        payload = {
            "instruments": [
                {
                    "symbol": sym,
                    "type": "OPTION",
                }
            ]
        }

        quote_data = await client.post(
            f"/userapigateway/marketdata/{account_id}/quotes", json_data=payload
        )

        if quote_data and quote_data.get("quotes"):
            # Find the quote for the requested symbol
            for quote in quote_data["quotes"]:
                if quote.get("instrument", {}).get("symbol") == sym:
                    bid = quote.get("bid")
                    ask = quote.get("ask")
                    last = quote.get("last")
                    outcome = quote.get("outcome")
                    result = {
                        "symbol": symbol,
                        "bid": bid,
                        "ask": ask,
                        "last": last,
                        # Backward-compatible aliases used elsewhere in code
                        "bid_price": bid,
                        "ask_price": ask,
                        "last_trade_price": last,
                        "bidSize": quote.get("bidSize"),
                        "askSize": quote.get("askSize"),
                        "timestamp": quote.get("timestamp"),
                    }
                    if last is None and bid is None and ask is None:
                        logger.warning(f"No usable option quote for {symbol} (outcome={outcome}).")
                    else:
                        logger.debug(
                            f"Successfully fetched option quote for {symbol}: bid={result['bid']}, ask={result['ask']}, last={result['last']}"
                        )
                    return result
            logger.warning(f"Option quote for {symbol} not found in response.")
            return None
        else:
            logger.warning(f"No quotes data in response for option {symbol}.")
            return None
    except Exception as e:
        logger.error(f"Failed to retrieve option quote for {symbol}: {e}")
        return None

async def get_option_chain(symbol: str, expiration_date: str) -> dict:
    """
    Fetch the option chain for an underlying on a given expiration date.
    Returns a dict with keys 'calls' and 'puts' arrays as provided by the API.
    """
    try:
        client = PublicAPIClient()
        account_id = await get_primary_account_id()
        payload = {
            "instrument": {
                "symbol": normalize_equity_symbol(symbol),
                "type": "EQUITY",
            },
            "expirationDate": expiration_date,
        }
        logger.debug(f"Fetching option chain for {symbol} @ {expiration_date} with payload: {payload}")
        data = await client.post(
            f"/userapigateway/marketdata/{account_id}/option-chain", json_data=payload
        )
        return data or {}
    except Exception as e:
        logger.error(f"Failed to fetch option chain for {symbol} {expiration_date} with payload {payload}: {e}")
        return {}

async def get_option_greeks(osi_option_symbol: str) -> Greeks:
    """
    Fetches option greeks for a given OSI option symbol.
    """
    try:
        client = PublicAPIClient()
        account_id = await get_primary_account_id()
        logger.debug(f"Attempting to fetch greeks for OSI option symbol: {osi_option_symbol}...")

        url = f"/userapigateway/option-details/{account_id}/{osi_option_symbol}/greeks"
        data = await client.get(url)

        if data:
            # Convert string values to float and create Greeks object
            # Only include fields that our Greeks class expects
            expected_fields = ['delta', 'gamma', 'theta', 'vega', 'impliedVolatility']
            greeks_data = {k: float(v) for k, v in data.items() if k in expected_fields}
            greeks = Greeks(**greeks_data)
            logger.debug(f"Successfully fetched greeks for {osi_option_symbol}.")
            return greeks
        else:
            logger.warning(f"No greeks data in response for {osi_option_symbol}.")
            return Greeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0, impliedVolatility=0.0) # Return default Greeks object
    except Exception as e:
        logger.error(f"Failed to retrieve greeks for {osi_option_symbol}: {e}")
        return Greeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0, impliedVolatility=0.0) # Return default Greeks object

async def get_batch_quotes(symbols: list) -> dict:
    """
    Fetches quotes for multiple symbols in a single batched API request.
    Accepts a list of symbols and returns a dictionary where keys are the symbols
    and values are the corresponding quote objects from the API response.

    Args:
        symbols: List of stock/option symbols to fetch quotes for

    Returns:
        Dictionary mapping symbols to their quote data objects

    Notes:
        - Handles empty input list by returning an empty dictionary
        - Automatically determines if each symbol is an OPTION or EQUITY using is_occ_option_symbol
        - Uses the existing API endpoint for batched quotes
    """
    # Handle empty input
    if not symbols:
        logger.debug("Empty symbol list provided to get_batch_quotes, returning empty dict")
        return {}

    try:
        client = PublicAPIClient()
        account_id = await get_primary_account_id()
        logger.debug(f"Attempting to fetch batched quotes for symbols: {symbols}...")

        # Construct payload for multiple instruments
        instruments = []
        for symbol in symbols:
            if is_occ_option_symbol(symbol):
                # This is an option symbol
                instrument = {
                    "symbol": normalize_option_symbol(symbol),
                    "type": "OPTION"
                }
            else:
                # This is an equity symbol
                instrument = {
                    "symbol": normalize_equity_symbol(symbol),
                    "type": "EQUITY"
                }
            instruments.append(instrument)

        payload = {"instruments": instruments}

        # Make the batched API call
        quote_data = await client.post(
            f"/userapigateway/marketdata/{account_id}/quotes",
            json_data=payload
        )

        # Process and transform the response
        result = {}
        if quote_data and quote_data.get("quotes"):
            for quote in quote_data["quotes"]:
                # Extract the original symbol from the instrument in the response
                instrument_symbol = quote.get("instrument", {}).get("symbol")
                instrument_type = quote.get("instrument", {}).get("type")

                # Find the original user-provided symbol that matches this response
                matched_original_symbol = None
                for original_symbol in symbols:
                    if instrument_type == "OPTION":
                        if normalize_option_symbol(original_symbol) == instrument_symbol:
                            matched_original_symbol = original_symbol
                            break
                    else:  # EQUITY
                        if normalize_equity_symbol(original_symbol) == instrument_symbol:
                            matched_original_symbol = original_symbol
                            break

                if matched_original_symbol:
                    result[matched_original_symbol] = quote
                    logger.debug(f"Successfully processed quote for {matched_original_symbol}")
                else:
                    logger.warning(f"Could not match response symbol {instrument_symbol} to any requested symbol")

            logger.debug(f"Successfully fetched batched quotes for {len(result)} out of {len(symbols)} symbols")
        else:
            logger.warning("No quotes data in batched response")

        return result

    except Exception as e:
        logger.error(f"Failed to retrieve batched quotes for symbols {symbols}: {e}")
        raise
