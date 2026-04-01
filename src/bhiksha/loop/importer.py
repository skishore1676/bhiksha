"""Import Mala loop artifacts into generated Bhiksha deployments."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

import yaml

from bhiksha.config.loader import load_app_config, load_bias_inputs
from bhiksha.config.models import AppConfig, BiasSelection, DeploymentManifest
from bhiksha.loop.contracts import (
    ALLOWED_PLAYBOOK_COVERAGE_STATUSES,
    DEPLOYMENT_CANDIDATES_CONTRACT_NAME,
    PLAYBOOK_CATALOG_CONTRACT_NAME,
    validate_contract_metadata,
)


@dataclass(slots=True, frozen=True)
class ImportReport:
    generated_deployments: list[dict[str, Any]] = field(default_factory=list)
    manual_research_candidates: list[dict[str, Any]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    safe_for_live_review: bool = False
    output_paths: dict[str, str] = field(default_factory=dict)


def refresh_generated_deployments(
    *,
    config_root: str | Path = "config",
    deployment_candidates_path: str | Path,
    playbook_catalog_path: str | Path,
    bias_inputs_path: str | Path | None = None,
    max_export_age_hours: int = 36,
) -> ImportReport:
    config_root = Path(config_root)
    repo_root = config_root.parent
    app_config = _load_app_config_or_default(config_root)
    generated_dir = repo_root / app_config.generated_deployments_dir
    playbook_dir = repo_root / app_config.playbook_artifacts_dir

    candidates_payload = _load_json(Path(deployment_candidates_path))
    playbook_payload = _load_json(Path(playbook_catalog_path))
    validate_contract_metadata(
        candidates_payload,
        expected_contract_name=DEPLOYMENT_CANDIDATES_CONTRACT_NAME,
    )
    validate_contract_metadata(
        playbook_payload,
        expected_contract_name=PLAYBOOK_CATALOG_CONTRACT_NAME,
    )
    _validate_playbook_contexts(playbook_payload.get("contexts", {}))
    bias_inputs = load_bias_inputs(bias_inputs_path or (repo_root / app_config.bias_inputs_path))

    issues: list[str] = []
    issues.extend(_staleness_issues(candidates_payload, "deployment_candidates", max_export_age_hours))
    issues.extend(_staleness_issues(playbook_payload, "playbook_catalog", max_export_age_hours))

    enabled_bias_inputs = [selection for selection in bias_inputs if selection.enabled]
    if not enabled_bias_inputs:
        issues.append("empty_bias_selection")

    candidate_by_id = _candidate_index(candidates_payload.get("candidates", []))
    playbook_contexts = playbook_payload.get("contexts", {})

    selected_candidates: list[dict[str, Any]] = []
    manual_candidates: list[dict[str, Any]] = []

    for selection in enabled_bias_inputs:
        context_key = _context_key(selection)
        context = playbook_contexts.get(context_key)
        if not isinstance(context, dict):
            issues.append(f"missing_playbook_context:{context_key}")
            continue
        coverage_status = str(context.get("coverage_status"))
        if coverage_status == "not_covered_by_enabled_family":
            issues.append(f"playbook_context_uncovered:{context_key}")
            continue
        selected_candidates.extend(
            _select_supported_candidates(
                candidate_by_id=candidate_by_id,
                context=context,
                selection=selection,
                issues=issues,
            )
        )
        manual_candidates.extend(
            _select_manual_candidates(
                candidate_by_id=candidate_by_id,
                context=context,
                selection=selection,
                issues=issues,
            )
        )

    selected_candidates = _dedupe_candidates(selected_candidates)
    manual_candidates = _dedupe_candidates(manual_candidates)

    generated_deployments: list[dict[str, Any]] = []
    human_ids = _human_managed_deployment_ids(config_root / "deployments")
    blocked_generation = any(
        issue.startswith(prefix)
        for prefix in ("empty_bias_selection", "stale_", "missing_playbook_context")
        for issue in issues
    )
    if blocked_generation:
        issues.append("generated_deployments_skipped")
    else:
        generated_dir.mkdir(parents=True, exist_ok=True)
        for file_path in sorted(generated_dir.glob("*.yaml")):
            file_path.unlink()
        for candidate in selected_candidates:
            manifest = _build_generated_manifest(candidate)
            deployment_id = str(manifest["deployment_id"])
            if deployment_id in human_ids:
                issues.append(f"deployment_id_collision:{deployment_id}")
                continue
            DeploymentManifest.model_validate(manifest)
            target_path = generated_dir / f"{deployment_id}.yaml"
            target_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
            generated_deployments.append(
                {
                    "deployment_id": deployment_id,
                    "candidate_id": candidate["candidate_id"],
                    "path": str(target_path),
                    "bias_template": candidate["bias_template"],
                    "horizon": candidate["horizon"],
                    "symbol": candidate["symbol"],
                    "direction": candidate["direction"],
                }
            )

    playbook_dir.mkdir(parents=True, exist_ok=True)
    manual_board_path = playbook_dir / "manual_research_board.json"
    manual_board_md_path = playbook_dir / "manual_research_board.md"
    import_report_path = playbook_dir / "import_report.json"
    import_report_md_path = playbook_dir / "import_report.md"
    _write_manual_board(manual_board_path, manual_board_md_path, manual_candidates)

    safe_for_live_review = not issues and bool(generated_deployments)
    report = ImportReport(
        generated_deployments=generated_deployments,
        manual_research_candidates=manual_candidates,
        issues=issues,
        safe_for_live_review=safe_for_live_review,
        output_paths={
            "generated_dir": str(generated_dir),
            "manual_board_json": str(manual_board_path),
            "manual_board_md": str(manual_board_md_path),
            "import_report_json": str(import_report_path),
            "import_report_md": str(import_report_md_path),
        },
    )
    import_report_path.write_text(json.dumps(asdict(report), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    import_report_md_path.write_text(_report_markdown(report), encoding="utf-8")
    return report


def _load_app_config_or_default(config_root: Path) -> AppConfig:
    app_path = config_root / "app.yaml"
    if app_path.exists():
        return load_app_config(app_path)
    return AppConfig()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object at {path}")
    return payload


def _validate_playbook_contexts(contexts: Any) -> None:
    if not isinstance(contexts, dict):
        raise ValueError("playbook contexts must be a JSON object")
    for key, context in contexts.items():
        if not isinstance(context, dict):
            raise ValueError(f"Playbook context {key!r} must be an object")
        coverage_status = context.get("coverage_status")
        if coverage_status not in ALLOWED_PLAYBOOK_COVERAGE_STATUSES:
            raise ValueError(
                f"Playbook context {key!r} has invalid coverage_status {coverage_status!r}"
            )
        covered_families = context.get("covered_by_strategy_families")
        if not isinstance(covered_families, list):
            raise ValueError(
                f"Playbook context {key!r} must include covered_by_strategy_families"
            )


def _staleness_issues(payload: dict[str, Any], label: str, max_export_age_hours: int) -> list[str]:
    generated_at_raw = payload.get("generated_at")
    if not generated_at_raw:
        return [f"stale_{label}:missing_generated_at"]
    try:
        generated_at = datetime.fromisoformat(str(generated_at_raw))
    except ValueError:
        return [f"stale_{label}:invalid_generated_at"]
    age_seconds = (datetime.now(UTC) - generated_at.astimezone(UTC)).total_seconds()
    if age_seconds > max_export_age_hours * 3600:
        return [f"stale_{label}:age_hours={round(age_seconds / 3600.0, 2)}"]
    return []


def _candidate_index(candidates: list[Any]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        candidate_id = str(raw.get("candidate_id", "")).strip()
        if not candidate_id:
            continue
        if candidate_id in indexed:
            raise ValueError(f"Duplicate candidate_id in export: {candidate_id}")
        indexed[candidate_id] = raw
    return indexed


def _context_key(selection: BiasSelection) -> str:
    return f"{selection.symbol}|{selection.bias_template}|{selection.horizon}"


def _select_supported_candidates(
    *,
    candidate_by_id: dict[str, dict[str, Any]],
    context: dict[str, Any],
    selection: BiasSelection,
    issues: list[str],
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for entry in context.get("supported_candidates", []):
        if len(selected) >= selection.max_active_candidates:
            break
        if not isinstance(entry, dict):
            continue
        candidate_id = str(entry.get("candidate_id", "")).strip()
        candidate = candidate_by_id.get(candidate_id)
        if candidate is None:
            issues.append(f"playbook_candidate_missing:{candidate_id}")
            continue
        if candidate.get("surface_class") != "supported":
            issues.append(f"playbook_supported_surface_mismatch:{candidate_id}")
            continue
        if candidate.get("automation_status") != "shadow_ready":
            continue
        selected.append(candidate)
    return selected


def _select_manual_candidates(
    *,
    candidate_by_id: dict[str, dict[str, Any]],
    context: dict[str, Any],
    selection: BiasSelection,
    issues: list[str],
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for entry in context.get("proposed_candidates", []):
        if len(selected) >= selection.max_active_candidates:
            break
        if not isinstance(entry, dict):
            continue
        candidate_id = str(entry.get("candidate_id", "")).strip()
        candidate = candidate_by_id.get(candidate_id)
        if candidate is None:
            issues.append(f"playbook_candidate_missing:{candidate_id}")
            continue
        if candidate.get("surface_class") != "proposed":
            issues.append(f"playbook_proposed_surface_mismatch:{candidate_id}")
            continue
        selected.append(candidate)
    return selected


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id"))
        deduped.setdefault(candidate_id, candidate)
    return list(deduped.values())


def _human_managed_deployment_ids(root: Path) -> set[str]:
    if not root.exists():
        return set()
    ids: set[str] = set()
    generated_segment = str((root / "generated").resolve())
    for file_path in sorted(root.rglob("*.yaml")):
        if str(file_path.resolve()).startswith(generated_segment):
            continue
        payload = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
        if isinstance(payload, dict) and "deployment_id" in payload:
            ids.add(str(payload["deployment_id"]))
    return ids


def _build_generated_manifest(candidate: dict[str, Any]) -> dict[str, Any]:
    manifest = json.loads(json.dumps(candidate["manifest"]))
    execution = manifest.setdefault("execution", {})
    execution["shadow_only"] = True
    source = manifest.setdefault("source", {})
    metadata = source.setdefault("metadata", {})
    metadata.update(
        {
            "candidate_id": candidate["candidate_id"],
            "surface_class": candidate["surface_class"],
            "automation_status": candidate["automation_status"],
            "bias_template": candidate["bias_template"],
            "horizon": candidate["horizon"],
            "automation_lane": "automated_shadow",
        }
    )
    return manifest


def _write_manual_board(json_path: Path, markdown_path: Path, candidates: list[dict[str, Any]]) -> None:
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "manual_research_candidates": [_manual_candidate_entry(candidate) for candidate in candidates],
    }
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = ["# Manual Research Board", ""]
    if not candidates:
        lines.append("- No proposed candidates matched the active bias selections.")
    else:
        for candidate in candidates:
            entry = _manual_candidate_entry(candidate)
            lines.append(
                f"- `{entry['candidate_id']}` {entry['symbol']} {entry['direction']} "
                f"{entry['bias_template']} | strategy={entry['strategy_key']} | run_date={entry['run_date']}"
            )
            lines.append(
                f"  capabilities={', '.join(entry['required_bhiksha_capabilities']) or 'none listed'} | "
                f"mc_prob_positive_exp={entry['mc_prob_positive_exp']} | "
                f"mean_holdout_exp_r={entry['mean_holdout_exp_r']}"
            )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _manual_candidate_entry(candidate: dict[str, Any]) -> dict[str, Any]:
    summary = candidate.get("evidence", {}).get("holdout", {}).get("summary", {})
    monte_carlo = candidate.get("evidence", {}).get("monte_carlo", {})
    metadata = candidate.get("manifest", {}).get("source", {}).get("metadata", {})
    return {
        "candidate_id": candidate.get("candidate_id"),
        "strategy_key": candidate.get("strategy_key"),
        "symbol": candidate.get("symbol"),
        "direction": candidate.get("direction"),
        "bias_template": candidate.get("bias_template"),
        "run_date": candidate.get("source", {}).get("run_date"),
        "required_bhiksha_capabilities": metadata.get("required_bhiksha_capabilities", []),
        "mc_prob_positive_exp": monte_carlo.get("mc_prob_positive_exp"),
        "mc_exp_r_p50": monte_carlo.get("mc_exp_r_p50"),
        "mean_holdout_exp_r": summary.get("mean_holdout_exp_r"),
        "artifact": candidate.get("source", {}).get("artifact"),
    }


def _report_markdown(report: ImportReport) -> str:
    lines = [
        "# Playbook Import Report",
        "",
        f"- safe_for_live_review: `{report.safe_for_live_review}`",
        f"- generated_deployments: `{len(report.generated_deployments)}`",
        f"- manual_research_candidates: `{len(report.manual_research_candidates)}`",
    ]
    if report.issues:
        lines.extend(["", "## Issues"])
        for issue in report.issues:
            lines.append(f"- `{issue}`")
    if report.generated_deployments:
        lines.extend(["", "## Generated Deployments"])
        for deployment in report.generated_deployments:
            lines.append(
                f"- `{deployment['deployment_id']}` <- `{deployment['candidate_id']}` "
                f"{deployment['symbol']} {deployment['direction']} {deployment['bias_template']}"
            )
    if report.manual_research_candidates:
        lines.extend(["", "## Manual Research"])
        for candidate in report.manual_research_candidates:
            lines.append(
                f"- `{candidate['candidate_id']}` {candidate['symbol']} {candidate['direction']} "
                f"{candidate['bias_template']}"
            )
    return "\n".join(lines) + "\n"
