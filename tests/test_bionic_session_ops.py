from __future__ import annotations

import json
from pathlib import Path

from bhiksha.bionic.session_ops import (
    build_compile_command,
    default_feedback_export_dir,
    session_summary_to_dict,
    write_feedback_bundle,
)
from bhiksha.ops.summary import RecentEvent, SessionSummary


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
    session_payload = repo_root / "artifacts" / "playbook" / "active_session.json"
    session_payload.parent.mkdir(parents=True)
    session_payload.write_text(
        json.dumps(
            {
                "contract_name": "active_session",
                "schema_version": 1,
                "session_id": "active_session_2026-04-02",
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
        session_payload_path=session_payload,
        summary=summary,
        observation_packets=packets,
        export_to_mala_root=mala_root,
    )

    assert (bundle_dir / "session_summary.json").exists()
    assert (bundle_dir / "observation_index.json").exists()
    assert (bundle_dir / "observations" / "market_impulse_spy_short.json").exists()
    exported = default_feedback_export_dir(mala_root, "active_session_2026-04-02")
    assert (exported / "active_session.json").exists()
    exported_summary = json.loads((exported / "session_summary.json").read_text(encoding="utf-8"))
    assert exported_summary["total_events"] == 3
    assert session_summary_to_dict(summary)["recent_events"][0]["detail"] == "ok"
