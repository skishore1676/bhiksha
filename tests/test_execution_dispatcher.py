import asyncio

from bhiksha.app.execution_dispatcher import SymbolExecutionDispatcher


def test_symbol_execution_dispatcher_deduplicates_pending_keys() -> None:
    dispatcher = SymbolExecutionDispatcher()

    async def run() -> None:
        dispatcher.start(["QQQ"])
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[str] = []

        async def slow_task() -> None:
            calls.append("first")
            started.set()
            await release.wait()

        accepted_first = dispatcher.submit("QQQ", key="entry:qqq", runner=slow_task)
        await started.wait()
        accepted_second = dispatcher.submit("QQQ", key="entry:qqq", runner=slow_task)
        release.set()
        await asyncio.sleep(0)
        await dispatcher.stop()

        assert accepted_first is True
        assert accepted_second is False
        assert calls == ["first"]

    asyncio.run(run())


def test_symbol_execution_dispatcher_runs_symbols_independently() -> None:
    dispatcher = SymbolExecutionDispatcher()

    async def run() -> None:
        dispatcher.start(["QQQ", "SPY"])
        qqq_started = asyncio.Event()
        spy_done = asyncio.Event()
        release_qqq = asyncio.Event()
        order: list[str] = []

        async def qqq_task() -> None:
            order.append("qqq_start")
            qqq_started.set()
            await release_qqq.wait()
            order.append("qqq_done")

        async def spy_task() -> None:
            order.append("spy_done")
            spy_done.set()

        assert dispatcher.submit("QQQ", key="entry:qqq", runner=qqq_task) is True
        await qqq_started.wait()
        assert dispatcher.submit("SPY", key="entry:spy", runner=spy_task) is True
        await asyncio.wait_for(spy_done.wait(), timeout=1)
        release_qqq.set()
        await asyncio.sleep(0)
        await dispatcher.stop()

        assert order[0] == "qqq_start"
        assert "spy_done" in order
        assert order[-1] == "qqq_done"

    asyncio.run(run())


def test_symbol_execution_dispatcher_recovers_after_task_failure() -> None:
    dispatcher = SymbolExecutionDispatcher()

    async def run() -> None:
        dispatcher.start(["QQQ"])
        succeeded = asyncio.Event()

        async def failing_task() -> None:
            raise RuntimeError("boom")

        async def succeeding_task() -> None:
            succeeded.set()

        assert dispatcher.submit("QQQ", key="entry:qqq", runner=failing_task) is True
        await asyncio.sleep(0)
        assert dispatcher.submit("QQQ", key="manage:qqq", runner=succeeding_task) is True
        await asyncio.wait_for(succeeded.wait(), timeout=1)
        await dispatcher.stop()

    asyncio.run(run())


def test_duplicate_entry_retains_original_signal_identity():
    async def run():
        dispatcher=SymbolExecutionDispatcher();dispatcher.start(['SPY'])
        release=asyncio.Event()
        assert dispatcher.submit('SPY',key='entry:lane',runner=release.wait,identity='first')
        assert not dispatcher.submit('SPY',key='entry:lane',runner=release.wait,identity='second')
        assert dispatcher.pending_identity('SPY','entry:lane')=='first'
        release.set();await dispatcher.stop()
        assert dispatcher.pending_identity('SPY','entry:lane') is None
    asyncio.run(run())


def test_coalesced_signal_has_terminal_outcome_and_original_link():
    from types import SimpleNamespace
    from datetime import UTC,datetime
    from unittest.mock import AsyncMock
    from bhiksha.app.runtime import record_coalesced_signal
    from bhiksha.domain.enums import SignalDirection
    async def run():
        dispatcher=SymbolExecutionDispatcher();dispatcher.start(['SPY'])
        release=asyncio.Event()
        dispatcher.submit('SPY',key='entry:lane',runner=release.wait,identity='first')
        events=SimpleNamespace(append=AsyncMock())
        deployment=SimpleNamespace(deployment_id='lane',symbol='SPY',execution=SimpleNamespace(shadow_only=True))
        decision=SimpleNamespace(timestamp=datetime.now(UTC),direction=SignalDirection.LONG)
        await record_coalesced_signal(events,dispatcher,deployment,decision)
        event,payload=events.append.await_args.args
        assert event=='signal_outcome' and payload['outcome']=='existing_position_block'
        assert payload['pending_signal_id']=='first' and payload['signal_id']!='first'
        release.set();await dispatcher.stop()
    asyncio.run(run())
