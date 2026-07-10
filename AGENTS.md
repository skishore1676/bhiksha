# bhiksha — bootstrap contract

**Identity:** bhiksha is the **live options execution runtime** of Suman's trading
family — it places real orders on the Public.com API. It retains full ownership of:
live options execution, active-plan compilation, Sheet semantics, orders, runtime
validation, receipts, and money gates. Nothing outside this repo may exercise them.

## Where runtime truth lives

- Production runs on **oldmac** from `/Users/sunny/Documents/bhiksha` (NOT `~/code`
  — a common trap). That checkout's HEAD at the 08:20 CT live-start is what trades.
- App-owned status: SQLite `bhiksha.db` in the runtime checkout; launchd job logs +
  `latest_status.json` under `artifacts/playbook/launchd/`.
- **Safe read-only inspection:** `git log` on the runtime checkout;
  `sqlite3 -cmd ".timeout 8000" <db>` on-host (or open a copied snapshot with
  `immutable=1`); read the launchd logs/status files. Never write, never run
  trading-runtime jobs by hand.

## Money / deploy gates

- Any change that can place, size, or suppress a real order requires **green tests +
  multi-round adversarial audit + a session boundary** (market closed, hard-flat).
  A green test suite alone is NOT proof.
- Deploys are operator-gated: push from dev, pull on oldmac, readback (`oldmac ==
  origin == local`), confirm launchd jobs loaded. One build increment per day.

## The family brain

Durable family knowledge (which app owns what, current state, ADRs, queue) lives in
the **private repo `tradelab`** — clone location `~/code/tradelab` on both machines.
Entry point: `docs/brain/INDEX.md`. Trust order: **runtime evidence > diary > brain
summary** — when sources disagree, the runtime wins. Re-verify anything stale.

## Forbidden by default (reading the brain grants NO authority)

- No placing, modifying, or cancelling orders.
- No writes to the operator's arming Sheet (any tab).
- No deploys, restarts, or launchd changes to this or any trading app.
- No auth/token/credential changes; never read or copy secrets.
- No external sends (Telegram, email, public pushes) without an explicit operator gate.
- Default stance: read-and-recommend. Execution authority is granted per lane by the
  operator, never inherited from documentation.
