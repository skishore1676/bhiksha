from __future__ import annotations

import json
from pathlib import Path

import polars as pl

from bhiksha.tools.export_reversion_events import export_reversion_events
from tests.test_intraday_mean_reversion_strategy import _frame_for_short_reversal


def test_export_reversion_events_writes_runtime_parity_csv(tmp_path: Path) -> None:
    bars_path = tmp_path / "bars.csv"
    config_path = tmp_path / "config.json"
    out_path = tmp_path / "runtime_events.csv"
    _frame_for_short_reversal().write_csv(bars_path)
    config_path.write_text(
        json.dumps(
            {
                "configs": {
                    "cfg_short": {
                        "stretch_source": "prior_rth_close_atr",
                        "stretch_threshold": 2.0,
                        "reversal_range_minutes": 5,
                        "confirming_bars": 1,
                        "velocity_filter": "no_filter",
                        "stage_filter": "no_filter",
                        "gap_state_filter": "no_filter",
                        "use_jerk_confirmation": False,
                        "exit_family": "fixed_1r",
                    }
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    export_reversion_events(
        bars_path=bars_path,
        config_path=config_path,
        out_path=out_path,
        assume_enriched=True,
    )

    rows = pl.read_csv(out_path).to_dicts()
    assert len(rows) == 1
    assert rows[0]["config_id"] == "cfg_short"
    assert rows[0]["direction"] == "short"
    assert rows[0]["policy_id"] == "cfg_short"
