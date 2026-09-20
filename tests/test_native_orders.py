import pytest
from bhiksha.config.models import ExecutionSpec
from bhiksha.execution.native_orders import decide_execution_route
from historical_config import historical_deployment

@pytest.mark.parametrize("flag", ["enable_native_bracket_route", "enable_native_oto_route"])
def test_native_groups_fail_before_order_submission(flag):
    deployment = historical_deployment("market_impulse_qqq_short_v1")
    with pytest.raises(ValueError, match="native order groups"):
        ExecutionSpec.model_validate({**deployment.execution.model_dump(), flag: True})
    bypassed = deployment.model_copy(update={"execution": deployment.execution.model_copy(update={flag: True})})
    with pytest.raises(ValueError, match="native order groups"):
        decide_execution_route(bypassed)
