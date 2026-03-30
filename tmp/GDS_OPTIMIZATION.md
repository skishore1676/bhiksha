# GDS Optimization Initiative

## Overview

The Greek Deviation Scoring (GDS) system monitors how option Greeks drift from their values at entry and uses this to adjust targets and stops. **This initiative aims to move from binary fixed-bump logic to data-driven adaptive exits.**

### Current Behavior (v1)

GDS = `0.4·Δ_dev + 0.2·γ_dev + 0.2·θ_dev + 0.2·ν_dev`

| Condition | Action |
|-----------|--------|
| GDS ≥ 0.15 (favorable) | Bump Target 1 by +5%, Target 2 by +10% |
| GDS ≤ −0.15 (adverse) & position down ≥15% | Tighten stop by 5% |
| Neutral | Standard fixed targets |

**Problem**: This is binary. A GDS of +0.16 gets the same bump as +0.80. From live trading analysis (see below), GDS often stays below the 0.15 threshold even during winning trades, making the bump logic inactive.

### Case Study: IWM $249 Put — 2026-03-16

- Entry $1.19, exited at Target 1 ($1.63, +37%)
- GDS ranged +0.014 to +0.071 — **never crossed 0.15**, so target bumps never activated
- GDS was **rising sharply** (+0.033 → +0.071) in the 3 minutes before exit
- The GDS *slope* was far more informative than the *level*

---

## Implementation Phases

### Phase 1: Enrich Telemetry ✅ Approved

Add to the `gds_snapshots` table (via `gds_repo.py` and `background.py`):

| Column | Purpose |
|--------|---------|
| `entry_price` | Enables P&L % calculations directly from telemetry |
| `pnl_pct` | Computed on insert: `(current_price - entry_price) / entry_price × 100` |
| `gds_slope` | Change in GDS from previous snapshot (1st derivative) |

### Phase 2: Post-Exit Tracking ✅ Approved

- Continue logging GDS snapshots with `POST_EXIT` state tag for a configurable window after trade close
- Duration controlled by `POST_EXIT_TRACKING_MINUTES` (env-overridable, default: 30 min)
- Also stores `exit_price` and `exit_reason` to enable filtering analysis by exit type
- Runs as a background task that doesn't interfere with the trading bot

### Phase 3: Trajectory Analyzer ✅ Approved

Build `analysis/gds_trajectory_analyzer.py` — a **standalone script** (no bot dependency) that:

1. Cross-references `trades.db` and `gds_history.db`
2. Calculates per-trade metrics:
   - GDS at entry, at peak price, at exit
   - GDS slope (1st derivative) at each point
   - P&L % at peak vs. at actual exit → "left on table" metric
   - GDS value/slope when price peaked → candidate exit signals
3. Outputs a summary table and per-trade analysis

### Phase 4: Adaptive Exit Rules (Future — Pending Phase 3 Findings)

Replace binary bumps with data-driven rules. Candidate approaches to validate:

| Hypothesis | Description |
|------------|-------------|
| **GDS Slope Reversal** | Exit when GDS starts declining after being positive for N snapshots |
| **GDS Momentum Stall** | Tighten stop if GDS stops rising for 3+ snapshots while in profit |
| **Proportional Targets** | Scale T1/T2 proportionally to GDS magnitude, not binary |
| **Zero-Crossing** | Use GDS crossing zero from positive as a trailing exit trigger |

**What we're looking for in Phase 3 analysis:**
- Does GDS slope reversal predict price reversal? If so, by how many snapshots?
- What GDS level/slope was present when price peaked for each trade?
- Is there a consistent "GDS shape" for winners vs. losers?
- Would a slope-based exit have captured more profit than fixed T1/T2?

---

## File Map

| File | Role |
|------|------|
| `utils/greeks.py` | GDS calculation functions |
| `src/persistence/gds_repo.py` | SQLite telemetry storage |
| `src/background.py` → `_log_trade_telemetry()` | Cold-loop fire-and-forget snapshot logger |
| `src/strategy.py` | Trading decisions using GDS |
| `gds_history.db` | Telemetry database |
| `trades.db` | Trade outcomes database |
| `analysis/gds_trajectory_analyzer.py` | Standalone analysis script (Phase 3) |
| `config.py` | GDS weights, thresholds, and feature flags |

## Configuration Reference

```env
# GDS Weights
GDS_W_DELTA=0.4
GDS_W_GAMMA=0.2
GDS_W_THETA=0.2
GDS_W_VEGA=0.2

# GDS Thresholds
GDS_FAVORABLE_THRESHOLD=0.15
GDS_ADVERSE_THRESHOLD=-0.15
GDS_STOP_ADJUSTMENT_THRESHOLD=15.0

# GDS Target/Stop Manipulation
GDS_STOP_MANIPULATION=10.0
GDS_TARGET_1_MANIPULATION=10.0
GDS_TARGET_2_MANIPULATION=10.0

# Post-Exit Tracking (Phase 2)
POST_EXIT_TRACKING_MINUTES=30
```
