"""
Shared broker client instance for singleton pattern.
Ensures only one PublicBrokerClient instance exists throughout the application.
"""
from src.brokers.public.adapter import PublicBrokerClient
from src.api_client import PublicAPIClient

# Global singleton instance
_broker_client = None

def get_broker_client() -> PublicBrokerClient:
    """Get the singleton broker client instance."""
    global _broker_client
    if _broker_client is None:
        _broker_client = PublicBrokerClient()
    return _broker_client

async def shutdown_broker_client():
    """Shutdown the broker client gracefully."""
    global _broker_client
    if _broker_client is not None:
        await _broker_client.close()
        _broker_client = None
    
    # Also shutdown the API client
    api_client = PublicAPIClient()
    await api_client.close()
