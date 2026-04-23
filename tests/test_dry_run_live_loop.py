from bhiksha.tools.dry_run_live_loop import _message_level, _runtime_output


def test_runtime_output_filters_debug_lines_by_default(monkeypatch) -> None:
    monkeypatch.delenv("BHIKSHA_RUNTIME_OUTPUT_LEVEL", raising=False)
    lines: list[str] = []
    output = _runtime_output(lines.append)

    output("BAR QQQ 2026-03-30T14:31:00+00:00 close=1.05")
    output("EXECUTION_ENQUEUED QQQ hard_flat_check")
    output("RUNTIME_ISSUE QQQ stage=manual_intrabar error=QQQ quote failed")

    assert lines == ["RUNTIME_ISSUE QQQ stage=manual_intrabar error=QQQ quote failed"]


def test_runtime_output_allows_debug_when_requested(monkeypatch) -> None:
    monkeypatch.setenv("BHIKSHA_RUNTIME_OUTPUT_LEVEL", "DEBUG")
    lines: list[str] = []
    output = _runtime_output(lines.append)

    output("BAR QQQ 2026-03-30T14:31:00+00:00 close=1.05")

    assert lines == ["BAR QQQ 2026-03-30T14:31:00+00:00 close=1.05"]


def test_message_level_classifies_runtime_output() -> None:
    assert _message_level("PROVIDER_BACKOFF provider=schwab symbol=QQQ error=HTTPStatusError('429')") > _message_level(
        "BAR QQQ 2026-03-30T14:31:00+00:00 close=1.05"
    )
    assert _message_level("RUNTIME_ISSUE ALL stage=reconciliation error=boom") > _message_level(
        "PROVIDER_BACKOFF provider=schwab symbol=QQQ error=HTTPStatusError('429')"
    )
