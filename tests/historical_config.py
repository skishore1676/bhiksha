from __future__ import annotations

from pathlib import Path

from bhiksha.config.loader import load_deployments, load_strategy_catalog

FIXTURE_CONFIG_ROOT = Path(__file__).parent / "fixtures" / "config"
HISTORICAL_DEPLOYMENTS_DIR = FIXTURE_CONFIG_ROOT / "deployments"
HISTORICAL_STRATEGY_CATALOG_DIR = FIXTURE_CONFIG_ROOT / "strategy_catalog"


def load_historical_deployments():
    return load_deployments(HISTORICAL_DEPLOYMENTS_DIR)


def load_historical_strategy_catalog():
    return load_strategy_catalog(HISTORICAL_STRATEGY_CATALOG_DIR)


def historical_deployment(deployment_id: str):
    return next(
        deployment
        for deployment in load_historical_deployments()
        if deployment.deployment_id == deployment_id
    )
