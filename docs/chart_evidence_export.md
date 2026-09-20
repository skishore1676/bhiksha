# Bhiksha chart-evidence export

`python -m bhiksha.tools.chart_evidence_export` exports one Chicago trading-day
snapshot from Bhiksha SQLite into a Chart Workbench evidence-protocol v2 packet.
It defaults to `bhiksha.db` and writes immutable JSON to
`artifacts/chart-workbench/publications/<packet.id>.json`.

The exporter opens SQLite with `mode=ro`, enables `query_only`, and holds one
read transaction. It imports an allowlist of deployment scope, event timing,
signal/order/fill state, underlying coordinates, and option metadata. It does
not construct the runtime, load credentials, contact a provider, or serialize
broker payloads. A `trade_sessions` row preserves the actual Bhiksha `tradeId`
in a source extension. Pending-entry rows remain management/identity evidence;
the terminal live `open_*` state is a recorded fill because Bhiksha writes it
only after its broker-confirmed entry flow. An explicit positive
`entry_fill_check` is also a recorded fill, while `shadow_entry_assumed` and
shadow trade rows remain modeled. Option premiums are never mapped onto the
underlying chart.

The packet has the ordinary v2 `schemaVersion`, source, run, provenance,
instruments, cases, and records fields. Its top-level `publication` envelope
uses a stable `streamId` of `bhiksha-<date>`, a caller-selected positive
revision, deterministic source-derived `publishedAt`, and coverage entries
linked to `caseId`. Each case also declares its Bhiksha deployment, mode, and
strategy in `extensions.bhiksha:scope`; execution extensions retain option
symbol and call/put metadata without treating its premium as a chart price.

Coverage declares the source's supported signal, order, fill, management, exit,
and rejection domains even when that day has no records in one of them. It is
`partial` by default. The exporter marks a case `complete` only if
that deployment has an explicit `session_coverage` or `session_complete` event
with `complete: true` (or `session_complete: true`) and the bounded event read
did not truncate. Session clock time, a stopped runtime, and a report receipt
do not certify coverage.

The same source snapshot and revision canonicalize to the same content-addressed
packet ID and reuse the existing file. A correction uses a higher revision on
the same stream and writes a new immutable file. If that source stream and
revision already name different retained content, the exporter rejects it with
an explicit `--revision` instruction rather than creating a conflicting packet.
Writing the outbox does not
import, upload, or otherwise publish a packet into Chart Workbench.

The existing `live-stop` launchd job invokes the exporter only after its stop
stage reports success. It reads only `config/app.yaml` to locate SQLite; it does
not construct a new trading runtime. The result is retained in the `live-stop`
status receipt; an export error makes that job visibly fail after the runtime is
already stopped, with `runtime_stop_status: ok` kept distinct from the exporter
failure reason.
