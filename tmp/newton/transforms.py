"""Composable Newton feature transforms."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterable

import numpy as np
import polars as pl
from loguru import logger

from src.newton.market_impulse import enrich_impulse_columns
from src.newton.resampler import TimeframeResampler, timeframe_tag


class FeatureTransform(ABC):
    """A named Newton transform with explicit inputs, outputs, and dependencies."""

    name: str
    depends_on: tuple[str, ...] = ()
    required_input_columns: set[str]

    @property
    def spec(self) -> str:
        """Stable identifier for de-duplicating parameterized transforms."""
        return self.name

    @property
    @abstractmethod
    def output_columns(self) -> set[str]:
        ...

    @abstractmethod
    def apply(self, df: pl.DataFrame) -> pl.DataFrame:
        ...


@dataclass(frozen=True, slots=True)
class VelocityTransform(FeatureTransform):
    name: str = "velocity"
    depends_on: tuple[str, ...] = ()
    required_input_columns: set[str] = frozenset({"close"})

    @property
    def output_columns(self) -> set[str]:
        return {"velocity_1m"}

    def apply(self, df: pl.DataFrame) -> pl.DataFrame:
        return df.with_columns(
            (pl.col("close") - pl.col("close").shift(1)).alias("velocity_1m")
        )


@dataclass(frozen=True, slots=True)
class AccelerationTransform(FeatureTransform):
    name: str = "acceleration"
    depends_on: tuple[str, ...] = ("velocity",)
    required_input_columns: set[str] = frozenset({"velocity_1m"})

    @property
    def output_columns(self) -> set[str]:
        return {"accel_1m"}

    def apply(self, df: pl.DataFrame) -> pl.DataFrame:
        return df.with_columns(
            (pl.col("velocity_1m") - pl.col("velocity_1m").shift(1)).alias("accel_1m")
        )


@dataclass(frozen=True, slots=True)
class JerkTransform(FeatureTransform):
    name: str = "jerk"
    depends_on: tuple[str, ...] = ("acceleration",)
    required_input_columns: set[str] = frozenset({"accel_1m"})

    @property
    def output_columns(self) -> set[str]:
        return {"jerk_1m"}

    def apply(self, df: pl.DataFrame) -> pl.DataFrame:
        return df.with_columns(
            (pl.col("accel_1m") - pl.col("accel_1m").shift(1)).alias("jerk_1m")
        )


@dataclass(frozen=True, slots=True)
class EmaStackTransform(FeatureTransform):
    periods: tuple[int, ...]
    name: str = "ema_stack"
    depends_on: tuple[str, ...] = ()
    required_input_columns: set[str] = frozenset({"close"})

    @property
    def output_columns(self) -> set[str]:
        return {f"ema_{period}" for period in self.periods}

    def apply(self, df: pl.DataFrame) -> pl.DataFrame:
        return df.with_columns(
            [
                pl.col("close").ewm_mean(span=period, adjust=False).alias(f"ema_{period}")
                for period in self.periods
            ]
        )


@dataclass(frozen=True, slots=True)
class VolumeMaTransform(FeatureTransform):
    period: int
    name: str = "volume_ma"
    depends_on: tuple[str, ...] = ()
    required_input_columns: set[str] = frozenset({"volume"})

    @property
    def output_columns(self) -> set[str]:
        return {f"volume_ma_{self.period}"}

    def apply(self, df: pl.DataFrame) -> pl.DataFrame:
        return df.with_columns(
            pl.col("volume").rolling_mean(window_size=self.period).alias(f"volume_ma_{self.period}")
        )


@dataclass(frozen=True, slots=True)
class DirectionalMassTransform(FeatureTransform):
    volume_ma_period: int
    name: str = "directional_mass"
    depends_on: tuple[str, ...] = ()
    required_input_columns: set[str] = frozenset({"high", "low", "close", "volume"})

    @property
    def output_columns(self) -> set[str]:
        return {
            "internal_strength",
            "directional_mass",
            f"directional_mass_ma_{self.volume_ma_period}",
        }

    def apply(self, df: pl.DataFrame) -> pl.DataFrame:
        range_expr = pl.col("high") - pl.col("low")
        internal_strength = (
            pl.when(range_expr == 0)
            .then(pl.lit(0.0))
            .otherwise(
                ((pl.col("close") - pl.col("low")) - (pl.col("high") - pl.col("close")))
                / range_expr
            )
        )
        return df.with_columns([
            internal_strength.alias("internal_strength"),
            (pl.col("volume") * internal_strength).alias("directional_mass"),
        ]).with_columns(
            pl.col("directional_mass")
            .rolling_mean(window_size=self.volume_ma_period)
            .alias(f"directional_mass_ma_{self.volume_ma_period}")
        )


@dataclass(frozen=True, slots=True)
class VpocTransform(FeatureTransform):
    lookback: int
    name: str = "vpoc"
    depends_on: tuple[str, ...] = ()
    required_input_columns: set[str] = frozenset({"close", "high", "low", "volume"})

    @property
    def output_columns(self) -> set[str]:
        return {"vpoc_4h"}

    def apply(self, df: pl.DataFrame) -> pl.DataFrame:
        close = df["close"].to_numpy()
        high = df["high"].to_numpy()
        low = df["low"].to_numpy()
        volume = df["volume"].to_numpy().astype(np.float64)
        n = len(df)

        vpoc = np.full(n, np.nan)
        for i in range(self.lookback, n):
            window_start = i - self.lookback
            typical = (high[window_start:i] + low[window_start:i] + close[window_start:i]) / 3.0
            vol_slice = volume[window_start:i]
            price_bins = np.round(typical, 2)
            unique_prices, inverse = np.unique(price_bins, return_inverse=True)
            vol_by_price = np.zeros(len(unique_prices))
            np.add.at(vol_by_price, inverse, vol_slice)
            vpoc[i] = unique_prices[np.argmax(vol_by_price)]

        return df.with_columns(pl.Series("vpoc_4h", vpoc))


@dataclass(frozen=True, slots=True)
class MarketImpulseTransform(FeatureTransform):
    vma_length: int = 10
    vwma_periods: tuple[int, ...] = (8, 21, 34)
    timeframe: str = "5m"
    market_open: tuple[int, int] = (9, 30)
    market_close: tuple[int, int] = (16, 0)
    name: str = "market_impulse"
    depends_on: tuple[str, ...] = ()
    required_input_columns: set[str] = frozenset({"timestamp", "open", "high", "low", "close", "volume"})

    @property
    def spec(self) -> str:
        return f"{self.name}:{self.timeframe}:vma_{self.vma_length}"

    @property
    def output_columns(self) -> set[str]:
        tag = timeframe_tag(self.timeframe)
        columns = {
            f"vma_{self.vma_length}",
            "impulse_regime",
            "impulse_stage",
            f"vma_{self.vma_length}_{tag}",
            f"impulse_regime_{tag}",
            f"impulse_stage_{tag}",
        }
        columns.update(f"vwma_{period}" for period in self.vwma_periods)
        return columns

    def apply(self, df: pl.DataFrame) -> pl.DataFrame:
        resampler = TimeframeResampler()
        market_df = resampler.filter_market_hours(
            df,
            market_open=self.market_open,
            market_close=self.market_close,
        )
        if market_df.is_empty():
            logger.warning(
                "Market Impulse transform produced no in-session bars for timeframe {}",
                self.timeframe,
            )
            return market_df
        market_df = enrich_impulse_columns(
            market_df,
            vma_length=self.vma_length,
            vwma_periods=self.vwma_periods,
            suffix="",
        )

        tag = timeframe_tag(self.timeframe)
        timeframe_df = resampler.resample_ohlcv(market_df, every=self.timeframe)
        timeframe_df = enrich_impulse_columns(
            timeframe_df,
            vma_length=self.vma_length,
            vwma_periods=self.vwma_periods,
            suffix=f"_{tag}",
        )

        feature_columns = [
            f"impulse_regime_{tag}",
            f"impulse_stage_{tag}",
            f"vma_{self.vma_length}_{tag}",
        ]
        joined = resampler.join_timeframe_features(
            market_df,
            timeframe_df,
            every=self.timeframe,
            feature_columns=feature_columns,
        )
        logger.info(
            "Market Impulse transform complete: {} base bars, {} {} bars",
            len(joined),
            len(timeframe_df),
            self.timeframe,
        )
        return joined


def transform_names(transforms: Iterable[FeatureTransform]) -> list[str]:
    return [transform.spec for transform in transforms]
