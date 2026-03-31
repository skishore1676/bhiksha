from datetime import UTC, datetime
from pathlib import Path

from bhiksha.domain.enums import SignalDirection
from bhiksha.domain.models import SignalDecision
from bhiksha.tools.signal_inspector import _decision_to_csv_row, _write_csv


def test_signal_inspector_writes_csv(tmp_path: Path) -> None:
    decision = SignalDecision(
        deployment_id="jerk_pivot_momentum_tsla_short_v1",
        symbol="TSLA",
        timestamp=datetime(2026, 3, 27, 14, 43, tzinfo=UTC),
        signal=True,
        direction=SignalDirection.SHORT,
        reason=["time_window_ok", "jerk_pivot_short"],
        features={"vpoc_4h": 100.0},
    )

    row = _decision_to_csv_row(
        decision,
        show_features=True,
        window_start=datetime(2026, 3, 27, 0, 0, tzinfo=UTC),
        trading_days=3,
    )
    output_path = tmp_path / "signals.csv"
    _write_csv(output_path, [row], show_features=True)

    content = output_path.read_text(encoding="utf-8")

    assert "deployment_id,symbol,timestamp_et,timestamp_utc,signal,direction,reason_json,window_start_utc,trading_days,features_json" in content
    assert "jerk_pivot_momentum_tsla_short_v1" in content
    assert "\"[\"\"time_window_ok\"\", \"\"jerk_pivot_short\"\"]\"" in content
