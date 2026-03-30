from src.domain.trading.models import Greeks
from config import Config

def calculate_theta_factor(initial_greeks: Greeks, current_greeks: Greeks) -> float:
    """
    Calculates the Theta Factor, which measures the acceleration or deceleration of time decay.
    
    A positive value indicates slowing decay (favorable), while a negative value indicates
    accelerating decay (adverse, especially near expiration).
    """
    # Calculate theta deviation as percentage change from initial theta
    if initial_greeks.theta != 0:
        theta_dev = (current_greeks.theta - initial_greeks.theta) / abs(initial_greeks.theta)
    else:
        theta_dev = 0.0
    
    # Theta factor is the inverse of theta deviation (negative theta deviation = adverse)
    # We want positive values for favorable conditions (slower decay) and negative for adverse (faster decay)
    theta_factor = -theta_dev
    
    return theta_factor

def is_accelerating_theta_decay(theta_factor: float) -> bool:
    """
    Checks if theta decay is accelerating based on the theta factor.
    """
    # Accelerating decay is when theta factor is negative and below threshold
    return theta_factor <= Config.THETA_DECAY_THRESHOLD

def is_favorable_theta_factor(theta_factor: float) -> bool:
    """
    Checks if the theta factor is favorable (slowing decay).
    """
    # Favorable theta factor is when decay is slowing (positive value)
    return theta_factor >= abs(Config.THETA_DECAY_THRESHOLD)

def calculate_gds(initial_greeks: Greeks, current_greeks: Greeks, option_type: str) -> float:
    """
    Calculates the Greek Deviation Score (GDS).
    """
    # Calculate deviations
    delta_dev = (current_greeks.delta - initial_greeks.delta) / abs(initial_greeks.delta) if initial_greeks.delta != 0 else 0
    gamma_dev = (current_greeks.gamma - initial_greeks.gamma) / initial_greeks.gamma if initial_greeks.gamma != 0 else 0
    theta_dev = (current_greeks.theta - initial_greeks.theta) / abs(initial_greeks.theta) if initial_greeks.theta != 0 else 0
    vega_dev = (current_greeks.vega - initial_greeks.vega) / initial_greeks.vega if initial_greeks.vega != 0 else 0

    # Apply sign inversion for puts
    # Handle both single character ('P') and full word ('put') formats
    normalized_option_type = option_type.upper() if option_type else ''
    if normalized_option_type in ('P', 'PUT'):
        delta_dev *= -1
        gamma_dev *= -1

    # Calculate GDS Score
    gds_score = (
        Config.GDS_W_DELTA * delta_dev +
        Config.GDS_W_GAMMA * gamma_dev +
        Config.GDS_W_THETA * theta_dev +
        Config.GDS_W_VEGA * vega_dev
    )
    return gds_score

def is_favorable_gds(gds_score: float) -> bool:
    """
    Checks if the GDS score is favorable based on the configured threshold.
    """
    return gds_score >= Config.GDS_FAVORABLE_THRESHOLD

def is_adverse_gds(gds_score: float) -> bool:
    """
    Checks if the GDS score is adverse based on the configured threshold.
    """
    return gds_score <= Config.GDS_ADVERSE_THRESHOLD

def analyze_greek_deviations(initial_greeks: Greeks, current_greeks: Greeks) -> dict:
    """
    Analyze individual Greek deviations for detailed insights.
    """
    deviations = {
        'delta': (current_greeks.delta - initial_greeks.delta) / abs(initial_greeks.delta) if initial_greeks.delta != 0 else 0,
        'gamma': (current_greeks.gamma - initial_greeks.gamma) / initial_greeks.gamma if initial_greeks.gamma != 0 else 0,
        'theta': (current_greeks.theta - initial_greeks.theta) / abs(initial_greeks.theta) if initial_greeks.theta != 0 else 0,
        'vega': (current_greeks.vega - initial_greeks.vega) / initial_greeks.vega if initial_greeks.vega != 0 else 0
    }
    return deviations

def should_tighten_stop_loss(current_position_down_pct: float, gds_score: float) -> bool:
    """
    Determine if stop-loss should be tightened based on position drawdown and adverse GDS.
    """
    return (abs(current_position_down_pct) >= Config.GDS_STOP_ADJUSTMENT_THRESHOLD and 
            gds_score < Config.GDS_ADVERSE_THRESHOLD)

def should_widen_stop_loss(current_position_down_pct: float, gds_score: float) -> bool:
    """
    Determine if stop-loss should be widened based on position drawdown and favorable GDS.
    """
    return (abs(current_position_down_pct) >= Config.GDS_STOP_ADJUSTMENT_THRESHOLD and 
            gds_score > Config.GDS_FAVORABLE_THRESHOLD)

def should_force_theta_exit(theta_deviation: float, current_loss_pct: float) -> bool:
    """
    Determine if forced exit is warranted due to accelerating theta decay.
    """
    return (Config.ENABLE_THETA_EXIT_TRIGGER and 
            theta_deviation < Config.THETA_DECAY_THRESHOLD and 
            current_loss_pct > Config.THETA_EXIT_LOSS_THRESHOLD)