# Strategy Surface Notes

This file is the companion note for [strategy_surface.yaml](/Users/suman/kg_env/projects/bhiksha/docs/strategy_surface.yaml)
and [strategy_surface_proposed.yaml](/Users/suman/kg_env/projects/bhiksha/docs/strategy_surface_proposed.yaml).
The first YAML is the strict shared contract for Bhiksha-supported surfaces. The
second YAML captures Mala-first surfaces that produced useful evidence but still
need Bhiksha capability work. This note keeps the surrounding context that is
useful for humans but should not be treated as part of the machine-level surface
definition.

## Design Notes

- Keep `strategy_surface.yaml` limited to concrete, implemented, parameterized fields.
- Add new fields to the contract only when Bhiksha can express and honor them.
- Keep derived internal fields out of the contract unless Bhiksha consumes them directly.
- Keep research heuristics and discovery guidance out of the contract.

## Current Constraints

- Bhiksha supports multiple strategy families through a normalized deployment manifest.
- Signal logic is replayable from underlying bars and feature enrichment.
- Time exits are replayable from timestamp rules.
- Strategy exits are replayable only for families that implement `evaluate_exit`.
- Premium stop loss and premium target logic exist in live execution but are not faithfully replayable without option quote history.
- The current live vehicle model is single-leg long premium via option selection constraints.

## Cross-Repo Normalization Notes

- `market_impulse` uses `vma_length` and `regime_timeframe` as the shared contract surface.
- Mala may derive `vma_col` and `regime_col` internally, but those are not first-class contract fields.
- `exit.stop_loss_pct` is the canonical live premium stop knob. `risk.stop_loss_pct` is the default supplied by the risk profile when the exit layer does not override it.

## Surface Classification

- `supported`: Bhiksha can execute the parameter today and the field belongs in `strategy_surface.yaml`.
- `proposed`: Mala used the parameter during research, the result mattered, but Bhiksha cannot execute it yet. Record it in `strategy_surface_proposed.yaml`.
- `derived`: Internal helper fields such as derived feature column names. These should stay out of both registries unless Bhiksha begins consuming them directly.

## Research Guidance Moved Out Of The Contract

Examples of useful research dimensions that should stay out of the strict YAML:

- direction
- entry window shape
- signal threshold or proximity threshold
- smoothing lookback
- volume gate on or off
- volume threshold
- higher timeframe regime choice
- vehicle DTE range
- vehicle delta range
- spread and liquidity filters
- hard flat time
- strategy exit enabled or disabled
- strategy exit strictness
