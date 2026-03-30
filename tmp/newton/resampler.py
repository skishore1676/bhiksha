"""Reusable multi-timeframe resampling helpers."""

from __future__ import annotations

from datetime import time as dt_time

import polars as pl
from loguru import logger

from src.time_utils import et_time_expr


def timeframe_tag(every: str) -> str:
    return every.strip().lower()


class TimeframeResampler:
    """Resample OHLCV bars and safely align higher-timeframe features back down."""

    def __init__(self, timestamp_col: str = "timestamp") -> None:
        self.timestamp_col = timestamp_col

    def filter_market_hours(
        self,
        df: pl.DataFrame,
        *,
        market_open: tuple[int, int] = (9, 30),
        market_close: tuple[int, int] = (16, 0),
    ) -> pl.DataFrame:
        if self.timestamp_col not in df.columns:
            logger.warning("No {} column; skipping market-hours filter.", self.timestamp_col)
            return df

        mkt_open = dt_time(market_open[0], market_open[1])
        mkt_close = dt_time(market_close[0], market_close[1])
        before = len(df)
        filtered = df.filter(
            (et_time_expr(self.timestamp_col) >= mkt_open)
            & (et_time_expr(self.timestamp_col) <= mkt_close)
        )
        dropped = before - len(filtered)
        if dropped > 0:
            logger.info(
                "Filtered to market hours ({} - {}): dropped {} bars, {} remaining",
                mkt_open,
                mkt_close,
                dropped,
                len(filtered),
            )
        return filtered

    def resample_ohlcv(self, df: pl.DataFrame, *, every: str) -> pl.DataFrame:
        required = {self.timestamp_col, "open", "high", "low", "close", "volume"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"Cannot resample OHLCV without columns: {sorted(missing)}")

        return (
            df.sort(self.timestamp_col)
            .group_by_dynamic(self.timestamp_col, every=every)
            .agg([
                pl.col("open").first().alias("open"),
                pl.col("high").max().alias("high"),
                pl.col("low").min().alias("low"),
                pl.col("close").last().alias("close"),
                pl.col("volume").sum().alias("volume"),
            ])
        )

    def join_timeframe_features(
        self,
        base_df: pl.DataFrame,
        timeframe_df: pl.DataFrame,
        *,
        every: str,
        feature_columns: list[str],
    ) -> pl.DataFrame:
        if self.timestamp_col not in base_df.columns:
            raise ValueError(f"Base DataFrame is missing {self.timestamp_col!r}")
        if self.timestamp_col not in timeframe_df.columns:
            raise ValueError(f"Timeframe DataFrame is missing {self.timestamp_col!r}")

        tag = timeframe_tag(every)
        join_key = f"ts_{tag}"
        selected = [self.timestamp_col, *[c for c in feature_columns if c in timeframe_df.columns]]
        right = timeframe_df.select(selected).rename({self.timestamp_col: join_key}).sort(join_key)

        joined = base_df.sort(self.timestamp_col).join_asof(
            right,
            left_on=self.timestamp_col,
            right_on=join_key,
            strategy="backward",
        )
        if join_key in joined.columns:
            joined = joined.drop(join_key)
        return joined
