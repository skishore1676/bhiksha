"""Redacted Schwab account and market-data health checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from bhiksha.integrations.schwab.client import SchwabApiClient
from bhiksha.integrations.schwab.settings import SchwabSettings


@dataclass(slots=True)
class SymbolHealth:
    symbol: str
    quote_ok: bool = False
    chain_ok: bool = False
    call_expirations: int = 0
    put_expirations: int = 0
    error: str | None = None


@dataclass(slots=True)
class SchwabHealthResult:
    ok: bool
    linked_account_count: int = 0
    symbols: list[SymbolHealth] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def run_schwab_healthcheck(
    *,
    symbols: tuple[str, ...] = ("QQQ", "IWM"),
    client: SchwabApiClient | None = None,
    settings: SchwabSettings | None = None,
) -> SchwabHealthResult:
    """Prove account, quote, and option-chain access without placing orders."""

    owned_client = client is None
    client = client or SchwabApiClient(settings=settings)
    checks: list[SymbolHealth] = []
    try:
        accounts = await client.linked_accounts()
        account_count = len(accounts) if isinstance(accounts, list) else 0
        if account_count < 1:
            return SchwabHealthResult(ok=False, error="no_linked_accounts")
        for symbol in symbols:
            item = SymbolHealth(symbol=symbol)
            try:
                quote = await client.quote(symbol)
                item.quote_ok = isinstance(quote, dict) and symbol.upper() in {str(key).upper() for key in quote}
                chain = await client.option_chain(symbol, contract_type="ALL", strike_count=5)
                calls = chain.get("callExpDateMap") if isinstance(chain, dict) else None
                puts = chain.get("putExpDateMap") if isinstance(chain, dict) else None
                item.call_expirations = len(calls) if isinstance(calls, dict) else 0
                item.put_expirations = len(puts) if isinstance(puts, dict) else 0
                item.chain_ok = item.call_expirations > 0 and item.put_expirations > 0
            except Exception as exc:  # noqa: BLE001 - receipts must degrade safely.
                item.error = _safe_error(exc)
            checks.append(item)
        ok = all(item.quote_ok and item.chain_ok and item.error is None for item in checks)
        return SchwabHealthResult(ok=ok, linked_account_count=account_count, symbols=checks)
    except Exception as exc:  # noqa: BLE001 - never expose response bodies or tokens.
        return SchwabHealthResult(ok=False, error=_safe_error(exc))
    finally:
        if owned_client:
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - connection cleanup cannot erase the health result.
                pass


def _safe_error(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return f"{type(exc).__name__}:http_{status}" if status is not None else type(exc).__name__
