from pathlib import Path

from bhiksha.config.loader import load_deployments


def test_load_deployments_from_config_directory() -> None:
    deployments = load_deployments(Path("config/deployments"))
    ids = {deployment.deployment_id for deployment in deployments}
    assert ids == {
        "market_impulse_qqq_short_v1",
        "market_impulse_spy_short_v1",
    }

