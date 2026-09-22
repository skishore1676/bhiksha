# Monday entry/retry review — September 21, 2026

Monday did not prove the weekend retry/repricing enhancements end to end. This review uses oldmac's Monday events and startup configuration, not Tuesday's replacement plan. Each positive signal is counted once, using its last outcome rather than counting pending and terminal events separately.

| Mode | Positive signals | Modeled fills | Expired limits | Selection failures | Pending/open blocks | Exceptions |
|---|---:|---:|---:|---:|---:|---:|
| LIVE | 3 | 0 | 0 | 0 | 0 | 3 |
| Shadow | 24 | 5 | 5 | 5 | 9 | 0 |

The three LIVE IWM signals (08:44, 09:07, 09:08 CT) encountered `fromisoformat: argument must be str` before an order plan. The Rail B historical decoder was fixed and deployed Monday around 09:40 CT (`42f0f8c`); local/oldmac source parity was verified again in this review. No later Monday LIVE positive signal exercised that repair. These were software-blocked opportunities, not negative strategy outcomes or proof that an order would have filled.

Shadow fills were GOOGL, MU's second attempt, AAPL, AVGO and SPY. QQQ, SNOW, MU's first attempt, RBLX and WFC rested paper limits that expired. Nine further MU/WFC signals correctly encountered pending/open-position guards; they were not nine missing independent trades.

## Selection failures and retry evidence

- HLT: 63 contracts inside the expiry window failed minimum open interest; zero spread-only retry candidates.
- WFC, twice: requested 0–3 DTE, but available expiries were 4 and 11 DTE. Nearest-expiry fallback also found no contract passing all gates; two would have passed if spreads improved. The terse rejection described only the original-window DTE failure. At 09:10 CT a later signal selected a 4-DTE contract and submitted a paper limit, which expired.
- RBLX, twice: failures were a mix of open interest, delta and spread requirements; five candidates would have passed if spreads improved.

Bhiksha recorded no entry repricing or liquidity-retry events. Its 28 scanner lanes had liquidity-retry duration zero and continued evaluating fresh signals normally. Four Cartographer lanes had a 600-second retry window. HLT, their only selection failure, did not qualify for a spread-only retry. This is configuration/gate behavior, not evidence of a broken retry scheduler.

PAT V3 was checked separately: no Monday entry-trigger, repricing, WAITING_ENTRY, ENTRY_EXHAUSTED or ENTRY_STOPPED log messages; its durable `signal_entry_retries` table had zero rows at review time. Its entry retry/repricing behavior remains unproved by a natural Monday episode. Exit-related retry messages are not entry evidence. Its newer deployed exit changes were preserved.

## Repairs selected

1. **Shadow repricing was missing.** Pending shadow entries used the expiration deadline but never applied the configured ladder. They now share live pricing-parameter construction, checkpoint settings and the original-price chase limit. Quantity stays fixed and the effective premium cap remains enforced. A fresh quote observed after the revised limit takes effect is required to model a fill; the repricing quote cannot fill the new limit. Fixed concessions stay fixed. Events explicitly use `paper_entry_repriced` and `mode=shadow`.
2. **An unmatched real SPY position was adopted by a shadow lane.** A broker SPY put was assigned by root symbol to the SPY long shadow lane, creating a synthetic recovered record and repeated degraded-protection issues. Shadow lanes now reject unmatched broker adoption and ownership based only on paper/synthetic records. Genuine broker entries retain ownership if their lane is later demoted to shadow. The historical record is preserved and excluded from the signal funnel above.

No eligibility thresholds, sizing, authorization modes or Sheet settings were loosened. Historical paper results were not rewritten. We cannot infer which expired Monday limits would have filled under the repaired ladder without a complete contemporaneous quote stream. Shadow ask-touch fills do not prove broker queue priority, cash availability or preflight acceptance.

## Validation and release

Eight new regression cases cover later-quote fills, stale/reused quotes, fixed quantity, original-price chase limits, premium caps, disabled/fixed-concession repricing, shadow adoption rejection and genuine live ownership after demotion. The existing network-retry fixture now explicitly uses a live lane.

Focused local tests: **38 passed**. Full isolated oldmac suite: **1,309 passed**. The development Mac intermittently ran out of disk space; its full run had SQLite disk-I/O failures plus the obsolete shadow-adoption fixture. Release validation therefore used production Python with the isolated source explicitly first on PYTHONPATH and its import path verified.

The read-only event extract is `artifacts/audits/2026-09-21-entry-retries/monday-evidence.json`. Release receipts record preimage checks, source hashes and test evidence. Tests and deployment are not a natural retry/fill episode; that remains to be observed.
