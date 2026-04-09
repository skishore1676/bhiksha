from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

from bhiksha.config.loader import load_active_plan
from bhiksha.bionic.session_ops import (
    build_compile_command,
    default_feedback_export_dir,
    session_summary_to_dict,
    write_feedback_bundle,
)
from bhiksha.ops.summary import RecentEvent, SessionSummary
from bhiksha.tools.bionic_session import main as bionic_session_main


def test_build_compile_command_prefers_publish_and_live_flags(tmp_path: Path) -> None:
    mala_root = tmp_path / "mala_v1"
    (mala_root / ".venv" / "bin").mkdir(parents=True)
    (mala_root / ".venv" / "bin" / "python").write_text("", encoding="utf-8")

    command = build_compile_command(
        mala_root=mala_root,
        out_dir=tmp_path / "out",
        live_authorized=True,
        publish_bhiksha=True,
        extra_args=["--manual-google-sheet-id", "sheet123"],
    )

    assert command[0] == str((mala_root / ".venv" / "bin" / "python").resolve())
    assert command[1] == "scripts/compile_active_session.py"
    assert "--live-authorized" in command
    assert "--publish-bhiksha" in command
    assert command[-2:] == ["--manual-google-sheet-id", "sheet123"]


def test_write_feedback_bundle_exports_to_mala(tmp_path: Path) -> None:
    repo_root = tmp_path / "bhiksha"
    repo_root.mkdir()
    active_plan = repo_root / "artifacts" / "playbook" / "active_plan.json"
    active_plan.parent.mkdir(parents=True)
    active_plan.write_text(
        json.dumps(
            {
                "contract_name": "active_plan",
                "schema_version": 1,
                "active_plan_id": "active_plan_2026-04-02",
                "deployments": [],
            }
        ),
        encoding="utf-8",
    )
    summary = SessionSummary(
        total_events=3,
        event_type_counts={"startup_config": 1},
        recent_events=[RecentEvent(created_at="2026-04-02T14:30:00Z", event_type="startup_config", detail="ok")],
    )
    packets = [{"deployment_id": "market_impulse_spy_short", "symbol": "SPY", "source_origin": "mala_playbook"}]
    mala_root = tmp_path / "mala_v1"
    mala_root.mkdir()

    bundle_dir = write_feedback_bundle(
        repo_root=repo_root,
        active_plan_path=active_plan,
        summary=summary,
        observation_packets=packets,
        export_to_mala_root=mala_root,
    )

    assert (bundle_dir / "session_summary.json").exists()
    assert (bundle_dir / "observation_index.json").exists()
    assert (bundle_dir / "observations" / "market_impulse_spy_short.json").exists()
    exported = default_feedback_export_dir(mala_root, "active_plan_2026-04-02")
    assert (exported / "active_plan.json").exists()
    exported_summary = json.loads((exported / "session_summary.json").read_text(encoding="utf-8"))
    assert exported_summary["total_events"] == 3
    assert session_summary_to_dict(summary)["recent_events"][0]["detail"] == "ok"


def test_bionic_prepare_compiles_local_sheet_into_active_plan(tmp_path: Path) -> None:
    repo_root = tmp_path / "bhiksha"
    repo_root.mkdir()
    catalog_root = repo_root / "config" / "strategy_catalog"
    catalog_root.mkdir(parents=True)
    sheet_path = repo_root / "sheet.csv"
    output_path = repo_root / "artifacts" / "playbook" / "active_plan.json"

    (catalog_root / "spy_strategy.yaml").write_text(
        yaml.safe_dump(
            {
                "strategy_id": "market_impulse_spy_short_v1",
                "enabled": True,
                "symbol": "SPY",
                "strategy": {"key": "market_impulse", "version": 1, "params": {"direction": "short"}},
                "execution": {
                    "profile": "single_leg_long_premium_v1",
                    "shadow_only": True,
                    "option_mapping": {"long_signal": "CALL", "short_signal": "PUT"},
                    "dte_min": 0,
                    "dte_max": 7,
                    "target_abs_delta_min": 0.2,
                    "target_abs_delta_max": 0.4,
                    "min_open_interest": 100,
                    "max_bid_ask_spread_pct": 0.2,
                },
                "risk": {
                    "profile": "conservative_day1",
                    "max_trade_premium_usd": 300,
                    "hard_flat_time_et": "15:55",
                    "stop_loss_pct": 0.45,
                },
                "exit": {
                    "profile": "market_impulse_exit_v1",
                    "use_algorithmic_exit": True,
                    "use_profit_target": False,
                    "profit_target_multiple": None,
                    "stop_loss_pct": 0.45,
                    "stop_to_breakeven_after_r_multiple": None,
                    "hard_flat_time_et": "15:55",
                },
                "source": {"origin": "test_catalog", "run_date": "2026-04-08", "artifact": "research.md"},
                "approval_status": "approved",
                "tags": ["test"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    sheet_path.write_text(
        "\n".join(
            [
                "row_id,row_type,authorization_mode,strategy_id",
                "spy_live_today,strategy,live,market_impulse_spy_short_v1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    cwd = Path.cwd()
    try:
        os.chdir(repo_root)
        exit_code = bionic_session_main(
            [
                "prepare",
                "--sheet",
                str(sheet_path),
                "--strategy-catalog",
                str(catalog_root),
                "--active-plan-out",
                str(output_path),
                "--active-plan-id",
                "active_plan_2026-04-09",
                "--skip-healthcheck",
                "--skip-warm-start",
            ]
        )
    finally:
        os.chdir(cwd)

    assert exit_code == 0
    plan = load_active_plan(output_path)
    assert plan.active_plan_id == "active_plan_2026-04-09"
    assert [deployment.deployment_id for deployment in plan.deployments] == ["spy_live_today"]
