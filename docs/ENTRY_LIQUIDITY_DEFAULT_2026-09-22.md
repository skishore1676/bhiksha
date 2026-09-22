# Default single-leg entry liquidity policy

Decision: Bhiksha uses one entry policy for live and shadow lanes. The Google
Sheet remains the operator's authority for DTE, delta, premium, authorization,
and the existing OI/spread thresholds. Those OI/spread values describe the
preferred market; a contract outside them can receive a more favorable limit.
They are not a reason by themselves to discard a valid signal. No new per-row
activation switch is required.

## Selection and pricing

1. Keep the Sheet DTE and delta constraints. Selection requires positive OI;
   the later fresh option quote must reject missing, crossed, nonpositive,
   nonfinite, or absurd markets. Preserve one
   bounded later-expiry fallback where the Sheet authorizes it.
2. Rank eligible contracts by the existing delta/DTE rule. Persist the chosen
   contract and the original chain snapshot. Mark OI below the preferred floor
   or spread above the preferred width as liquidity pressure.
3. For a normal quote, retain the selected lane's existing entry profile. For
   liquidity pressure, submit a buy limit below midpoint using the existing
   nonlinear width/OI discount. Reprice that *same* contract first to the
   original midpoint, then at most $0.10 above it. The original chase cap,
   maximum trade premium, cash, preflight, quote-quality and order-reconciliation
   gates still apply. Expire an unfilled order at the lane's existing deadline.
4. The quote used to change a price must be fresh and two-sided. Live records a
   fill only from broker truth. Shadow uses a later fresh ask at or below the
   effective resting limit; it does not invent inside-spread fills. A shadow
   no-fill is evidence about this conservative model, not proof of a live miss.

## DTE and authority

The operator Sheet should use 0-7 DTE with a bounded 14-DTE later-expiry
fallback for single-stock rows that currently request 0-3 DTE, retaining rows
whose research deliberately requests a different range. A 0-7 primary window
can change the selected contract because selection ranks delta before DTE;
record the selected DTE and fallback source on each attempt.

Code owns the common algorithm; Sheet values and compiled-plan receipts own
operator choices. Do not introduce a second scheduler, service, database, or
per-ticker pricing code. Existing entry order, risk, and lifecycle owners remain
in place. This design changes no exit or trade authorization rule.
