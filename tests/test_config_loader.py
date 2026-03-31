from pathlib import Path

from bhiksha.config.loader import load_deployments


def test_load_deployments_from_config_directory() -> None:
    deployments = load_deployments(Path("config/deployments"))
    ids = {deployment.deployment_id for deployment in deployments}
    assert ids == {
        "jerk_pivot_momentum_tsla_short_v1",
        "market_impulse_qqq_short_v1",
        "market_impulse_spy_short_v1",
    }
    tsla = next(deployment for deployment in deployments if deployment.deployment_id == "jerk_pivot_momentum_tsla_short_v1")
    assert tsla.enabled is True
    assert tsla.execution.shadow_only is True
    assert tsla.execution.dte_min == 7
    assert tsla.execution.dte_max == 21
    assert tsla.execution.target_abs_delta_min == 0.35
    assert tsla.execution.target_abs_delta_max == 0.55
    assert tsla.execution.entry_window_start_et == "09:45"
    assert tsla.execution.entry_window_end_et == "14:30"
    assert tsla.source.metadata["holdout_mean_exp_r"] == 0.8397
