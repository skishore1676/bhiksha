from contextlib import contextmanager
from pathlib import Path

from bhiksha.tools import dry_run_live_loop


def test_trade_session_participates_in_runtime_control_lock(tmp_path, monkeypatch) -> None:
    pid_path = tmp_path / "bhiksha.pid"
    observed: list[Path] = []

    @contextmanager
    def fake_lock(path: Path):
        observed.append(path)
        yield path.with_name("bhiksha.control.lock")

    async def fake_run(max_bars, live, active_plan):
        assert max_bars == 0
        assert live is True
        assert active_plan == "plan.json"

    monkeypatch.setenv("BHIKSHA_RUNTIME_PID_PATH", str(pid_path))
    monkeypatch.setattr(dry_run_live_loop, "runtime_control_lock", fake_lock)
    monkeypatch.setattr(dry_run_live_loop, "_run", fake_run)

    result = dry_run_live_loop.main(
        ["--live", "--max-bars", "0", "--active-plan", "plan.json"]
    )

    assert result == 0
    assert observed == [pid_path]
