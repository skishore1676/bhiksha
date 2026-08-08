"""Auto-create Mala packet + Bhiksha binding when a shadow row is flipped.

Called from sheet sync (live-start) — no new sheets, no new params.
Sheet row enabled+shadow → fingerprint → find-or-create frozen packet.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Mala is a sibling checkout; add to path if present
# Explicit absolute candidates for both machines; no Path.home() which
# can resolve to a different user/homedir when Bhiksha runs under launchd.
MALA_ROOT_CANDIDATES = [
    Path("/Users/suman/code/mala_v2"),  # Mac Air dev
    Path("/Users/sunny/code/mala_v2"),  # oldmac live (~/code/mala_v2)
    Path("/Users/sunny/Documents/mala_v2"),  # fallback if checked out alongside bhiksha runtime
    Path(__file__).resolve().parents[5] / "mala_v2",  # repo-relative sibling
    Path(__file__).resolve().parents[4] / "mala_v2",
]

def _mala_root() -> Path | None:
    for p in MALA_ROOT_CANDIDATES:
        if (p / "src/research/experiment_packets.py").exists():
            return p
    return None

def ensure_shadow_packets(
    *,
    packet_root: Path,
    evidence_bindings_path: Path,
    strategy_catalog_path: Path,
    rows: list[Any],  # ActivePlanSheetRow
) -> dict[str, Any]:
    """Ensure every enabled shadow strategy row has a Mala packet + binding.

    Returns {created: [...], reused: [...]} for logging/board.
    """
    mala_root = _mala_root()
    if mala_root is None:
        return {"created": [], "reused": [], "error": "mala_v2 not found"}

    sys.path.insert(0, str(mala_root))
    try:
        from src.research.experiment_packets import build_cohort_contract, build_option_selection_contract, write_experiment_packet, ArtifactInput
        from src.research.evidence_identity import compute_contract_sha256  # noqa: F401
    except ImportError as e:
        return {"created": [], "reused": [], "error": f"mala import failed: {e}"}

    # Load catalog for option params
    from bhiksha.config.loader import load_strategy_catalog
    catalog = {e.strategy_id: e for e in load_strategy_catalog(strategy_catalog_path)}

    # Load existing bindings
    from bhiksha.evidence.bindings import build_registry_payload
    import json as _json
    reg = _json.loads(evidence_bindings_path.read_text())
    bindings_by_strategy = {b["strategy_id"]: b for b in reg.get("bindings", [])}

    # Load packet registry to avoid duplicate
    packet_dir = packet_root
    existing_packets = {p.name for p in packet_dir.iterdir() if p.is_dir()} if packet_dir.exists() else set()

    created: list[dict[str, Any]] = []
    reused: list[str] = []

    for row in rows:
        if getattr(row, "row_type", "strategy") != "strategy":
            continue
        if not getattr(row, "enabled", False):
            continue
        if str(getattr(row, "authorization_mode", "")).lower() != "shadow":
            continue
        sid = str(getattr(row, "strategy_id", "")).strip()
        if not sid:
            continue
        if sid in bindings_by_strategy:
            reused.append(sid)
            continue
        entry = catalog.get(sid)
        if entry is None:
            # No catalog entry — cannot fingerprint
            continue
        # Fingerprint is already the catalog execution + strategy params; if catalog changes, new packet needed.
        # Build packet for this strategy
        # Use execution spec from catalog for option contract
        exec_spec = entry.execution
        # Map catalog execution to option contract params
        opt = build_option_selection_contract(
            contract_id=f"{sid.replace('__', '-').replace('/', '-')}-option-v1"[:64],
            selector_family="single_leg_long_premium",
            selector_implementation="bhiksha.options.selectors.SingleLegOptionSelector",
            selector_version="1",
            parameters={
                "dte_min": int(getattr(exec_spec, "dte_min", 3) or 3),
                "dte_max": int(getattr(exec_spec, "dte_max", 7) or 7),
                "target_abs_delta_min": float(getattr(exec_spec, "target_abs_delta_min", 0.15) or 0.15),
                "target_abs_delta_max": float(getattr(exec_spec, "target_abs_delta_max", 0.35) or 0.35),
                "dte_fallback_policy": str(getattr(exec_spec, "dte_fallback_policy", "allow_nearest_after") or "allow_nearest_after"),
                "max_bid_ask_spread_pct": float(getattr(exec_spec, "max_bid_ask_spread_pct", 0.08) or 0.08),
                "min_open_interest": int(getattr(exec_spec, "min_open_interest", 100) or 100),
                "short_signal_contract_type": "PUT" if str(entry.strategy.params.get("direction", "short")).lower() == "short" else "CALL",
                "long_signal_contract_type": "CALL" if str(entry.strategy.params.get("direction", "short")).lower() == "short" else "PUT",
            },
        )
        cohort = build_cohort_contract(
            cohort_id=f"{sid.split('__')[-1] if '__' in sid else sid}-next20-or-28d-v1"[:64],
            window={"eligible_closed_trade_target": 20, "max_calendar_days": 28, "mode": "next_eligible_closed_or_days", "start_boundary": "first_observation_bound_to_packet_and_plan"},
            eligibility={
                "allowed_runtime_contexts": ["shadow"],
                "require_exact_identity": True,
                "require_declared_and_actual_option_selection": True,
                "require_exit_policy_hash": True,
                "require_pnl_eligible": True,
                "required_observation_outcome": "FILLED/CLOSED",
                "exclude_legacy_before_bound_plan": True,
                "quarantine_plumbing_invalid": True,
            },
            cluster_rule={"version": "underlying-session-direction-v1", "keys": ["underlying_symbol", "market_session_date", "direction"], "partial_exits_count_as_new_cluster": False},
            decision_rule={
                "version": "entry-cohort-decision.v1",
                "minimum_eligible_closed": 20,
                "minimum_independent_clusters": 8,
                "kill": {"cumulative_after_cost_r_lte": -2.0, "or_safety_stop": True},
                "retune": {"complete_cohort_sum_after_cost_r_lte": 0.0},
                "promotion_review": {"sum_after_cost_r_gt": 0.0, "median_after_cost_r_gte": 0.0, "included_plumbing_invalid_count": 0},
            },
        )
        # Create temp artifacts
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        a1 = tmp / "RESEARCH.json"
        a1.write_text(json.dumps({"strategy_id": sid, "experiment": f"{sid}-prospective-v1", "auto_created": True}, indent=2))
        a2 = tmp / "PROVIDER.json"
        a2.write_text(json.dumps({"provider": "public_api", "selector": "SingleLegOptionSelector v1"}, indent=2))

        # Use commit from mala root if available
        import subprocess
        try:
            commit = subprocess.check_output(["git", "-C", str(mala_root), "rev-parse", "HEAD"], text=True).strip()
        except Exception:
            commit = "auto-shadow-ensemble"

        # Write packet
        # Derive packet dir name from strategy
        safe = sid.replace("__", "_").replace("/", "_")[:50]
        pdir = packet_root / safe
        if pdir.exists():
            # already exists from previous run
            reused.append(sid)
            continue
        from src.research.experiment_packets import write_experiment_packet as _write
        manifest = _write(
            packet_dir=pdir,
            run_id=f"auto-shadow-{sid.split('__')[-1][:12]}-20260808",
            hypothesis_id=sid.split("__")[0][:64] if "__" in sid else sid[:64],
            candidate_id=sid,
            experiment_id=f"{sid}-prospective-v1"[:64],
            research_source_commit=commit,
            option_selection_contract=opt,
            cohort_contract=cohort,
            artifacts=[ArtifactInput("auto_research", a1, "candidate_research"), ArtifactInput("auto_provider", a2, "provider_validation")],
        )
        # Read manifest for binding
        import json as _j
        man = _j.loads(Path(manifest).read_text())
        packet_id = man["evidence_packet_id"]
        # Build binding
        new_binding = {
            "strategy_id": sid,
            "symbol": str(entry.symbol),
            "direction": str(entry.strategy.params.get("direction", "short")).lower(),
            "run_id": man["run_id"],
            "experiment_id": man["experiment_id"],
            "cohort_id": cohort["cohort_id"],
            "cohort_contract_sha256": cohort["contract_sha256"],
            "evidence_packet_id": packet_id,
            "artifact_sha256": next(a["sha256"] for a in man["artifacts"] if a["artifact_id"] == "auto_provider"),
            "artifact_uri": next(a["artifact_uri"] for a in man["artifacts"] if a["artifact_id"] == "auto_provider"),
            "declared_option_selection_contract": opt,
            "allowed_authorization_modes": ["shadow"],
        }
        # Append to registry
        all_bindings = list(reg.get("bindings", [])) + [new_binding]
        payload = build_registry_payload(all_bindings)
        evidence_bindings_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        # Update in-memory for next iteration
        reg = payload
        bindings_by_strategy[sid] = new_binding
        created.append({"strategy_id": sid, "packet_id": packet_id, "cohort_id": cohort["cohort_id"]})

    # Update Mala registry.json
    try:
        from src.research.experiment_packets import write_registry
        write_registry(packet_root)
    except Exception:
        pass

    return {"created": created, "reused": reused, "packet_root": str(packet_root)}
