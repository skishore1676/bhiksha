"""Auto-create Mala packet + Bhiksha binding — Option C.

Post-compile reconcile: fingerprint the **final effective deployment**
(catalog + active_strategy overrides + risk/exit overrides) and
find-or-create the matching experiment. Tuesday reuses, DTE/delta/
exit change creates v2.

Called from compiler *after* _compile_row, before apply_evidence_binding.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

MALA_ROOT_CANDIDATES = [
    Path("/Users/suman/code/mala_v2"),
    Path("/Users/sunny/code/mala_v2"),
    Path("/Users/sunny/Documents/mala_v2"),
    Path(__file__).resolve().parents[5] / "mala_v2",
    Path(__file__).resolve().parents[4] / "mala_v2",
]

def _mala_root() -> Path | None:
    for p in MALA_ROOT_CANDIDATES:
        if (p / "src/research/experiment_packets.py").exists():
            return p
    return None


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def compute_deployment_experiment_fingerprint(deployment: Any) -> str:
    """Canonical experiment fingerprint from effective economic behavior.

    Includes strategy identity/params/direction, entry window/signal,
    option selection (DTE, delta, OI, spread, pricing), Control exit
    policy hash. Excludes date, active_plan_id, row_id, notes,
    generated_at, risk sizing for pure shadow.
    """
    s = deployment.strategy
    e = deployment.execution
    x = deployment.exit
    params = dict(getattr(s, "params", {}) or {})
    # Direction from strategy params (primary economic direction)
    direction = str(params.get("direction") or "").lower()

    # Option mapping derived for contract type
    opt_map = dict(getattr(e, "option_mapping", {}) or {})

    payload = {
        "strategy_key": str(getattr(s, "key", "")),
        "strategy_version": int(getattr(s, "version", 1)),
        "strategy_params": params,  # full economic params
        "direction": direction,
        "option_mapping": opt_map,
        "execution": {
            "dte_min": int(getattr(e, "dte_min", 0) or 0),
            "dte_max": int(getattr(e, "dte_max", 0) or 0),
            "dte_fallback_policy": str(getattr(e, "dte_fallback_policy", "strict")),
            "target_abs_delta_min": float(getattr(e, "target_abs_delta_min", 0) or 0) if getattr(e, "target_abs_delta_min", None) is not None else None,
            "target_abs_delta_max": float(getattr(e, "target_abs_delta_max", 0) or 0) if getattr(e, "target_abs_delta_max", None) is not None else None,
            "min_open_interest": int(getattr(e, "min_open_interest", 0) or 0),
            "max_bid_ask_spread_pct": float(getattr(e, "max_bid_ask_spread_pct", 0) or 0) if getattr(e, "max_bid_ask_spread_pct", None) is not None else None,
            "entry_pricing_mode": str(getattr(e, "entry_pricing_mode", "urgent")),
            "entry_pricing_require_two_sided_quote": bool(getattr(e, "entry_pricing_require_two_sided_quote", True)),
            "entry_pricing_require_open_interest": bool(getattr(e, "entry_pricing_require_open_interest", True)),
            "entry_reprice_enabled": getattr(e, "entry_reprice_enabled", None),
            "entry_reprice_checkpoints_seconds": getattr(e, "entry_reprice_checkpoints_seconds", None),
            "entry_reprice_spread_fractions": getattr(e, "entry_reprice_spread_fractions", None),
            "entry_window_start_et": getattr(e, "entry_window_start_et", None),
            "entry_window_end_et": getattr(e, "entry_window_end_et", None),
        },
        "exit": {
            "exit_policy_hash": getattr(x, "exit_policy_hash", None),
            "exit_policy_id": getattr(x, "exit_policy_id", None),
            "use_algorithmic_exit": bool(getattr(x, "use_algorithmic_exit", True)),
            "stop_loss_pct": float(getattr(x, "stop_loss_pct", 0) or 0) if getattr(x, "stop_loss_pct", None) is not None else None,
            "profile_exit_id": getattr(x, "profile_exit_id", None),
            "hard_flat_time_et": getattr(x, "hard_flat_time_et", None),
            "max_hold_seconds": getattr(x, "max_hold_seconds", None),
            "giveback_arm_r": getattr(x, "giveback_arm_r", None),
            "giveback_retrace_fraction": getattr(x, "giveback_retrace_fraction", None),
        },
        "cohort_rule": "next20-or-28d-v1",
    }
    return _canonical_sha256(payload)


def _fingerprint_to_option_contract(deployment: Any) -> dict[str, Any]:
    # Build parameters that exactly match deployment.execution model_dump + contract types
    # so apply_evidence_binding drift check passes when we just created the packet.
    e = deployment.execution
    s = deployment.strategy
    direction = str(s.params.get("direction", "short")).lower()
    actual = e.model_dump(mode="json")
    return {
        "dte_min": actual.get("dte_min"),
        "dte_max": actual.get("dte_max"),
        "target_abs_delta_min": actual.get("target_abs_delta_min"),
        "target_abs_delta_max": actual.get("target_abs_delta_max"),
        "dte_fallback_policy": actual.get("dte_fallback_policy"),
        "max_bid_ask_spread_pct": actual.get("max_bid_ask_spread_pct"),
        "min_open_interest": actual.get("min_open_interest"),
        "short_signal_contract_type": "PUT" if direction == "short" else "CALL",
        "long_signal_contract_type": "CALL" if direction == "short" else "PUT",
    }


def _existing_binding_fingerprint(binding: dict[str, Any]) -> str | None:
    # Stored by reconcile as declared_option_selection_contract + exit_policy_hash
    # Fall back to contract sha for legacy bindings.
    fp = binding.get("experiment_fingerprint")
    if isinstance(fp, str) and re.fullmatch(r"[0-9a-f]{64}", fp):
        return fp
    return None


# Legacy shim: compile-time caller used ensure_shadow_packets(rows) pre-compile.
# Keep it but delegate to post-compile path via deployments; if called with rows
# alone we do nothing and let the post-compile reconciler handle it (avoids
# snapshotting catalog before overrides).
def ensure_shadow_packets(
    *,
    packet_root: Path,
    evidence_bindings_path: Path,
    strategy_catalog_path: Path,
    rows: list[Any],
) -> dict[str, Any]:
    return {"created": [], "reused": [], "skipped": "use reconcile_shadow_experiments post-compile"}


def reconcile_shadow_experiments(
    *,
    packet_root: Path,
    evidence_bindings_path: Path,
    deployments: list[Any],
    rows_by_id: dict[str, Any],
    evidence_bindings: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Post-compile reconcile: for each enabled shadow deployment.

    If fingerprint matches existing binding → reuse.
    If missing or mismatched → create new packet + replace binding.
    On failure → return AUTO_EXPERIMENT_BLOCKED per strategy (caller suppresses only that lane).

    Caller must reload bindings and apply to returned reconciled bindings dict.
    """
    mala_root = _mala_root()
    if mala_root is None:
        return {"created": [], "reused": [], "blocked": [], "error": "mala_v2 not found"}
    sys.path.insert(0, str(mala_root))
    try:
        from src.research.experiment_packets import build_cohort_contract, build_option_selection_contract, write_experiment_packet, ArtifactInput
    except ImportError as e:
        return {"created": [], "reused": [], "blocked": [], "error": f"mala import failed: {e}"}

    from bhiksha.evidence.bindings import build_registry_payload

    # Load registry
    try:
        reg = json.loads(evidence_bindings_path.read_text())
    except FileNotFoundError:
        reg = {"schema_version": "bhiksha.evidence_binding_registry.v1", "bindings": []}
    bindings_by_strategy = dict(evidence_bindings or {b["strategy_id"]: b for b in reg.get("bindings", [])})
    all_bindings = list(reg.get("bindings", []))

    created: list[dict[str, Any]] = []
    reused: list[str] = []
    blocked: list[dict[str, Any]] = []
    # Track bindings after reconcile (for caller to apply)
    reconciled = dict(bindings_by_strategy)

    for dep in list(deployments):
        row = rows_by_id.get(getattr(dep, "deployment_id", ""))
        if row is None:
            continue
        if getattr(row, "row_type", "strategy") != "strategy":
            continue
        if not getattr(row, "enabled", False):
            continue
        if str(getattr(row, "authorization_mode", "")).lower() != "shadow":
            continue
        sid = str(getattr(row, "strategy_id", "")).strip()
        if not sid:
            continue
        # Live rows untouched — fail-closed via existing bindings only.
        try:
            fp = compute_deployment_experiment_fingerprint(dep)
        except Exception as exc:
            blocked.append({"strategy_id": sid, "reason": f"fingerprint failed: {exc}"})
            continue
        existing = reconciled.get(sid)
        existing_fp = _existing_binding_fingerprint(existing) if existing else None
        if existing is not None and existing_fp == fp:
            # Even if fingerprint matches, verify option contract still matches
            # (stale binding with wrong dte/delta from early helper fallback)
            try:
                if _fingerprint_to_option_contract(dep) == (existing.get("declared_option_selection_contract") or {}).get("parameters", {}):
                    reused.append(sid)
                    continue
                # Drift despite fingerprint match -> force new version
            except Exception:
                reused.append(sid)
                continue
        # Need new packet/binding (missing or drift)
        if existing is not None and existing_fp is None:
            # Legacy binding without fingerprint: compare option contract params
            # If option params + exit hash match, treat as reuse and backfill fingerprint.
            try:
                dep_opt = _fingerprint_to_option_contract(dep)
                bound_opt = (existing.get("declared_option_selection_contract") or {}).get("parameters", {})
                exit_hash = getattr(dep.exit, "exit_policy_hash", None)
                bound_exit = (existing.get("declared_option_selection_contract") or {}).get("exit_policy_hash") or existing.get("exit_policy_hash")
                # Also check exit via binding snapshot
                if bound_opt == dep_opt and (exit_hash == bound_exit or (exit_hash is None and bound_exit is None)):
                    # Backfill fingerprint
                    existing = dict(existing)
                    existing["experiment_fingerprint"] = fp
                    # Update registry in place
                    for i, b in enumerate(all_bindings):
                        if b.get("strategy_id") == sid:
                            all_bindings[i] = existing
                            break
                    reconciled[sid] = existing
                    reused.append(sid)
                    continue
            except Exception:
                pass
        # Create new packet
        try:
            entry = dep  # effective deployment already has final overrides
            opt_params = _fingerprint_to_option_contract(dep)
            opt = build_option_selection_contract(
                contract_id=f"{sid.replace('__', '-').replace('/', '-')}-option-v1"[:64],
                selector_family="single_leg_long_premium",
                selector_implementation="bhiksha.options.selectors.SingleLegOptionSelector",
                selector_version="1",
                parameters=opt_params,
            )
            # Cohort stable
            short = sid.split("__")[-1] if "__" in sid else sid
            cohort = build_cohort_contract(
                cohort_id=f"{short}-next20-or-28d-v1"[:64],
                window={"eligible_closed_trade_target": 20, "max_calendar_days": 28, "mode": "next_eligible_closed_or_days", "start_boundary": "first_observation_bound_to_packet_and_plan"},
                eligibility={"allowed_runtime_contexts": ["shadow"], "require_exact_identity": True, "require_declared_and_actual_option_selection": True, "require_exit_policy_hash": True, "require_pnl_eligible": True, "required_observation_outcome": "FILLED/CLOSED", "exclude_legacy_before_bound_plan": True, "quarantine_plumbing_invalid": True},
                cluster_rule={"version": "underlying-session-direction-v1", "keys": ["underlying_symbol", "market_session_date", "direction"], "partial_exits_count_as_new_cluster": False},
                decision_rule={"version": "entry-cohort-decision.v1", "minimum_eligible_closed": 20, "minimum_independent_clusters": 8, "kill": {"cumulative_after_cost_r_lte": -2.0, "or_safety_stop": True}, "retune": {"complete_cohort_sum_after_cost_r_lte": 0.0}, "promotion_review": {"sum_after_cost_r_gt": 0.0, "median_after_cost_r_gte": 0.0, "included_plumbing_invalid_count": 0}},
            )
            import tempfile, subprocess
            tmp = Path(tempfile.mkdtemp())
            a1 = tmp / "RESEARCH.json"
            a1.write_text(json.dumps({"strategy_id": sid, "fingerprint": fp, "experiment": f"{sid}-prospective"}, indent=2))
            a2 = tmp / "PROVIDER.json"
            a2.write_text(json.dumps({"provider": "public_api", "selector": "SingleLegOptionSelector v1", "fingerprint": fp}, indent=2))
            try:
                commit = subprocess.check_output(["git", "-C", str(mala_root), "rev-parse", "HEAD"], text=True).strip()
            except Exception:
                commit = "auto-shadow-ensemble"
            # Versioned packet dir: safe + fingerprint suffix so old packet preserved
            safe = sid.replace("__", "_").replace("/", "_")[:48]
            suffix = fp[:8]
            # Determine next version
            existing_versions = [p for p in (packet_root.glob(f"{safe}*")) if p.is_dir()] if packet_root.exists() else []
            ver = len([p for p in existing_versions if p.name.startswith(safe)]) + 1
            pdir = packet_root / f"{safe}-{suffix}-v{ver}"
            if pdir.exists():
                # Reuse if same fingerprint dir already exists
                reused.append(sid)
                continue
            manifest = write_experiment_packet(
                packet_dir=pdir,
                run_id=f"auto-shadow-{short[:12]}-v{ver}-{suffix}",
                hypothesis_id=sid.split("__")[0][:64] if "__" in sid else sid[:64],
                candidate_id=sid,
                experiment_id=f"{sid}-prospective-v{ver}"[:64],
                research_source_commit=commit,
                option_selection_contract=opt,
                cohort_contract=cohort,
                artifacts=[ArtifactInput("auto_research", a1, "candidate_research"), ArtifactInput("auto_provider", a2, "provider_validation")],
            )
            man = json.loads(Path(manifest).read_text())
            packet_id = man["evidence_packet_id"]
            new_binding: dict[str, Any] = {
                "strategy_id": sid,
                "symbol": str(getattr(dep, "symbol", "")),
                "direction": str(dep.strategy.params.get("direction", "short")).lower(),
                "run_id": man["run_id"],
                "experiment_id": man["experiment_id"],
                "cohort_id": cohort["cohort_id"],
                "cohort_contract_sha256": cohort["contract_sha256"],
                "evidence_packet_id": packet_id,
                "artifact_sha256": next(a["sha256"] for a in man["artifacts"] if a["artifact_id"] == "auto_provider"),
                "artifact_uri": next(a["artifact_uri"] for a in man["artifacts"] if a["artifact_id"] == "auto_provider"),
                "declared_option_selection_contract": opt,
                "allowed_authorization_modes": ["shadow"],
                "experiment_fingerprint": fp,
                "exit_policy_hash": getattr(dep.exit, "exit_policy_hash", None),
            }
            # Replace binding for this sid, preserve others
            all_bindings = [b for b in all_bindings if b.get("strategy_id") != sid] + [new_binding]
            payload = build_registry_payload(all_bindings)
            evidence_bindings_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            reg = payload
            # Use normalized binding from payload (adds schema_version + binding_sha256)
            normalized = next(b for b in payload["bindings"] if b.get("strategy_id") == sid)
            reconciled[sid] = normalized
            created.append({"strategy_id": sid, "packet_id": packet_id, "fingerprint": fp, "version": ver, "packet_dir": str(pdir)})
        except Exception as exc:
            blocked.append({"strategy_id": sid, "reason": str(exc)[:300]})
            continue

    try:
        from src.research.experiment_packets import write_registry
        write_registry(packet_root)
    except Exception:
        pass
    return {"created": created, "reused": reused, "blocked": blocked, "bindings": reconciled, "packet_root": str(packet_root)}
