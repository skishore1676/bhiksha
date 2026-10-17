# Private after-close recovery backup

The existing `live-stop` owner runs this opt-in downstream stage after stopping
the runtime and exporting chart evidence. No new clock, daemon, heartbeat,
trading policy, broker action, or live database purge is introduced. Enablement
is the non-secret runtime marker
`artifacts/playbook/runtime_flags/after_close_backup.enabled`; settings live in
`config/after_close_backup.json`. Authorized local retention is enabled there,
not by the source release: the public example uses placeholders and disables
cleanup. Copy `config/after_close_backup.example.json` only when separately
configuring an authorized runtime; the release does not include owner settings.
Authorized local retention is enabled in the private settings,
not through an ad-hoc CLI switch. The existing weekday 15:10 America/Chicago
live-stop schedule owns stop → chart export → backup → verified local cleanup.

The guard requires a completed XNYS session (holiday/early-close aware), no
current regular session, and a positively stopped runtime. A new capture uses
the deployed `monitoring_snapshot` tool, never raw live-file copies. Capture
children and heavy validation/compression/restore workers apply background disk
priority only after process startup. The small authenticated Drive client does
not inherit low-priority disk startup. CPU work is niced. Per-database source
handles close before validation, hashing, compression and upload. Two databases
are independently consistent, **not an atomic cross-database transaction**.
Heavy/network child deadlines are capped before the next XNYS opening (with a
one-minute margin), so a manual premarket recovery cannot inherit an hours-long
budget through the next trading session. Each major phase rechecks the calendar.

The bundle includes both databases, the completed snapshot manifest, its three
allowlisted non-secret sidecars when present (including the active plan), and a
bundle manifest with file sizes, SHA-256, schema hashes and per-table maximum
rowids. It records the packaging Git commit and includes the allowlisted backup
tools, settings and this runbook under `recovery_source/`. It does not include credentials,
environment files, browser profiles or Keychain exports. Production and the Air
retain the versioned commit; no public Git push is part of backup setup.
Reauthorize credentials separately.

For completed source snapshots, checksum agreement binds the captured integrity
receipt to the exact bytes being packaged; the redundant full source integrity
scan is skipped. The downloaded restore always receives a fresh full SQLite
`integrity_check`, as well as hashes/schema/watermark comparisons.

Drive destination is an owner-only folder in the configured account's My Drive.
The stable installed gog binary uses its existing `companion-write` OAuth client
with `drive.file` and the macOS Keychain; no token is exported. Folder and file
permissions must be owner-only. File naming binds session and content hash.
The owner checks remote ID, parent, name, size and privacy, downloads the actual
remote file, verifies SHA-256, extracts only exact regular-file allowlisted
members into a fresh isolated directory, and compares all hashes, full SQLite
integrity, schemas and rowid watermarks. It never restores over production.
gog 0.11 metadata omits server checksum fields, so checksum evidence explicitly
means a downloaded remote round trip, not a claimed server MD5.

## Recovery and evidence

The stable lock prevents concurrent capture/upload. Each session retains
`../bhiksha-backups/YYYY-MM-DD/receipt.json`, `bundle.json`, its archive and any
new capture. Retries reuse completed evidence; a remote exact-name search
reconciles an upload whose response was lost, avoiding blind duplicate retries.
There is no unbounded retry loop. Each scheduled attempt is bounded; after a
failure use the existing Control Tower `backup-recovery-now` action (no runtime
stop/start), or the owner CLI for an explicit historical session:

```sh
python -m bhiksha.tools.after_close_backup --session YYYY-MM-DD
```

To seed a first backup from completed after-close evidence, add
`--snapshot /absolute/completed-monitoring-snapshot`. Existing incomplete
captures fail visibly and are preserved; choose a new session attempt only
after reviewing the failure. No fallback reads live SQL for analysis.

An incomplete managed capture is never reused merely because its directory
exists: recovery retains its path in `failed_snapshots` and selects a distinct
attempt directory. Explicit complete after-close evidence can replace a failed
unbundled capture; completed archives/bundles cannot change their snapshot.
The new capture receives a cooperative total deadline five seconds inside the
existing parent bound (also capped before market open), including validation
and hashing. A hard child timeout preserves bytes/progress and marks the
capture failed; the owner records `failed_phase`. No failed or partial snapshot
becomes valid evidence through this repair. Backup failure status is separate
from entry reconciliation and cannot erase successful stop proof. This makes
recovery usable; it does not establish the cause of slow validation or authorize
a historical backup/upload replay.

The existing launchd latest-status payload under `live-stop` carries progress,
last verified recovery, remote link, cutoff, checksum/restore proof, retention
preview, failure and recovery instruction. Control Tower reuses that job and a
stable attention key, not a second job or notification route. Routine success
is quiet; failed verification requires attention and a later verified recovery
clears it. Merely fixing authentication does not clear a failed backup.
Verified backup recovery also never clears an unrelated failed stop/export;
backup-only retries preserve that upstream failure until the close stage succeeds.

The stop owner publishes a timestamped `session_stop_receipt` immediately after
successful stop and before chart export. Export/backup progress or failure keeps
that proof independent from aggregate job status. The session report uses it
without relaxing its existing same-session, after-close, successful-stop checks.
Missing/failed/stale proof remains actionable; an already-idle runtime is not
invented evidence of a successful stop. Legacy records still require aggregate
success. A process already running at deployment cannot gain this new receipt
retroactively; historical reports and timestamps are not rewritten.

## Authorized local retention

Keep the newest two verified session archives and managed raw captures. Older
verified archives and captures can be removed only after fresh Drive identity,
parent, size and owner-only permission checks against the saved downloaded
checksum/full-restore proof. Drive history is never deleted. Successful isolated
restore-test working directories are disposable after that same gate, including
on an idempotent retry. Recovery remains through the private Drive archive.

Only exact paths under `../bhiksha-backups` qualify; external monitoring snapshots,
live databases, unknown files, incomplete/failed attempts and all receipts stay.
Successful selected `snapshot-attempt-<32 hex>` captures qualify under the same
two-verified-session policy only in their exact session directory, absent from
`failed_snapshots`, with exact manifest filenames/size/checksum agreement and
all existing remote/restore/privacy/link/open-file gates. A failed attempt is
never swept because its name resembles a successful one. This is a lifecycle
correction within the existing retention authority, not a cleanup invocation.
Links, special files and open targets block deletion. A per-session cleanup
journal records the path, bytes and remote recovery ID before and after removal.
Cleanup failure raises Control Tower attention without invalidating the verified
backup or repeating the restore. Retry the existing recovery action after resolving
the blocker. No new clock or automatic retry loop is added. Reserve checks remain
in force. Natural scheduled acceptance is distinct from manual verification.
