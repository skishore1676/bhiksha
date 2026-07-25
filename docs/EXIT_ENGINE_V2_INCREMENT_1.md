# Exit Engine V2 Increment 1 — Bhiksha Implementation

The cross-repository product contract is owned by TradeLab:
`docs/EXIT_ENGINE_V2_INCREMENT_1.md` in the sibling `tradelab` repository. This
document records only Bhiksha's implementation and operating boundary.

## Shipped boundary

- `active_plan` plus `startup_config` remain the only session configuration
  authority.
- Active-plan compilation resolves legacy profile labels to explicit
  `exit-policy.v1` numbers, then hashes the fully resolved policy after
  row-level precedence. Bhiksha-native management dials are frozen under the
  policy `parameters` bag, including the no-progress favorable floor and
  legacy target/breakeven controls.
- Each confirmed trade freezes one immutable policy snapshot and initializes
  versioned runtime state in the existing runtime SQLite database.
- Profile-exit events carry stable trade, policy, state, and quote lineage.
- Partial-scale and breakeven broker effects use durable action intents.
  Confirmed broker readback advances banked/breakeven state; unresolved effects
  block duplicates. The intent key is also submitted as Public's client order
  id, so a restart between broker acceptance and the local bind can recover the
  exact order.
- Missing, contradictory, or ambiguous restart state emits `STATE_DEGRADED`,
  persists that status, keeps every profile action closed, and retains or
  restores only the last proved protection. Recovery never invents a
  historical peak and never substitutes the current session policy.
- The generated session manifest is a review receipt, not a second source of
  configuration.
- Exit Edge Lab owns Control/Variant A/Variant B counterfactual evidence in its
  separate sidecar store. A candidate floor advances only after the configured
  `risk_envelope_ratchet_step_r` is crossed.

Canonical identity excludes the friendly giveback label and
`source_config_id`; both remain in the frozen snapshot for audit/provenance.
Explicit giveback numbers and all executable fields remain hashed.

## Safety boundary

Increment 1 adds no live Dynamic Risk Envelope switch and no envelope path to an
order manager, broker client, cancel, replace, or dispatch function. Existing
target, stop, partial, giveback, sizing, and native-exit economics remain the
live control. The profile route's pre-existing live gate is unchanged.

Existing open trades that predate an immutable policy snapshot do not silently
adopt the current session policy. They enter visible degraded recovery and
continue only through facts and protection paths the runtime can prove.

## Runtime evidence

At startup, Bhiksha emits the effective exit policy records in `startup_config`
and writes:

```text
<playbook_artifacts_dir>/session_manifests/session_manifest_<trading-date>_<config-fingerprint>.json
<playbook_artifacts_dir>/session_manifests/session_manifest_<trading-date>_<config-fingerprint>.md
```

For each material transition, audit the SQLite policy snapshot, runtime state,
action intent, trade/fill record, identified event, and broker readback
together. A local state assertion or accepted order response is not broker
fill proof. Local banked quantity and residual protection advance only after an
identified SELL/CLOSE fill readback.

## Release gate

Deploy only at the normal post-flat session boundary after:

1. kernel, Bhiksha, PAT, and TradeLab suites are green;
2. current-behavior golden fixtures are unchanged;
3. at least two fresh adversarial money-path audit rounds pass;
4. oldmac is confirmed flat and its checkout/dirtiness are preserved; and
5. post-deploy readback proves commit/tree, launchd health, startup policy
   identity, and fresh state/manifest output.

## 2026-07-24 deployment proof

Increment 1 passed two independent adversarial reviews and the complete Air
suite. The oldmac deployment then exposed and closed one observational
Exit Edge SQLite schema-startup race; the bounded readback fix passed the full
`1001`-test suite on both Air and oldmac. The production database remained flat
through deployment, and all Bhiksha launchd jobs remained loaded but stopped
after the session boundary.

The Dynamic Risk Envelope remains broker-inert and default-off. Deployment did
not edit the Google Sheet, enable the live profile route, start the trading
runtime, or submit/cancel/replace any broker order.

## Weekly evidence projection

The Friday Bhiksha job now emits a stable
`bhiksha.exit_edge_weekly_evidence.v1` receipt and binds it into the
content-digested `bhiksha.weekly_trading_decisions.v1` packet. The receipt
reports current-week and cumulative collection coverage, missingness,
Control-versus-candidate descriptive outcomes, freshness, and the zero-broker /
zero-live-activation guardrails.

TradeLab owns the executive interpretation. Missing, stale, censored, or
incomplete observation data remains an explicit evidence state; it is never
converted to zero uplift. The existing profile-versus-legacy confidence
indicator cannot promote Candidate A or B. All Increment 1 receipts remain
advisory and set `decision_ready=false`.
