from __future__ import annotations
import time
import asyncio
import logging
import httpx
from typing import Optional
from config import Config
from .token_store import read_session, write_session

logger = logging.getLogger(__name__)


async def refresh_and_store_access_token() -> str:
    """
    Refresh access token using the secret token.
    """
    
    payload = {"secret": Config.PUBLIC_SECRET_TOKEN, "validityInMinutes": 60}
    
    # Use httpx directly to avoid circular import
    async with httpx.AsyncClient() as client:
        try:
            # Small delay before auth calls to be respectful
            await asyncio.sleep(0.2)
            resp = await client.post(Config.PUBLIC_AUTH_ENDPOINT, json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"Auth token refresh failed: {e}")
            raise
    
    token = data.get("accessToken")
    expires_in = data.get("expiresIn") or 3600
    if not token:
        raise ValueError("Access token not found in authentication response")
    
    expiration_time = int(time.time()) + int(expires_in)
    write_session({"access_token": token, "expiration_timestamp": expiration_time})
    
    logger.info(f"Access token refreshed successfully. Expires at: {time.ctime(expiration_time)}")
    return token


async def get_access_token() -> str:
    """
    Get a valid access token, refreshing if necessary.
    """
    s = read_session()
    if not s:
        logger.debug("No session found, refreshing token")
        return await refresh_and_store_access_token()
    
    try:
        current_time = time.time()
        expiration_time = s.get("expiration_timestamp", 0)
        
        # Check if token is expired or will expire in next 5 minutes
        time_until_expiry = expiration_time - current_time
        if time_until_expiry <= 300:  # 5 minutes buffer
            logger.info(f"Token expires in {time_until_expiry:.1f}s, refreshing")
            return await refresh_and_store_access_token()
        
        logger.debug(f"Using cached token (expires in {time_until_expiry:.1f}s)")
        return s["access_token"]
    except Exception as e:
        logger.warning(f"Error checking token validity: {e}, refreshing")
        return await refresh_and_store_access_token()
