"""Market Impulse indicator computations."""

from __future__ import annotations

import numpy as np
import polars as pl
from loguru import logger


def compute_vma(close: np.ndarray, length: int = 10) -> np.ndarray:
    """Compute the Variable Moving Average."""
    n = len(close)
    vma = np.full(n, np.nan)

    tmp1 = np.zeros(n)
    tmp2 = np.zeros(n)
    for index in range(1, n):
        diff = close[index] - close[index - 1]
        if diff > 0:
            tmp1[index] = diff
        elif diff < 0:
            tmp2[index] = -diff

    d2 = np.convolve(tmp1, np.ones(length), mode="full")[:n]
    d4 = np.convolve(tmp2, np.ones(length), mode="full")[:n]
    denom = d2 + d4
    with np.errstate(divide="ignore", invalid="ignore"):
        ad3 = np.where(denom == 0, 0.0, (d2 - d4) / denom * 100.0)

    coeff = (2.0 / (length + 1)) * np.abs(ad3) / 100.0
    vma[0] = close[0]
    for index in range(1, n):
        c = coeff[index]
        vma[index] = c * close[index] + (1.0 - c) * vma[index - 1]
    return vma


def compute_vwma(close: np.ndarray, volume: np.ndarray, period: int) -> np.ndarray:
    """Compute a volume-weighted moving average."""
    volume_close = close * volume
    sum_vc = np.convolve(volume_close, np.ones(period), mode="full")[: len(close)]
    sum_v = np.convolve(volume.astype(np.float64), np.ones(period), mode="full")[: len(close)]
    with np.errstate(divide="ignore", invalid="ignore"):
        vwma = np.where(sum_v == 0, np.nan, sum_vc / sum_v)
    vwma[: period - 1] = np.nan
    return vwma


def classify_regime(vwma_8: np.ndarray, vwma_21: np.ndarray, vwma_34: np.ndarray) -> np.ndarray:
    """Classify bars as bullish, bearish, or neutral."""
    regime = np.full(len(vwma_8), "neutral", dtype=object)
    regime[(vwma_8 > vwma_21) & (vwma_21 > vwma_34)] = "bullish"
    regime[(vwma_8 < vwma_21) & (vwma_21 < vwma_34)] = "bearish"
    return regime


def classify_stage(regime: np.ndarray, close: np.ndarray, vma: np.ndarray) -> np.ndarray:
    """Classify the current Market Impulse stage."""
    stage = np.full(len(close), "distribution", dtype=object)
    for index in range(len(close)):
        if np.isnan(vma[index]):
            continue
        if regime[index] == "bullish" and close[index] >= vma[index]:
            stage[index] = "acceleration"
        elif regime[index] == "bearish" and close[index] <= vma[index]:
            stage[index] = "deceleration"
        elif close[index] >= vma[index]:
            stage[index] = "accumulation"
        else:
            stage[index] = "distribution"
    return stage


def enrich_impulse_columns(
    df: pl.DataFrame,
    vma_length: int = 10,
    vwma_periods: tuple[int, ...] = (8, 21, 34),
    suffix: str = "",
) -> pl.DataFrame:
    """Add Market Impulse columns to a DataFrame."""
    close = df["close"].to_numpy().astype(np.float64)
    volume = df["volume"].to_numpy().astype(np.float64)

    vma = compute_vma(close, length=vma_length)
    vwmas = {period: compute_vwma(close, volume, period) for period in vwma_periods}
    regime = classify_regime(vwmas[vwma_periods[0]], vwmas[vwma_periods[1]], vwmas[vwma_periods[2]])
    stage = classify_stage(regime, close, vma)

    columns = [pl.Series(f"vma_{vma_length}{suffix}", vma)]
    for period in vwma_periods:
        columns.append(pl.Series(f"vwma_{period}{suffix}", vwmas[period]))
    columns.append(pl.Series(f"impulse_regime{suffix}", regime))
    columns.append(pl.Series(f"impulse_stage{suffix}", stage))

    enriched = df.with_columns(columns)
    logger.debug(
        "Market Impulse enrichment complete{} - {} bars",
        f" (suffix={suffix})" if suffix else "",
        len(enriched),
    )
    return enriched
