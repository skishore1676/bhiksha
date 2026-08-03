#!/usr/bin/env python3
"""Build a broker-inert PDD v2 resize candidate from a fresh Sheet read."""

from __future__ import annotations

import argparse
import base64
import copy
import gzip
import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from bhiksha.active_plan.compiler import (
    GoogleSheetTableClient,
    _compile_row,
    compile_active_plan_from_rows,
    compute_live_triage_authorization_sha256,
    load_operator_defaults_sheet_rows,
    load_rows_from_sheet_records_with_report,
    load_strategy_catalog_sheet_rows_with_report,
    sync_google_strategy_catalog,
)
from bhiksha.config.loader import load_strategy_catalog
from bhiksha.execution.profile_exit import _partial_quantity


SPREADSHEET_ID = "1cJPWfkQB6pp91TAFNT86R5Pi1cUfzCgT3bUWgjY6rbc"
ACTIVE_PLAN_ID = "active_plan_2026-08-03_pdd_canary_v2"
ACTIVE_PLAN_V1_ID = "active_plan_2026-08-03_pdd_canary_v1"
TRADING_DATE = "2026-08-03"
PACKET_ID = "3a5235741f52bf249d5972cabf8a98cdd7fb4016350a6572376cdda1667fce17"
DEPLOYMENT_ID = "strategy_triage_market_impulse_pdd_pdd_long_live_row_29"
CANARY_ID = "pdd-market-impulse-long-entry-canary-v2"
AUTHORIZATION_CONTRACT = "pdd-entry-canary.v2"
PDD_STRATEGY_ID = "triage-market_impulse-PDD__pdd_long"
PDD_V1_AUTHORIZATION_SHA256 = (
    "7500d4f18bd7f0dde4697d4a77efcb90f881365f51fd898b33823a4f644efa01"
)
PDD_V1_ACTIVE_PLAN_SHA256 = (
    "9c1a2c68fb4d83e8c7538239a8838e55459980334078c814066a1389e8023e14"
)
PDD_V1_PLAN_REVISION_ID = (
    "sha256:608a2641d7b70d8c038ca3c866c6bb153b5d274518c75c99e1241f7272530e56"
)
SHEET_NAMES = (
    "active_strategy",
    "manual_entry",
    "Mala_Evidence_v1",
    "Operator_Defaults_v1",
)
PDD_TARGET_FIELDS = {
    "max_trade_premium_usd",
    "notes",
    "max_contracts",
    "source_metadata",
}
EXPECTED_ACTIVE_STRATEGY_COLUMNS = (
    "enabled",
    "authorization_mode",
    "strategy_id",
    "entry_window_start_et",
    "max_trade_premium_usd",
    "execution_overrides",
    "notes",
    "exit",
    "execution",
    "max_contracts",
    "source_metadata",
)


def canonical_sha(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _read_coherent_sheet_snapshot(
    read_rows: Callable[[str], list[dict]],
) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Double-read all source tabs and fail if any tab changes between passes."""

    first = {
        name: copy.deepcopy(read_rows(name))
        for name in SHEET_NAMES
    }
    first_hashes = {
        name: canonical_sha(value)
        for name, value in first.items()
    }
    second = {
        name: copy.deepcopy(read_rows(name))
        for name in SHEET_NAMES
    }
    second_hashes = {
        name: canonical_sha(value)
        for name, value in second.items()
    }
    if first_hashes != second_hashes:
        changed = [
            name
            for name in SHEET_NAMES
            if first_hashes[name] != second_hashes[name]
        ]
        raise RuntimeError(
            "Sheet source drift across complete reads: " + ", ".join(changed)
        )
    return first, first_hashes


def _json_object(value: object, *, field: str) -> dict:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"PDD row has invalid {field}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"PDD row {field} must be a JSON object")
    return parsed


def _numeric(value: object, *, field: str) -> float:
    if isinstance(value, bool):
        raise RuntimeError(f"PDD row {field} must be numeric")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"PDD row {field} must be numeric") from exc


def _assert_pdd_v1_preimage(row: dict) -> None:
    observed_columns = tuple(key for key in row if key != "row_index")
    if observed_columns != EXPECTED_ACTIVE_STRATEGY_COLUMNS:
        raise RuntimeError(
            "active_strategy header layout drifted; E/G/J/K are no longer "
            "the authorized PDD fields"
        )
    if row.get("enabled") not in {True, "TRUE"}:
        raise RuntimeError("PDD row 29 preimage is not enabled")
    if str(row.get("authorization_mode") or "").strip().lower() != "live":
        raise RuntimeError("PDD row 29 preimage is not live-authorized")
    if row.get("strategy_id") != PDD_STRATEGY_ID:
        raise RuntimeError("PDD row 29 strategy identity drifted")
    if _numeric(row.get("max_trade_premium_usd"), field="premium cap") != 300.0:
        raise RuntimeError("PDD row 29 preimage premium cap is not 300")
    if _numeric(row.get("max_contracts"), field="max contracts") != 1.0:
        raise RuntimeError("PDD row 29 preimage max contracts is not 1")
    metadata = _json_object(row.get("source_metadata"), field="source_metadata")
    if metadata.get("authorized_active_plan_id") != ACTIVE_PLAN_V1_ID:
        raise RuntimeError("PDD row 29 preimage active-plan id drifted")
    if metadata.get("authorization_sha256") != PDD_V1_AUTHORIZATION_SHA256:
        raise RuntimeError("PDD row 29 preimage authorization drifted")
    exit_payload = _json_object(row.get("exit"), field="exit")
    if exit_payload.get("profile_exit_drives_live") is not True:
        raise RuntimeError("PDD row 29 preimage profile exit is not live")
    if exit_payload.get("profile_exit_shadow_only") is not False:
        raise RuntimeError("PDD row 29 preimage profile exit is shadow-only")
    if exit_payload.get("risk_envelope_live_mode", "off") != "off":
        raise RuntimeError("PDD row 29 preimage Risk Envelope is not off")


def _project_pdd_v2(
    active_rows: list[dict],
) -> tuple[dict, list[dict], dict]:
    """Return immutable v1 evidence and a projection changing only E/G/J/K."""

    source_rows = copy.deepcopy(active_rows)
    source_hash = canonical_sha(source_rows)
    observed_row = copy.deepcopy(
        next(row for row in source_rows if int(row["row_index"]) == 29)
    )
    _assert_pdd_v1_preimage(observed_row)

    projected_rows = copy.deepcopy(source_rows)
    pdd_row = next(
        row for row in projected_rows if int(row["row_index"]) == 29
    )
    metadata = _json_object(
        pdd_row.get("source_metadata"),
        field="source_metadata",
    )
    policy = dict(metadata.get("canary_policy") or {})
    policy.update(
        {
            "max_cumulative_loss_r": -2.0,
            "provider_overlap_floor": 0.90,
            "stop_on_unprotected_position": True,
            "stop_on_missing_attribution": True,
            "stop_on_failed_exit_receipt": True,
            "scale_min_clean_closes": 10,
            "r_definition": (
                "sum_after_cost_trade_pnl_over_frozen_entry_stop_risk"
            ),
            "scale_fraction_of_baseline": 0.50,
            "round_trip_cost_per_contract_usd": 2.0,
        }
    )
    metadata.update(
        {
            "run_id": "triage-w1w2-20260710-pdd-long",
            "evidence_packet_id": PACKET_ID,
            "artifact_sha256": (
                "5f7ec476597ddd7abd6c4da888ccaae14c143eb6537706b9f87208859b27fcd8"
            ),
            "artifact_uri": (
                f"mala-evidence://sha256/{PACKET_ID}/"
                "pdd_research_evidence.json"
            ),
            "authorization_contract_version": AUTHORIZATION_CONTRACT,
            "canary_id": CANARY_ID,
            "canary_start_at": "2026-08-03T00:00:00-05:00",
            "canary_expires_at": "2026-08-28T15:15:00-05:00",
            "authorized_active_plan_id": ACTIVE_PLAN_ID,
            "authorized_deployment_id": DEPLOYMENT_ID,
            "baseline_max_trade_premium_usd": 2_000.0,
            "provider_signal_overlap": 0.9646,
            "canary_policy": policy,
            "operator_resize_decision": {
                "actor": "Suman",
                "recorded_at": "2026-08-02",
                "max_trade_premium_usd": 1_000.0,
                "max_contracts": 2,
                "management_policy": "TREND_CONTINUATION",
                "risk_envelope_live_mode": "off",
            },
            "authorization_sha256": "0" * 64,
        }
    )
    pdd_row.update(
        {
            "max_trade_premium_usd": 1_000.0,
            "max_contracts": 2,
            "notes": (
                "PDD entry canary v2 authorized 2026-08-02: maximum two "
                "contracts, max $1,000 premium, TREND_CONTINUATION profile, "
                "Risk Envelope off, 0-3 DTE, expires 2026-08-28; further "
                "scale requires a separate operator decision."
            ),
            "source_metadata": json.dumps(
                metadata,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }
    )
    changed_fields = {
        key
        for key in set(observed_row) | set(pdd_row)
        if observed_row.get(key) != pdd_row.get(key)
    }
    if changed_fields != PDD_TARGET_FIELDS:
        raise RuntimeError(
            "PDD projection changed unexpected fields: "
            + ", ".join(sorted(changed_fields))
        )
    if canonical_sha(source_rows) != source_hash:
        raise RuntimeError("immutable active_strategy source snapshot mutated")
    if canonical_sha(observed_row) != canonical_sha(
        next(row for row in source_rows if int(row["row_index"]) == 29)
    ):
        raise RuntimeError("immutable PDD observed row mutated")
    return observed_row, projected_rows, pdd_row


def _cutover_contract(
    *,
    observed_row_sha256: str,
    active_header_contract: dict,
    v1_replay: dict,
    current_v1_plan: dict,
) -> dict:
    return {
        "session_boundary": "both jobs must remain not running; no manual start",
        "preconditions": {
            "sheet_row_29_sha256": observed_row_sha256,
            "active_strategy_header_contract": active_header_contract,
            "v1_snapshot_replay": v1_replay,
            "current_v1_plan": current_v1_plan,
            "active_plan_id": ACTIVE_PLAN_V1_ID,
            "active_plan_sha256": PDD_V1_ACTIVE_PLAN_SHA256,
            "plan_revision_id": PDD_V1_PLAN_REVISION_ID,
            "launchd_active_plan_ids": {
                "com.bhiksha.live-start": ACTIVE_PLAN_V1_ID,
                "com.bhiksha.live-watchdog": ACTIVE_PLAN_V1_ID,
            },
            "launchd_states": {
                "com.bhiksha.live-start": "not running",
                "com.bhiksha.live-watchdog": "not running",
            },
        },
        "postconditions": {
            "active_plan_id": ACTIVE_PLAN_ID,
            "launchd_active_plan_ids": {
                "com.bhiksha.live-start": ACTIVE_PLAN_ID,
                "com.bhiksha.live-watchdog": ACTIVE_PLAN_ID,
            },
            "launchd_states": {
                "com.bhiksha.live-start": "not running",
                "com.bhiksha.live-watchdog": "not running",
            },
            "runtime_start_authorized": False,
        },
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--credentials",
        type=Path,
        default=Path(
            "/Users/sunny/Documents/bhiksha/config/google-credentials.json"
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(
            "docs/release_candidates/pdd_resize_20260802/"
            "pdd_canary_candidate_v2.json"
        ),
    )
    parser.add_argument(
        "--current-active-plan",
        type=Path,
        default=Path("artifacts/playbook/active_plan.json"),
    )
    return parser.parse_args()


def _active_header_contract(client: GoogleSheetTableClient) -> dict:
    first = client._read_layout()
    second = client._read_layout()
    first_payload = {
        "header_row_number": first.header_row_number,
        "header_start_index": first.header_start_index,
        "headers": list(first.headers),
    }
    second_payload = {
        "header_row_number": second.header_row_number,
        "header_start_index": second.header_start_index,
        "headers": list(second.headers),
    }
    if first_payload != second_payload:
        raise RuntimeError("active_strategy header drifted across reads")
    if first.header_row_number != 1 or first.header_start_index != 0:
        raise RuntimeError("active_strategy header is not anchored at A1")
    if tuple(first.headers) != EXPECTED_ACTIVE_STRATEGY_COLUMNS:
        raise RuntimeError("active_strategy physical header mapping drifted")
    mapping = {
        chr(ord("A") + index): header
        for index, header in enumerate(first.headers)
    }
    required_mapping = {
        "E": "max_trade_premium_usd",
        "G": "notes",
        "J": "max_contracts",
        "K": "source_metadata",
    }
    if any(mapping.get(cell) != field for cell, field in required_mapping.items()):
        raise RuntimeError("active_strategy E/G/J/K header mapping drifted")
    payload = {
        **first_payload,
        "cell_mapping": mapping,
        "required_cell_mapping": required_mapping,
    }
    payload["sha256"] = canonical_sha(payload)
    return payload


def _assert_current_v1_plan(path: Path) -> dict:
    plan_bytes = path.read_bytes()
    plan_sha256 = hashlib.sha256(plan_bytes).hexdigest()
    if plan_sha256 != PDD_V1_ACTIVE_PLAN_SHA256:
        raise RuntimeError("current active plan SHA does not match retained v1")
    payload = json.loads(plan_bytes)
    if payload.get("active_plan_id") != ACTIVE_PLAN_V1_ID:
        raise RuntimeError("current active plan id does not match retained v1")
    if payload.get("plan_revision_id") != PDD_V1_PLAN_REVISION_ID:
        raise RuntimeError("current active plan revision does not match retained v1")
    return {
        "path": str(path.resolve()),
        "active_plan_id": payload["active_plan_id"],
        "active_plan_sha256": plan_sha256,
        "plan_revision_id": payload["plan_revision_id"],
    }


def _assert_v1_snapshot_replay(
    *,
    snapshot: dict[str, list[dict]],
    catalog_root: Path,
    catalog_rows: list,
    operator_defaults: dict,
) -> dict:
    strategy_rows = load_rows_from_sheet_records_with_report(
        snapshot["active_strategy"],
        row_type="strategy",
        sheet_name="active_strategy",
    )
    manual_rows = load_rows_from_sheet_records_with_report(
        snapshot["manual_entry"],
        row_type="manual",
        sheet_name="manual_entry",
    )
    if strategy_rows.suppressed or manual_rows.suppressed:
        raise RuntimeError(
            "suppressed rows in v1 preimage replay: "
            f"strategy={strategy_rows.suppressed}, "
            f"manual={manual_rows.suppressed}"
        )
    result = compile_active_plan_from_rows(
        rows=[*strategy_rows.rows, *manual_rows.rows],
        strategy_catalog_path=catalog_root,
        active_plan_id=ACTIVE_PLAN_V1_ID,
        trading_date=TRADING_DATE,
        source_name="pdd_canary_v1_preimage_replay",
        source_details={"spreadsheet_id": SPREADSHEET_ID},
        google_strategy_catalog=catalog_rows,
        operator_defaults=operator_defaults,
    )
    plan = result.plan.model_dump(mode="json")
    if plan["plan_revision_id"] != PDD_V1_PLAN_REVISION_ID:
        raise RuntimeError(
            "untouched four-tab snapshot does not replay retained v1 revision"
        )
    deployment = next(
        item
        for item in result.plan.deployments
        if item.deployment_id == DEPLOYMENT_ID
    )
    authorization = compute_live_triage_authorization_sha256(
        deployment,
        active_plan_id=ACTIVE_PLAN_V1_ID,
    )
    if authorization != PDD_V1_AUTHORIZATION_SHA256:
        raise RuntimeError(
            "untouched four-tab snapshot does not replay retained v1 authorization"
        )
    if (
        str(deployment.source.metadata.get("authorization_sha256") or "")
        != authorization
    ):
        raise RuntimeError("v1 source row authorization does not recompute")
    return {
        "active_plan_id": plan["active_plan_id"],
        "plan_revision_id": plan["plan_revision_id"],
        "authorization_sha256": authorization,
        "deployment_sha256": canonical_sha(
            deployment.model_dump(mode="json")
        ),
    }


def main() -> None:
    args = _parse_args()
    clients: dict[str, GoogleSheetTableClient] = {}

    def rows(name: str) -> list[dict]:
        if name not in clients:
            clients[name] = GoogleSheetTableClient(
                spreadsheet_id=SPREADSHEET_ID,
                sheet_name=name,
                credentials_path=args.credentials,
            )
        return clients[name].read_rows()

    snapshot, source_hashes = _read_coherent_sheet_snapshot(rows)
    active_header_contract = _active_header_contract(
        clients["active_strategy"]
    )
    current_v1_plan = _assert_current_v1_plan(args.current_active_plan)
    manual = snapshot["manual_entry"]
    catalog_raw = snapshot["Mala_Evidence_v1"]
    defaults = snapshot["Operator_Defaults_v1"]

    catalog = load_strategy_catalog_sheet_rows_with_report(
        catalog_raw,
        sheet_name="Mala_Evidence_v1",
    )
    if catalog.suppressed:
        raise RuntimeError(f"suppressed Mala evidence rows: {catalog.suppressed}")
    operator_defaults = load_operator_defaults_sheet_rows(defaults)
    with tempfile.TemporaryDirectory() as temp_dir:
        catalog_root = Path(temp_dir) / "catalog"
        sync_google_strategy_catalog(
            strategy_catalog_path=catalog_root,
            google_strategy_catalog=catalog.rows,
            operator_defaults=operator_defaults,
        )
        v1_replay = _assert_v1_snapshot_replay(
            snapshot=snapshot,
            catalog_root=catalog_root,
            catalog_rows=catalog.rows,
            operator_defaults=operator_defaults,
        )
        observed_row, active, pdd_row = _project_pdd_v2(
            snapshot["active_strategy"]
        )
        metadata = _json_object(
            pdd_row.get("source_metadata"),
            field="source_metadata",
        )
        local_by_id = {
            item.strategy_id: item
            for item in load_strategy_catalog(catalog_root)
        }
        google_by_id = {item.catalog_key: item for item in catalog.rows}
        strategy_rows = load_rows_from_sheet_records_with_report(
            active,
            row_type="strategy",
            sheet_name="active_strategy",
        )
        if strategy_rows.suppressed:
            raise RuntimeError(
                f"suppressed active rows before authorization: "
                f"{strategy_rows.suppressed}"
            )
        compiled_row = next(
            item
            for item in strategy_rows.rows
            if item.source_metadata.get("row_index") == 29
        )
        provisional = _compile_row(
            compiled_row,
            local_by_id,
            google_by_id,
            True,
        )
        authorization = compute_live_triage_authorization_sha256(
            provisional,
            active_plan_id=ACTIVE_PLAN_ID,
        )

        metadata["authorization_sha256"] = authorization
        pdd_row["source_metadata"] = json.dumps(
            metadata,
            sort_keys=True,
            separators=(",", ":"),
        )
        strategy_rows = load_rows_from_sheet_records_with_report(
            active,
            row_type="strategy",
            sheet_name="active_strategy",
        )
        manual_rows = load_rows_from_sheet_records_with_report(
            manual,
            row_type="manual",
            sheet_name="manual_entry",
        )
        if strategy_rows.suppressed or manual_rows.suppressed:
            raise RuntimeError(
                "suppressed rows after v2 authorization: "
                f"strategy={strategy_rows.suppressed}, "
                f"manual={manual_rows.suppressed}"
            )
        result = compile_active_plan_from_rows(
            rows=[*strategy_rows.rows, *manual_rows.rows],
            strategy_catalog_path=catalog_root,
            active_plan_id=ACTIVE_PLAN_ID,
            trading_date=TRADING_DATE,
            source_name="pdd_canary_v2_release_candidate",
            source_details={"spreadsheet_id": SPREADSHEET_ID},
            google_strategy_catalog=catalog.rows,
            operator_defaults=operator_defaults,
        )
        plan = result.plan.model_dump(mode="json")
        deployment = next(
            item
            for item in plan["deployments"]
            if item["deployment_id"] == DEPLOYMENT_ID
        )

    observed = {
        "source_tab_sha256": source_hashes,
        "source_snapshot_sha256": canonical_sha(snapshot),
        "source_snapshot_gzip_base64": base64.b64encode(
            gzip.compress(canonical_bytes(snapshot), mtime=0)
        ).decode("ascii"),
        "active_strategy_header_contract": active_header_contract,
        "active_strategy_row_29": observed_row,
        "active_strategy_row_29_sha256": canonical_sha(observed_row),
        "v1_snapshot_replay": v1_replay,
        "current_v1_plan": current_v1_plan,
    }

    assert deployment["risk"]["max_trade_premium_usd"] == 1_000.0
    assert deployment["risk"]["max_contracts"] == 2
    assert deployment["exit"]["profile_exit_id"] == "profile__trend_continuation"
    assert deployment["exit"]["profile_exit_drives_live"] is True
    assert deployment["exit"]["target_1_quantity"] == 0.60
    assert deployment["exit"]["risk_envelope_live_mode"] == "off"
    assert deployment["execution"]["dte_min"] == 0
    assert deployment["execution"]["dte_max"] == 3
    assert deployment["execution"]["dte_fallback_policy"] == "allow_nearest_after"
    target_1_contracts = _partial_quantity(
        deployment["risk"]["max_contracts"],
        deployment["exit"]["target_1_quantity"],
    )
    assert target_1_contracts == 1
    assert deployment["risk"]["max_contracts"] - target_1_contracts == 1

    active_plan_bytes = json.dumps(
        plan,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    receipt = {
        "schema_version": "bhiksha.pdd_canary_release_candidate.v2",
        "generated_at": datetime.now(UTC).isoformat(),
        "effect_authority": (
            "none; fresh read-only Sheet projection and broker-inert compile"
        ),
        "operator_decision": {
            "symbol": "PDD",
            "max_trade_premium_usd": 1_000.0,
            "max_contracts": 2,
            "management_policy": "TREND_CONTINUATION",
            "risk_envelope_live_mode": "off",
        },
        "spreadsheet_id": SPREADSHEET_ID,
        "observed_source": observed,
        "observed_source_sha256": canonical_sha(observed),
        "cutover_contract": _cutover_contract(
            observed_row_sha256=canonical_sha(observed_row),
            active_header_contract=active_header_contract,
            v1_replay=v1_replay,
            current_v1_plan=current_v1_plan,
        ),
        "proposed_cells": {
            "row_29": {
                "E": 1_000.0,
                "G": pdd_row["notes"],
                "J": 2,
                "K": pdd_row["source_metadata"],
            }
        },
        "active_plan_id": ACTIVE_PLAN_ID,
        "plan_revision_id": plan["plan_revision_id"],
        "authorization_sha256": authorization,
        "authorization_payload": {
            "active_plan_id": ACTIVE_PLAN_ID,
            "deployment": deployment,
        },
        "active_plan_sha256": hashlib.sha256(
            active_plan_bytes
        ).hexdigest(),
        "active_plan_gzip_base64": base64.b64encode(
            gzip.compress(active_plan_bytes, mtime=0)
        ).decode("ascii"),
        "projection_assertions": {
            "deployment_count": len(plan["deployments"]),
            "suppressed_count": len(plan["suppressed"]),
            "pdd_max_trade_premium_usd": deployment["risk"][
                "max_trade_premium_usd"
            ],
            "pdd_max_contracts": deployment["risk"]["max_contracts"],
            "pdd_profile_exit_id": deployment["exit"]["profile_exit_id"],
            "pdd_target_1_quantity": deployment["exit"]["target_1_quantity"],
            "pdd_target_1_contracts": target_1_contracts,
            "pdd_runner_contracts": (
                deployment["risk"]["max_contracts"] - target_1_contracts
            ),
            "pdd_risk_envelope_live_mode": deployment["exit"][
                "risk_envelope_live_mode"
            ],
        },
    }
    receipt["cutover_contract"]["postconditions"].update(
        {
            "sheet_row_29_sha256": canonical_sha(pdd_row),
            "active_plan_sha256": receipt["active_plan_sha256"],
            "plan_revision_id": plan["plan_revision_id"],
        }
    )
    if canonical_sha(observed_row) != observed[
        "active_strategy_row_29_sha256"
    ]:
        raise RuntimeError("retained observed PDD row hash no longer recomputes")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(args.out)


if __name__ == "__main__":
    main()
