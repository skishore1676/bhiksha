from __future__ import annotations

import asyncio
import json

from bhiksha.persistence.exit_state import SQLiteExitStateRepository
from bhiksha.tools.risk_envelope_rollback_status import main


def test_rollback_status_is_read_only_and_filterable(
    tmp_path,
    capsys,
) -> None:
    db_path = tmp_path / "bhiksha.db"
    repository = SQLiteExitStateRepository(str(db_path))
    asyncio.run(
        repository.latch_risk_envelope_rollback(
            "iwm-canary",
            reason="stop_handoff_unproved",
        )
    )

    assert main(
        [
            "--db",
            str(db_path),
            "--deployment-id",
            "iwm-canary",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["schema"] == "bhiksha.risk_envelope_rollback_status.v1"
    assert payload["read_only"] is True
    assert payload["rollback_latched_count"] == 1
    assert payload["latches"][0]["deployment_id"] == "iwm-canary"
    assert payload["latches"][0]["reason"] == "stop_handoff_unproved"
    assert payload["latches"][0]["latched_at"]
