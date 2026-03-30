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
