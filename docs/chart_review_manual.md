# Bhiksha Trade Review Manual

Bhiksha Trade Review is a local browser viewer for reviewing executed option trades on the underlying stock or ETF chart.

The live trade source remains `oldmac`. The review viewer runs from this dev repo.

## What The Viewer Shows

- one-minute underlying candles from Polygon
- executed Bhiksha trades from `oldmac:/Users/sunny/Documents/bhiksha/bhiksha.db`
- entry and exit markers on the underlying chart
- trade cards with short IDs such as `e405`, `ad87`, `9698`
- Central Time labels on the axis, cards, and details
- optional collapsed non-trading gaps
- filters for symbol, strategy, and outcome

The viewer does not chart option premium. It is for answering:

> Where was the underlying when I entered, and where was it when I exited?

## Weekly Or On-Demand Review

From the dev machine:

```bash
cd /Users/suman/code/bhiksha
scripts/build_chart_review.sh
```

If the local server is not already running:

```bash
.venv/bin/python -m http.server 8765 --directory artifacts/chart_review
```

Open:

```text
http://127.0.0.1:8765
```

One command to regenerate and serve:

```bash
BHIKSHA_CHART_REVIEW_SERVE=1 scripts/build_chart_review.sh
```

## Common Variants

Review the last 14 calendar days:

```bash
BHIKSHA_CHART_REVIEW_DAYS=14 scripts/build_chart_review.sh
```

Review all persisted trades:

```bash
BHIKSHA_CHART_REVIEW_ALL=1 scripts/build_chart_review.sh
```

Include premarket and after-hours candles:

```bash
BHIKSHA_CHART_REVIEW_EXTENDED_HOURS=1 scripts/build_chart_review.sh
```

Use a different port:

```bash
BHIKSHA_CHART_REVIEW_PORT=8777 BHIKSHA_CHART_REVIEW_SERVE=1 scripts/build_chart_review.sh
```

## Review Workflow

1. Pick a symbol.
2. Leave `Collapse gaps` on for normal review.
3. Click a trade card on the right.
4. Use mouse or trackpad zoom to inspect the area around entry and exit.
5. Match the chart markers to the card using the short ID.

Marker format:

- `B PUT e405`: bought a put at entry
- `S PUT e405`: sold that put at exit
- `B CALL ...` / `S CALL ...`: same convention for calls

Trade card format:

- short ID
- option side
- Central entry and exit time
- underlying direction
- full option contract
- strategy family
- option P/L

## Server Setup

The recommended setup is:

- `oldmac` remains the live trading server and SQLite authority.
- this dev machine pulls a read-only DB snapshot over SSH and hosts the viewer locally.

Prerequisites on the dev machine:

- SSH alias `oldmac` works
- `.env` has `POLYGON_API_KEY`
- `.venv` is installed

Check:

```bash
ssh oldmac 'test -f /Users/sunny/Documents/bhiksha/bhiksha.db && echo ok'
grep '^POLYGON_API_KEY=' .env
```

No process needs to be added to `oldmac` for normal review. That keeps the trading server simple.

## If You Want A Persistent Server-Hosted Viewer

You can run this on the dev machine with launchd or cron, but keep it read-only:

```bash
cd /Users/suman/code/bhiksha
BHIKSHA_CHART_REVIEW_SERVE=1 scripts/build_chart_review.sh
```

For a scheduled weekly refresh, use a cron entry on the dev machine:

```cron
0 17 * * FRI cd /Users/suman/code/bhiksha && /bin/bash scripts/build_chart_review.sh >> artifacts/chart_review/refresh.log 2>&1
```

This refreshes the generated files. The `http.server` process can stay running separately.

## Data Integrity Rules

- Trade data comes from a copied SQLite snapshot from `oldmac`.
- Candle data comes from Polygon one-minute aggregate bars.
- Missing candles are shown as warnings.
- The app does not synthesize prices.
- With `Collapse gaps` enabled, only visual spacing changes; timestamps in the details remain the original Central times.

## Suggested Weekly Questions

- Did entries happen at reasonable underlying locations?
- Did losses cluster around the same setup or time of day?
- Were exits too early, too late, or aligned with the thesis?
- Are winners concentrated in one strategy family?
- Are there symbols where trade timing repeatedly looks poor?
