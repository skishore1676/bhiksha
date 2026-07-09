"""Session-report -> Obsidian coding-agent review surface wiring (#6).

These exercise the launchd session-report job's projection helper
(`_publish_session_report_review`) directly, with the Lathi Bus publish call
stubbed, so we assert the route/profile/artifact plumbing and the
graceful-degradation contract without standing up a runtime or the bus.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from bhiksha.ops.alerts import ReviewPublishResult
from bhiksha.ops.daily_report import DailyReportWriteResult
from bhiksha.tools import launchd_job


def _args(**overrides) -> argparse.Namespace:
    base = {
        "obsidian_review_mode": "on",
        "obsidian_review_profile": "coding-agent-northstar",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _result(tmp_path: Path) -> DailyReportWriteResult:
    markdown_path = tmp_path / "trade_session_report_2026-07-09.md"
    markdown_path.write_text("# Bhiksha Trade Session - 2026-07-09\n", encoding="utf-8")
    json_path = tmp_path / "trade_session_report_2026-07-09.json"
    return DailyReportWriteResult(
        report={"trading_date": "2026-07-09", "status": {"level": "GREEN"}},
        json_path=json_path,
        markdown_path=markdown_path,
    )


def test_session_report_review_publishes_markdown_to_coding_agent(monkeypatch, tmp_path) -> None:
    captured: dict = {}

    def fake_publish(**kwargs):
        captured.update(kwargs)
        return ReviewPublishResult(
            attempted=True,
            ok=True,
            mode="on",
            profile=kwargs["profile"],
            review_id="Bhiksha close session report - 2026-07-09",
            note_path="07 Agents/Coding/Inbox/Bhiksha close session report.md",
            surface="obsidian",
        )

    monkeypatch.setattr(launchd_job, "publish_lathi_review", fake_publish)

    result = _result(tmp_path)
    review = launchd_job._publish_session_report_review(_args(), result, "close")

    assert review is not None
    assert review.ok is True
    # Artifact: the on-disk markdown report is what gets projected.
    assert captured["source"] == result.markdown_path
    # Route/profile: the shared coding-agent surface.
    assert captured["profile"] == "coding-agent-northstar"
    assert captured["owner_consumer"] == "bhiksha"
    # Title carries the trading date so the operator can tell reports apart.
    assert captured["title"] == "Bhiksha close session report - 2026-07-09"


def test_session_report_review_off_mode_skips_publish(monkeypatch, tmp_path) -> None:
    def fake_publish(**kwargs):  # pragma: no cover - must never be called
        raise AssertionError("publish must not run when review mode is off")

    monkeypatch.setattr(launchd_job, "publish_lathi_review", fake_publish)

    review = launchd_job._publish_session_report_review(
        _args(obsidian_review_mode="off"), _result(tmp_path), "midday"
    )

    assert review is None


def test_session_report_review_degrades_when_bus_unreachable(monkeypatch, tmp_path) -> None:
    """A non-ok publish must be surfaced but never raise (graceful no-op)."""

    def fake_publish(**kwargs):
        return ReviewPublishResult(
            attempted=True,
            ok=False,
            mode="on",
            profile=kwargs["profile"],
            error="lathi-bus not installed",
        )

    monkeypatch.setattr(launchd_job, "publish_lathi_review", fake_publish)

    review = launchd_job._publish_session_report_review(_args(), _result(tmp_path), "morning")

    assert review is not None
    assert review.ok is False
    assert review.error == "lathi-bus not installed"


def test_session_report_review_swallows_publish_exception(monkeypatch, tmp_path) -> None:
    """Even a hard crash inside the publish call must not fail the report job."""

    def fake_publish(**kwargs):
        raise RuntimeError("unexpected bus explosion")

    monkeypatch.setattr(launchd_job, "publish_lathi_review", fake_publish)

    review = launchd_job._publish_session_report_review(_args(), _result(tmp_path), "close")

    assert review is not None
    assert review.ok is False
    assert "unexpected bus explosion" in (review.error or "")
