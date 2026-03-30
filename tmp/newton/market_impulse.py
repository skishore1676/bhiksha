"""
Market Impulse Indicator

Ported from the TOS "Market Pulse" indicator (tosindicators.com).

Two components:
  1. VMA (Variable Moving Average) – adaptive MA where the smoothing
     coefficient scales with directional price strength.
  2. VWMA Stack (8 / 21 / 34) – volume-weighted moving averages that
     define the market regime (bullish / bearish / neutral).

Combined, these produce four market stages:
  - Acceleration:  Bullish regime + Close ≥ VMA
  - Deceleration:  Bearish regime + Close ≤ VMA
  - Accumulation:  Close ≥ VMA but NOT bullish
  - Distribution:  Close < VMA
"""

from __future__ import annotations

import numpy as np
import polars as pl
from loguru import logger


# ── VMA (Variable Moving Average) ──────────────────────────────────────────


def compute_vma(close: np.ndarray, length: int = 10) -> np.ndarray:
    """
    Compute the Variable Moving Average.

    The smoothing coefficient adapts based on directional strength:
      coeff = (2 / (length + 1)) * |directional_strength| / 100
      VMA[t] = coeff * price + (1 - coeff) * VMA[t-1]

    Args:
        close: 1-D array of close prices.
        length: Lookback for the directional strength calc (default 10).

    Returns:
        1-D array same length as *close* containing the VMA.
    """
    n = len(close)
    vma = np.full(n, np.nan)

    # Pre-compute per-bar up and down moves
    tmp1 = np.zeros(n)
    tmp2 = np.zeros(n)
    for i in range(1, n):
        diff = close[i] - close[i - 1]
        if diff > 0:
            tmp1[i] = diff
        elif diff < 0:
            tmp2[i] = -diff

    # Rolling sums of up / down moves (d2, d4)
    d2 = np.convolve(tmp1, np.ones(length), mode="full")[:n]
    d4 = np.convolve(tmp2, np.ones(length), mode="full")[:n]

    # Shift convolution result to align (first `length-1` values incomplete)
    # np.convolve with 'full' mode already left-aligns, so trim is fine.

    # Directional strength: ad3
    denom = d2 + d4
    with np.errstate(divide="ignore", invalid="ignore"):
        ad3 = np.where(denom == 0, 0.0, (d2 - d4) / denom * 100.0)

    # Adaptive coefficient
    coeff = (2.0 / (length + 1)) * np.abs(ad3) / 100.0

    # VMA recursion
    vma[0] = close[0]
    for i in range(1, n):
        c = coeff[i]
        vma[i] = c * close[i] + (1.0 - c) * vma[i - 1]

    return vma


# ── VWMA (Volume-Weighted Moving Average) ──────────────────────────────────


def compute_vwma(
    close: np.ndarray, volume: np.ndarray, period: int
) -> np.ndarray:
    """
    Compute VWMA = sum(volume * close, period) / sum(volume, period).
    """
    vc = close * volume
    sum_vc = np.convolve(vc, np.ones(period), mode="full")[:len(close)]
    sum_v = np.convolve(volume.astype(np.float64), np.ones(period), mode="full")[
        :len(close)
    ]

    with np.errstate(divide="ignore", invalid="ignore"):
        vwma = np.where(sum_v == 0, np.nan, sum_vc / sum_v)

    # First (period - 1) values are partial — mark NaN
    vwma[: period - 1] = np.nan
    return vwma


# ── Regime & Stage Classification ──────────────────────────────────────────


def classify_regime(
    vwma_8: np.ndarray,
    vwma_21: np.ndarray,
    vwma_34: np.ndarray,
) -> np.ndarray:
    """
    Classify each bar as 'bullish', 'bearish', or 'neutral'.
    Returns a 1-D object array of strings.
    """
    n = len(vwma_8)
    regime = np.full(n, "neutral", dtype=object)

    bullish_mask = (vwma_8 > vwma_21) & (vwma_21 > vwma_34)
    bearish_mask = (vwma_8 < vwma_21) & (vwma_21 < vwma_34)

    regime[bullish_mask] = "bullish"
    regime[bearish_mask] = "bearish"

    return regime


def classify_stage(
    regime: np.ndarray,
    close: np.ndarray,
    vma: np.ndarray,
) -> np.ndarray:
    """
    Classify each bar into one of four stages:
      Acceleration:  bullish + close ≥ VMA
      Deceleration:  bearish + close ≤ VMA
      Accumulation:  close ≥ VMA + NOT bullish
      Distribution:  close < VMA
    """
    n = len(close)
    stage = np.full(n, "distribution", dtype=object)

    for i in range(n):
        if np.isnan(vma[i]):
            continue
        if regime[i] == "bullish" and close[i] >= vma[i]:
            stage[i] = "acceleration"
        elif regime[i] == "bearish" and close[i] <= vma[i]:
            stage[i] = "deceleration"
        elif close[i] >= vma[i]:
            stage[i] = "accumulation"
        else:
            stage[i] = "distribution"

    return stage


# ── High-Level Enrichment Functions ────────────────────────────────────────


def enrich_impulse_columns(
    df: pl.DataFrame,
    vma_length: int = 10,
    vwma_periods: tuple[int, ...] = (8, 21, 34),
    suffix: str = "",
) -> pl.DataFrame:
    """
    Add Market Impulse columns to a DataFrame.

    Requires 'close' and 'volume' columns.

    Columns added (with optional suffix):
      - vma_{vma_length}{suffix}
      - vwma_{p}{suffix}  for each p in vwma_periods
      - impulse_regime{suffix}
      - impulse_stage{suffix}
    """
    close = df["close"].to_numpy().astype(np.float64)
    volume = df["volume"].to_numpy().astype(np.float64)

    # VMA
    vma = compute_vma(close, length=vma_length)

    # VWMAs
    vwmas = {}
    for p in vwma_periods:
        vwmas[p] = compute_vwma(close, volume, p)

    # Regime & stage
    regime = classify_regime(vwmas[vwma_periods[0]], vwmas[vwma_periods[1]], vwmas[vwma_periods[2]])
    stage = classify_stage(regime, close, vma)

    # Attach columns
    new_cols = [
        pl.Series(f"vma_{vma_length}{suffix}", vma),
    ]
    for p in vwma_periods:
        new_cols.append(pl.Series(f"vwma_{p}{suffix}", vwmas[p]))
    new_cols.append(pl.Series(f"impulse_regime{suffix}", regime))
    new_cols.append(pl.Series(f"impulse_stage{suffix}", stage))

    df = df.with_columns(new_cols)

    logger.info(
        "Market Impulse enrichment complete{} – {} bars, regime distribution: {}",
        f" (suffix={suffix})" if suffix else "",
        len(df),
        _regime_counts(regime),
    )
    return df


def _regime_counts(regime: np.ndarray) -> dict:
    unique, counts = np.unique(regime, return_counts=True)
    return dict(zip(unique, counts.tolist()))
