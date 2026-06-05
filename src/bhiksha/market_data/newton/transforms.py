"""Composable Newton feature transforms."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import time as dt_time

import numpy as np
import polars as pl
from loguru import logger

from bhiksha.market_data.session import et_date_expr, et_time_expr
from bhiksha.market_data.newton.market_impulse import enrich_impulse_columns
from bhiksha.market_data.newton.resampler import TimeframeResampler, timeframe_tag


def velocity_column_name(periods_back: int) -> str:
    return "velocity_1m" if periods_back == 1 else f"velocity_{periods_back}"


def acceleration_column_name(periods_back: int) -> str:
    return "accel_1m" if periods_back == 1 else f"accel_{periods_back}"


def jerk_column_name(periods_back: int) -> str:
    return "jerk_1m" if periods_back == 1 else f"jerk_{periods_back}"


def validate_periods_back(periods_back: int) -> int:
    normalized = int(periods_back)
    if normalized <= 0:
        raise ValueError("periods_back must be a positive integer.")
    return normalized


class FeatureTransform(ABC):
    """A named Newton transform with explicit inputs and outputs."""

    name: str
    depends_on: tuple[str, ...] = ()
    required_input_columns: set[str]

    @property
    def spec(self) -> str:
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
    periods_back: int = 1
    name: str = "velocity"
    depends_on: tuple[str, ...] = ()
    required_input_columns: set[str] = frozenset({"close"})

    def __post_init__(self) -> None:
        object.__setattr__(self, "periods_back", validate_periods_back(self.periods_back))

    @property
    def spec(self) -> str:
        return self.name if self.periods_back == 1 else f"{self.name}:{self.periods_back}"

    @property
    def output_columns(self) -> set[str]:
        return {velocity_column_name(self.periods_back)}

    def apply(self, df: pl.DataFrame) -> pl.DataFrame:
        return df.with_columns(
            (pl.col("close") - pl.col("close").shift(self.periods_back))
            .alias(velocity_column_name(self.periods_back))
        )


@dataclass(frozen=True, slots=True)
class AccelerationTransform(FeatureTransform):
    periods_back: int = 1
    name: str = "acceleration"

    def __post_init__(self) -> None:
        object.__setattr__(self, "periods_back", validate_periods_back(self.periods_back))

    @property
    def spec(self) -> str:
        return self.name if self.periods_back == 1 else f"{self.name}:{self.periods_back}"

    @property
    def depends_on(self) -> tuple[str, ...]:
        return ("velocity" if self.periods_back == 1 else f"velocity:{self.periods_back}",)

    @property
    def required_input_columns(self) -> set[str]:
        return {velocity_column_name(self.periods_back)}

    @property
    def output_columns(self) -> set[str]:
        return {acceleration_column_name(self.periods_back)}

    def apply(self, df: pl.DataFrame) -> pl.DataFrame:
        velocity_col = velocity_column_name(self.periods_back)
        return df.with_columns(
            (pl.col(velocity_col) - pl.col(velocity_col).shift(self.periods_back))
            .alias(acceleration_column_name(self.periods_back))
        )


@dataclass(frozen=True, slots=True)
class JerkTransform(FeatureTransform):
    periods_back: int = 1
    name: str = "jerk"

    def __post_init__(self) -> None:
        object.__setattr__(self, "periods_back", validate_periods_back(self.periods_back))

    @property
    def spec(self) -> str:
        return self.name if self.periods_back == 1 else f"{self.name}:{self.periods_back}"

    @property
    def depends_on(self) -> tuple[str, ...]:
        return (
            "acceleration" if self.periods_back == 1 else f"acceleration:{self.periods_back}",
        )

    @property
    def required_input_columns(self) -> set[str]:
        return {acceleration_column_name(self.periods_back)}

    @property
    def output_columns(self) -> set[str]:
        return {jerk_column_name(self.periods_back)}

    def apply(self, df: pl.DataFrame) -> pl.DataFrame:
        accel_col = acceleration_column_name(self.periods_back)
        return df.with_columns(
            (pl.col(accel_col) - pl.col(accel_col).shift(self.periods_back))
            .alias(jerk_column_name(self.periods_back))
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
            [pl.col("close").ewm_mean(span=period, adjust=False).alias(f"ema_{period}") for period in self.periods]
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
class OpeningVwapRthTransform(FeatureTransform):
    """Regular-session cumulative VWAP starting at 09:30 ET."""

    market_open: tuple[int, int] = (9, 30)
    market_close: tuple[int, int] = (16, 0)
    name: str = "opening_vwap_rth"
    depends_on: tuple[str, ...] = ()
    required_input_columns: set[str] = frozenset({"timestamp", "close", "volume"})

    @property
    def output_columns(self) -> set[str]:
        return {"opening_vwap_rth"}

    def apply(self, df: pl.DataFrame) -> pl.DataFrame:
        trade_date_col = "_opening_vwap_rth_trade_date"
        is_rth_col = "_opening_vwap_rth_is_rth"
        pv_col = "_opening_vwap_rth_pv"
        volume_col = "_opening_vwap_rth_volume"
        mkt_open = dt_time(*self.market_open)
        mkt_close = dt_time(*self.market_close)
        rth_expr = (et_time_expr("timestamp") >= mkt_open) & (
            et_time_expr("timestamp") <= mkt_close
        )
        return (
            df.with_columns(
                [
                    et_date_expr("timestamp").alias(trade_date_col),
                    rth_expr.alias(is_rth_col),
                ]
            )
            .with_columns(
                [
                    pl.when(pl.col(is_rth_col))
                    .then(pl.col("close") * pl.col("volume"))
                    .otherwise(0.0)
                    .cum_sum()
                    .over(trade_date_col)
                    .alias(pv_col),
                    pl.when(pl.col(is_rth_col))
                    .then(pl.col("volume"))
                    .otherwise(0.0)
                    .cum_sum()
                    .over(trade_date_col)
                    .alias(volume_col),
                ]
            )
            .with_columns(
                pl.when(pl.col(is_rth_col) & (pl.col(volume_col) > 0))
                .then(pl.col(pv_col) / pl.col(volume_col))
                .otherwise(None)
                .alias("opening_vwap_rth")
            )
            .drop([trade_date_col, is_rth_col, pv_col, volume_col])
        )


@dataclass(frozen=True, slots=True)
class PriorRthCloseAtrTransform(FeatureTransform):
    """Prior regular-session close, RTH ATR, and RTH-open gap context."""

    atr_window: int = 14
    small_gap_atr: float = 0.25
    large_gap_atr: float = 1.0
    market_open: tuple[int, int] = (9, 30)
    market_close: tuple[int, int] = (16, 0)
    name: str = "prior_rth_close_atr"
    depends_on: tuple[str, ...] = ()
    required_input_columns: set[str] = frozenset({"timestamp", "open", "high", "low", "close"})

    def __post_init__(self) -> None:
        object.__setattr__(self, "atr_window", max(2, int(self.atr_window)))
        object.__setattr__(self, "small_gap_atr", abs(float(self.small_gap_atr)))
        object.__setattr__(self, "large_gap_atr", abs(float(self.large_gap_atr)))

    @property
    def output_columns(self) -> set[str]:
        return {
            "prior_rth_close",
            f"daily_rth_atr_{self.atr_window}",
            "atr_distance_from_prior_rth_close",
            "gap_rth_atr",
            "gap_state_rth_open",
        }

    def apply(self, df: pl.DataFrame) -> pl.DataFrame:
        trade_date_col = "_prior_rth_close_trade_date"
        is_rth_col = "_prior_rth_close_is_rth"
        is_at_or_after_open_col = "_prior_rth_close_is_at_or_after_open"
        atr_col = f"daily_rth_atr_{self.atr_window}"
        mkt_open = dt_time(*self.market_open)
        mkt_close = dt_time(*self.market_close)
        bar_time = et_time_expr("timestamp")
        rth_expr = (bar_time >= mkt_open) & (bar_time <= mkt_close)
        dated = df.with_columns(
            [
                et_date_expr("timestamp").alias(trade_date_col),
                rth_expr.alias(is_rth_col),
                (bar_time >= mkt_open).alias(is_at_or_after_open_col),
            ]
        )
        daily = (
            dated.filter(pl.col(is_rth_col))
            .group_by(trade_date_col, maintain_order=True)
            .agg(
                [
                    pl.col("open").first().alias("_daily_rth_open"),
                    pl.col("high").max().alias("_daily_rth_high"),
                    pl.col("low").min().alias("_daily_rth_low"),
                    pl.col("close").last().alias("_daily_rth_close"),
                ]
            )
            .sort(trade_date_col)
            .with_columns(pl.col("_daily_rth_close").shift(1).alias("prior_rth_close"))
            .with_columns(
                pl.when(pl.col("prior_rth_close").is_null())
                .then(pl.col("_daily_rth_high") - pl.col("_daily_rth_low"))
                .otherwise(
                    pl.max_horizontal(
                        pl.col("_daily_rth_high") - pl.col("_daily_rth_low"),
                        (pl.col("_daily_rth_high") - pl.col("prior_rth_close")).abs(),
                        (pl.col("_daily_rth_low") - pl.col("prior_rth_close")).abs(),
                    )
                )
                .alias("_rth_true_range")
            )
            .with_columns(
                pl.col("_rth_true_range")
                .rolling_mean(window_size=self.atr_window, min_samples=1)
                .shift(1)
                .alias(atr_col)
            )
            .with_columns(
                pl.when((pl.col(atr_col) > 0) & pl.col("prior_rth_close").is_not_null())
                .then((pl.col("_daily_rth_open") - pl.col("prior_rth_close")) / pl.col(atr_col))
                .otherwise(None)
                .alias("gap_rth_atr")
            )
            .with_columns(self._gap_state_expr().alias("gap_state_rth_open"))
            .select([trade_date_col, "prior_rth_close", atr_col, "gap_rth_atr", "gap_state_rth_open"])
        )
        return (
            dated.join(daily, on=trade_date_col, how="left")
            .with_columns(
                [
                    pl.when((pl.col(atr_col) > 0) & pl.col("prior_rth_close").is_not_null())
                    .then((pl.col("close") - pl.col("prior_rth_close")) / pl.col(atr_col))
                    .otherwise(None)
                    .alias("atr_distance_from_prior_rth_close"),
                    pl.when(pl.col(is_at_or_after_open_col))
                    .then(pl.col("gap_rth_atr"))
                    .otherwise(None)
                    .alias("gap_rth_atr"),
                    pl.when(pl.col(is_at_or_after_open_col))
                    .then(pl.col("gap_state_rth_open"))
                    .otherwise(None)
                    .alias("gap_state_rth_open"),
                ]
            )
            .drop([trade_date_col, is_rth_col, is_at_or_after_open_col])
        )

    def _gap_state_expr(self) -> pl.Expr:
        large = self.large_gap_atr
        small = self.small_gap_atr
        gap = pl.col("gap_rth_atr")
        return (
            pl.when(gap.is_null())
            .then(pl.lit(None))
            .when(gap >= large)
            .then(pl.lit("gap_up_large"))
            .when(gap >= small)
            .then(pl.lit("gap_up_small"))
            .when(gap <= -large)
            .then(pl.lit("gap_down_large"))
            .when(gap <= -small)
            .then(pl.lit("gap_down_small"))
            .otherwise(pl.lit("flat"))
        )


@dataclass(frozen=True, slots=True)
class RelativeVolumeRthTransform(FeatureTransform):
    """Relative volume over a rolling baseline of regular-session bars only."""

    period: int
    market_open: tuple[int, int] = (9, 30)
    market_close: tuple[int, int] = (16, 0)
    name: str = "relative_volume_rth"
    depends_on: tuple[str, ...] = ()
    required_input_columns: set[str] = frozenset({"timestamp", "volume"})

    def __post_init__(self) -> None:
        object.__setattr__(self, "period", max(2, int(self.period)))

    @property
    def spec(self) -> str:
        return f"{self.name}:{self.period}"

    @property
    def output_columns(self) -> set[str]:
        return {f"relative_volume_rth_{self.period}"}

    def apply(self, df: pl.DataFrame) -> pl.DataFrame:
        output_col = f"relative_volume_rth_{self.period}"
        ma_col = f"_relative_volume_rth_ma_{self.period}"
        is_rth_col = "_relative_volume_rth_is_rth"
        row_col = "_relative_volume_rth_row"
        mkt_open = dt_time(*self.market_open)
        mkt_close = dt_time(*self.market_close)
        rth_expr = (et_time_expr("timestamp") >= mkt_open) & (
            et_time_expr("timestamp") <= mkt_close
        )
        prepared = df.with_row_index(row_col).with_columns(rth_expr.alias(is_rth_col))
        rth_values = (
            prepared.filter(pl.col(is_rth_col))
            .with_columns(pl.col("volume").rolling_mean(window_size=self.period).alias(ma_col))
            .with_columns(
                pl.when(pl.col(ma_col) > 0)
                .then(pl.col("volume") / pl.col(ma_col))
                .otherwise(None)
                .alias(output_col)
            )
            .select([row_col, output_col])
        )
        return prepared.join(rth_values, on=row_col, how="left").drop([row_col, is_rth_col])


@dataclass(frozen=True, slots=True)
class DirectionalMassTransform(FeatureTransform):
    volume_ma_period: int
    name: str = "directional_mass"
    depends_on: tuple[str, ...] = ()
    required_input_columns: set[str] = frozenset({"high", "low", "close", "volume"})

    @property
    def output_columns(self) -> set[str]:
        return {"internal_strength", "directional_mass", f"directional_mass_ma_{self.volume_ma_period}"}

    def apply(self, df: pl.DataFrame) -> pl.DataFrame:
        range_expr = pl.col("high") - pl.col("low")
        internal_strength = (
            pl.when(range_expr == 0)
            .then(pl.lit(0.0))
            .otherwise(((pl.col("close") - pl.col("low")) - (pl.col("high") - pl.col("close"))) / range_expr)
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
        for index in range(self.lookback, n):
            window_start = index - self.lookback
            typical = (high[window_start:index] + low[window_start:index] + close[window_start:index]) / 3.0
            vol_slice = volume[window_start:index]
            price_bins = np.round(typical, 2)
            unique_prices, inverse = np.unique(price_bins, return_inverse=True)
            vol_by_price = np.zeros(len(unique_prices))
            np.add.at(vol_by_price, inverse, vol_slice)
            vpoc[index] = unique_prices[np.argmax(vol_by_price)]
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
            "market_pulse_stage",
            "vwma_stage",
            f"vma_{self.vma_length}_{tag}",
            f"impulse_regime_{tag}",
            f"impulse_stage_{tag}",
            f"market_pulse_stage_{tag}",
            f"vwma_stage_{tag}",
        }
        columns.update(f"vwma_{period}" for period in self.vwma_periods)
        return columns

    def apply(self, df: pl.DataFrame) -> pl.DataFrame:
        resampler = TimeframeResampler()
        market_df = resampler.filter_market_hours(df, market_open=self.market_open, market_close=self.market_close)
        if market_df.is_empty():
            logger.warning("Market Impulse transform produced no in-session bars for timeframe {}", self.timeframe)
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
            f"market_pulse_stage_{tag}",
            f"vwma_stage_{tag}",
            f"vma_{self.vma_length}_{tag}",
        ]
        joined = resampler.join_timeframe_features(
            market_df,
            timeframe_df,
            every=self.timeframe,
            feature_columns=feature_columns,
        )
        logger.info("Joined Market Impulse timeframe features for {}", self.timeframe)
        return joined


def transform_names(transforms: list[FeatureTransform]) -> list[str]:
    return [transform.spec for transform in transforms]
