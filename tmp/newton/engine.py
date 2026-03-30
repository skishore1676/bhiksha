"""
Newton Physics Engine

Compatibility facade over a composable feature pipeline.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import polars as pl
from loguru import logger

from src.config import settings
from src.newton.transforms import (
    AccelerationTransform,
    DirectionalMassTransform,
    EmaStackTransform,
    FeatureTransform,
    JerkTransform,
    MarketImpulseTransform,
    VelocityTransform,
    VolumeMaTransform,
    VpocTransform,
    transform_names,
)


class PhysicsEngine:
    """Apply Newton feature transforms to raw market data."""

    def __init__(
        self,
        vpoc_lookback: int = settings.vpoc_lookback_bars,
        ema_periods: Sequence[int] | None = None,
        volume_ma_period: int = settings.volume_ma_period,
        transforms: Sequence[FeatureTransform | str] | None = None,
    ) -> None:
        self.vpoc_lookback = vpoc_lookback
        self.ema_periods = tuple(ema_periods or settings.ema_periods)
        self.volume_ma_period = volume_ma_period
        self._registry = self._build_registry()
        selected = transforms or self._default_transforms()
        self.transforms = self._resolve_transforms(selected)

    @property
    def available_transforms(self) -> list[str]:
        return [*self._registry, "market_impulse[:timeframe]"]

    def enrich(self, df: pl.DataFrame) -> pl.DataFrame:
        required = {"open", "high", "low", "close", "volume"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"DataFrame is missing required columns: {missing}")
        return self._apply_transforms(df, self.transforms)

    def enrich_for_features(self, df: pl.DataFrame, required_features: set[str]) -> pl.DataFrame:
        transforms = self.transforms_for_features(required_features)
        return self._apply_transforms(df, transforms)

    def transforms_for_features(self, required_features: set[str]) -> list[FeatureTransform]:
        candidates: list[FeatureTransform | str] = [
            transform
            for transform in self._registry.values()
            if transform.name in required_features or set(required_features) & transform.output_columns
        ]
        candidates.extend(self._market_impulse_transforms_for_features(required_features))
        return self._resolve_transforms(candidates)

    def _market_impulse_transforms_for_features(
        self,
        required_features: set[str],
    ) -> list[FeatureTransform]:
        requests: dict[str, set[int]] = {}
        base_requested = False
        base_lengths: set[int] = set()

        def add_request(timeframe: str = "5m", vma_length: int | None = None) -> None:
            lengths = requests.setdefault(timeframe, set())
            if vma_length is not None:
                lengths.add(vma_length)

        for feature in required_features:
            spec_match = _MARKET_IMPULSE_SPEC_RE.fullmatch(feature)
            if spec_match:
                add_request(spec_match.group("timeframe") or "5m")
                continue

            impulse_match = _MARKET_IMPULSE_COLUMN_RE.fullmatch(feature)
            if impulse_match:
                timeframe = impulse_match.group("timeframe")
                if timeframe:
                    add_request(timeframe)
                else:
                    base_requested = True
                continue

            vma_match = _MARKET_IMPULSE_VMA_RE.fullmatch(feature)
            if vma_match:
                timeframe = vma_match.group("timeframe")
                vma_length = int(vma_match.group("vma_length"))
                if timeframe:
                    add_request(timeframe, vma_length)
                else:
                    base_requested = True
                    base_lengths.add(vma_length)

        if not requests and (base_requested or base_lengths):
            add_request("5m")

        if len(base_lengths) > 1:
            raise ValueError(
                "Market Impulse feature request mixes multiple base VMA lengths: "
                f"{sorted(base_lengths)}"
            )
        if base_lengths:
            base_length = next(iter(base_lengths))
            for timeframe, lengths in requests.items():
                if not lengths:
                    lengths.add(base_length)
                    continue
                if lengths != {base_length}:
                    raise ValueError(
                        "Market Impulse feature request mixes incompatible VMA lengths "
                        f"for timeframe {timeframe}: base={base_length}, tagged={sorted(lengths)}"
                    )

        transforms: list[FeatureTransform] = []
        for timeframe, lengths in sorted(requests.items()):
            if len(lengths) > 1:
                raise ValueError(
                    "Market Impulse feature request mixes multiple VMA lengths "
                    f"for timeframe {timeframe}: {sorted(lengths)}"
                )
            transforms.append(
                MarketImpulseTransform(
                    vma_length=next(iter(lengths), settings.vma_length),
                    vwma_periods=tuple(settings.vwma_periods),
                    timeframe=timeframe,
                )
            )
        return transforms

    def _build_registry(self) -> dict[str, FeatureTransform]:
        transforms: list[FeatureTransform] = [
            VelocityTransform(),
            AccelerationTransform(),
            JerkTransform(),
            EmaStackTransform(periods=self.ema_periods),
            VolumeMaTransform(period=self.volume_ma_period),
            DirectionalMassTransform(volume_ma_period=self.volume_ma_period),
            VpocTransform(lookback=self.vpoc_lookback),
        ]
        return {transform.name: transform for transform in transforms}

    def _default_transforms(self) -> tuple[FeatureTransform, ...]:
        return (
            self._registry["velocity"],
            self._registry["acceleration"],
            self._registry["jerk"],
            self._registry["ema_stack"],
            self._registry["volume_ma"],
            self._registry["directional_mass"],
            self._registry["vpoc"],
        )

    def _resolve_transforms(
        self,
        transforms: Sequence[FeatureTransform | str],
    ) -> list[FeatureTransform]:
        resolved: list[FeatureTransform] = []
        seen: set[str] = set()

        def add_transform(item: FeatureTransform | str) -> None:
            transform = self._coerce_transform(item)
            if transform.spec in seen:
                return
            for dependency_name in transform.depends_on:
                add_transform(dependency_name)
            resolved.append(transform)
            seen.add(transform.spec)

        for item in transforms:
            add_transform(item)
        return resolved

    def _coerce_transform(self, item: FeatureTransform | str) -> FeatureTransform:
        if isinstance(item, str):
            spec_match = _MARKET_IMPULSE_SPEC_RE.fullmatch(item)
            if spec_match:
                return MarketImpulseTransform(
                    vma_length=settings.vma_length,
                    vwma_periods=tuple(settings.vwma_periods),
                    timeframe=spec_match.group("timeframe") or "5m",
                )
            try:
                return self._registry[item]
            except KeyError as exc:
                raise KeyError(f"Unknown transform {item!r}") from exc
        return item

    def _apply_transforms(
        self,
        df: pl.DataFrame,
        transforms: Sequence[FeatureTransform],
    ) -> pl.DataFrame:
        logger.info(
            "Applying Newton transforms: {}",
            ", ".join(transform_names(transforms)) or "(none)",
        )
        result = df
        for transform in transforms:
            missing = set(transform.required_input_columns) - set(result.columns)
            if missing:
                raise ValueError(
                    f"Transform '{transform.name}' requires columns: {sorted(missing)}"
                )
            result = transform.apply(result)
        logger.info("Physics enrichment complete - {} columns total", len(result.columns))
        return result


_MARKET_IMPULSE_SPEC_RE = re.compile(r"^market_impulse(?::(?P<timeframe>[^:]+))?$")
_MARKET_IMPULSE_COLUMN_RE = re.compile(
    r"^impulse_(?:regime|stage)(?:_(?P<timeframe>[0-9]+[A-Za-z]+))?$"
)
_MARKET_IMPULSE_VMA_RE = re.compile(
    r"^vma_(?P<vma_length>\d+)(?:_(?P<timeframe>[0-9]+[A-Za-z]+))?$"
)
