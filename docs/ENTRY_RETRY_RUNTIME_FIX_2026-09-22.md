# Production entry retry repair — September 22, 2026

## Problem and owner

The scheduled `trade_session` entrypoint uses `app.bootstrap.build_runtime`, which constructs `app.runtime.BhikshaRuntime`. Earlier retry integration and its restart latch lived in the duplicate `active_plan.runtime.BhikshaRuntime`; production never executed those hooks.

FDX demonstrated the consequence: its first contract selection failed on liquidity, a ten-minute retry window opened, but subsequent prices below its trigger were suppressed by the historical first-trigger rule. The window ended after only the initial selection attempt. Separately, IWM filled after repricing, but its fill outcome omitted the original signal ID.

## Implementation

- Wire the existing retry controller into **app/runtime.py**. Fresh intrabar observations can re-evaluate the current trigger during an existing retry window. Completed bars can invalidate that intent but cannot authorize a retry.
- Keep Sheet-owned duration and interval, original deadline, quote freshness, trigger, invalidation, validity, lifecycle, risk and budget checks. Do not re-arm ordinary one-shot manual entries or add retries to scanner lanes.
- Share the small consumed-intent restoration function between callers. Production startup reads the existing attempt ledger and restores only consumed flags, with a startup receipt. It does not replay signals or extend retry deadlines.
- Store original signal identity in the trade plan's risk details. Confirmed fills and terminal unfilled outcomes carry it through order replacement. Fill timestamps remain actual fill timestamps; `signal_timestamp` identifies the original observation.
- Retain the waiting decision in each retry. When a fresh eligible signal advances the retry, close the preceding selection failure with `liquidity_retry_continues`; the fresh attempt receives its own outcome. Expiry/invalidation closes the waiting signal rather than leaving it pending. The deadline reason is `liquidity_retry_deadline_reached`, which does not falsely assert that the trigger stayed valid throughout the window.
- Log waiting liquidity as such, rather than incorrectly reporting a lifecycle block.

The duplicate runtime is not the production repair target. No broad runtime rewrite is included; tests import the runtime through its actual bootstrap to prevent repeating this wiring mistake. Historical events are not rewritten, and consumed FDX is not re-armed by deployment.

## Verification and deployment

Full local suite: **1,317 passed**. Integration cases exercise the production runtime with historical trigger data plus fresh observations: eligible retry, trigger no longer met, invalidation, unarmed intent, interval not elapsed, stale quote and expired deadline. Additional checks cover one terminal expiry event, signal identity through a broker replacement/fill, and existing restart protection.

Deployment requires target-file preimage checks, rollback copies, broker pending-order readback, unchanged compiled Sheet plan, controlled process restart and a new startup receipt proving consumed retry restoration. Tests and process readback do not establish a new natural liquidity-retry fill; that requires a later eligible market event.
