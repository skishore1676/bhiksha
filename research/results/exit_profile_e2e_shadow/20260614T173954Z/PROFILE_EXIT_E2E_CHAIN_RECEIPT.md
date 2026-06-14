# Exit-Profile E2E Chain Shadow Receipt (config bridge)

- Generated: `2026-06-14T17:39:54.842888+00:00`
- Gate state: **CLOSED (shadow)**
- **gate CLOSED — dispatched=0, orders=0, broker untouched, no Sheet write.**

## Chain input — real Phase-3 mala proposal

- source: `file:/Users/suman/code/mala_v2/.claude/worktrees/agent-ab6661b1de219b8b7/research/results/exit_profile_classify_propose/20260614T154615Z/market-impulse-all-basket-discovery__amd_short.json`
- catalog_key: `market-impulse-all-basket-discovery__amd_short`
- symbol / direction: **AMD short**
- classified profile: **TREND_CONTINUATION**
- chosen: **profile** (profile-vs-legacy)
- selection rule: _profile expectancy ahead of legacy by +4.100 pct -> operator profile wins outright_
- spec policy_id: `profile__trend_continuation`

## Bridge proof — compiled DeploymentManifest.exit (ExitSpec) fields

- suppressed: `[]` | deployments: **1** | deployment_id: `amd_short_profile_e2e` | symbol: `AMD`

| ExitSpec field | value | expected | from |
|----------------|-------|----------|------|
| `profile_exit_id` | `profile__trend_continuation` | `profile__trend_continuation` | spec.policy_id |
| `target_1_r` | `1.0` | `1.0` | spec.target_1_r |
| `target_2_r` | `2.0` | `2.0` | spec.target_2_r |
| `target_1_quantity` | `0.6` | `0.6` | spec.target_1_quantity |
| `initial_stop_pct` | `0.35` | `0.35` | spec.initial_stop_pct |
| `premium_disaster_stop_pct` | `0.35` | `0.35` | spec.premium_disaster_stop_pct |
| `no_progress_seconds` | `2700` | `2700` | spec.no_progress_seconds |
| `max_hold_seconds` | `10800` | `10800` | spec.max_hold_seconds |
| `high_water_giveback_policy` | `MODERATE` | `MODERATE` | spec.high_water_giveback_policy |
| `breakeven_after_t1` | `True` | `True` | spec.breakeven_after_t1 |
| `eod_flat` | `True` | `True` | spec.eod_flat |
| `stop_loss_pct` | `0.45` | `0.45` | spec.option_stop_fallback_pct -> exit.stop_loss_pct |
| `hard_flat_time_et` | `15:55` | `15:55` | spec.hard_flat_time_et |
| `profile_exit_drives_live` | `False` | `False` | bridge boundary (never enables live) |
| `profile_exit_shadow_only` | `True` | `True` | bridge boundary (stays shadow) |

> Every field above survived the chain `kernel ManagementPolicySpec -> Sheet cell (management_policy_spec) -> compile_active_plan_from_sheet -> ExitSpec`. `profile_exit_drives_live=False` + `profile_exit_shadow_only=True` prove the bridge never enables live.

## Replayed shadow decisions (ProfileExitFields.from_exit_spec(deployment.exit))

- ladder rungs observed across paths: eod_flat, high_water_giveback, target_1_partial
- breakeven actions emitted: **2**
- shadow events recorded: **9**
- dispatched: **0** | exit_decision objects built: **False** | no dispatch occurred: **True**
- all expected terminals matched: **True**

### trend_continuation_giveback

_rise through ~1R (bank 60%), ratchet to breakeven, peak the runner just shy of T2, then surrender half the peak excursion (MODERATE giveback)._

- entry premium `3.0`, qty `5` | expected terminal `high_water_giveback` -> observed `high_water_giveback` (match: **True**)
- target-1 partial fired: **True** | breakeven emitted: **True** | rungs fired: `['target_1_partial', 'target_1_partial', 'high_water_giveback']` | state persisted then cleared: **True** | any dispatch: **False**

| min | bar ET | premium | rule | fsm action | exit | qty | dispatched |
|----:|:------:|--------:|------|-----------|:----:|----:|:----------:|
| 2 | 10:00 | 3.40 | hold | hold |  |  | n |
| 6 | 10:00 | 4.10 | target_1_partial | partial_scale | Y | 3 | n |
| 10 | 10:00 | 4.60 | target_1_partial | stop_to_breakeven |  |  | n |
| 15 | 10:00 | 4.90 | hold | hold |  |  | n |
| 20 | 10:00 | 3.90 | high_water_giveback | square_off | Y | 2 | n |

### trend_continuation_hard_flat

_rise through ~1R (bank 60%), ratchet to breakeven, ride the runner, then the 15:55 bar clock forces the operator's EOD hard-flat._

- entry premium `3.0`, qty `5` | expected terminal `eod_flat` -> observed `eod_flat` (match: **True**)
- target-1 partial fired: **True** | breakeven emitted: **True** | rungs fired: `['target_1_partial', 'target_1_partial', 'eod_flat']` | state persisted then cleared: **True** | any dispatch: **False**

| min | bar ET | premium | rule | fsm action | exit | qty | dispatched |
|----:|:------:|--------:|------|-----------|:----:|----:|:----------:|
| 2 | 10:00 | 4.10 | target_1_partial | partial_scale | Y | 3 | n |
| 6 | 15:30 | 4.50 | target_1_partial | stop_to_breakeven |  |  | n |
| 10 | 15:50 | 4.70 | hold | hold |  |  | n |
| 14 | 15:56 | 4.65 | eod_flat | hard_flat | Y | 2 | n |

---
**gate CLOSED — dispatched=0, orders=0, broker untouched, no Sheet write.**

All ticks were recorded as `shadow_record` (gate closed). No ExitDecision was built for dispatch, no order was placed, no broker/live API was touched, and no Google Sheet was written. The active_plan input was an in-process fixture CSV; the event sink was in-memory.
