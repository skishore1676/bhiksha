import asyncio

from bhiksha.tools.schwab_healthcheck import run_schwab_healthcheck


class _Client:
    async def linked_accounts(self):
        return [{"hashValue": "never surfaced"}]

    async def quote(self, symbol):
        return {symbol: {"quote": {"lastPrice": 1}}}

    async def option_chain(self, symbol, **kwargs):
        return {"callExpDateMap": {"2026-07-17:3": {}}, "putExpDateMap": {"2026-07-17:3": {}}}


def test_schwab_healthcheck_requires_accounts_quotes_and_both_chain_sides() -> None:
    result = asyncio.run(run_schwab_healthcheck(client=_Client()))

    assert result.ok is True
    assert result.linked_account_count == 1
    assert [item.symbol for item in result.symbols] == ["QQQ", "IWM"]
    assert all(item.quote_ok and item.chain_ok for item in result.symbols)
    assert "never surfaced" not in str(result.to_dict())


class _BrokenChainClient(_Client):
    async def option_chain(self, symbol, **kwargs):
        return {"callExpDateMap": {}, "putExpDateMap": {}}


def test_schwab_healthcheck_fails_closed_on_empty_chain() -> None:
    result = asyncio.run(run_schwab_healthcheck(client=_BrokenChainClient()))

    assert result.ok is False
    assert all(item.chain_ok is False for item in result.symbols)
