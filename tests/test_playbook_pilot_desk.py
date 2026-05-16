from __future__ import annotations

import json
from pathlib import Path

from bhiksha.tools.playbook_pilot_desk import main as pilot_main
from tests.test_packet_compile import _execution_packet, _supporting_manifest, _write_parity_report

from bhiksha.shared_kernel import ensure_kernel_on_path

ensure_kernel_on_path()
from mala_bhiksha_kernel import write_packet  # noqa: E402


def test_pilot_desk_preflight_prints_eligibility_not_trade_decision(tmp_path: Path, capsys) -> None:
    _write_parity_report(tmp_path)
    packet_path = write_packet(tmp_path, _execution_packet())
    manifest_path = tmp_path / "capabilities.json"
    manifest_path.write_text(_supporting_manifest().model_dump_json(), encoding="utf-8")
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text(
        json.dumps({"status": "clear", "active_legacy_wire_count": 0}),
        encoding="utf-8",
    )

    code = pilot_main(
        [
            "preflight",
            "--packet",
            str(packet_path),
            "--capability-manifest",
            str(manifest_path),
            "--legacy-retirement-report",
            str(legacy_path),
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["eligibility"] == "eligible"
    assert "decision" not in payload


def test_pilot_desk_latest_suggests_first_step_when_empty(tmp_path: Path, capsys) -> None:
    code = pilot_main(["latest", "--artifact-root", str(tmp_path / "playbook")])

    assert code == 0
    assert "Suggested next step: run guided consultation" in capsys.readouterr().out


def test_pilot_desk_latest_does_not_advance_passed_intent(tmp_path: Path, capsys) -> None:
    intent_dir = tmp_path / "playbook" / "intents" / "pass"
    intent_dir.mkdir(parents=True)
    (intent_dir / "playbook_operator_decision.json").write_text(
        json.dumps({"status": "operator_pass"}),
        encoding="utf-8",
    )

    code = pilot_main(["latest", "--artifact-root", str(tmp_path / "playbook")])

    output = capsys.readouterr().out
    assert code == 0
    assert "[operator_pass]" in output
    assert "Suggested next step: run guided consultation when a fresh setup appears" in output
