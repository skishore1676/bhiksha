---
title: Preview runs must not replace passing receipts
type: gotcha
area: weekly trading decisions
date: 2026-07-28
tags: [reports, receipts, preview, learning-loop]
refs: [src/bhiksha/tools/launchd_job.py, tests/test_launchd_control_status.py, 956a516]
---

# Preview Runs Must Not Replace Passing Receipts

## What We Learned

A safe preview can still damage the learning loop if it writes to the same stable filename as a passing report. Preview evidence must live in a separate directory so an intentionally skipped workbook update cannot erase the receipt required by downstream weekly learning.

## Context and Evidence

The weekly decision runner formerly wrote both normal and `--workbook-update-mode off` runs to `artifacts/playbook/reports/weekly_trading_decisions_<week>.json`. A preview therefore replaced `workbook_update.status: ok` with `skipped`, and TradeLab correctly failed closed with `passing weekly trading receipt is unavailable`.

Commit `956a516` routes preview output to `artifacts/playbook/reports/previews/`. A live broker-inert preview on oldmac left the canonical report SHA-256 unchanged while producing a separate preview artifact.

## When It Applies

Use this rule for any report whose stable path is also a downstream readiness gate. Preview, dry-run, failed, and partial attempts should remain inspectable, but they must not replace the last successful canonical receipt.

## Apply It Next Time

Before adding a preview flag, identify the canonical success path and every downstream consumer. Test that a preview writes elsewhere and that the canonical hash remains unchanged.

## Dead Ends

Preserving only the launchd `latest_status.json` entry is insufficient: it describes the last attempt, while the learner needs the last passing evidence artifact.

## Retired publisher dependencies (2026-09-04)

TradeLab commit `6d2f4c4` retired its duplicate workbook writer, but Bhiksha still
invoked `scripts/review/update_trading_decision_ledger.sh` and both readers required
`workbook_update.status: ok`. Friday's job consequently left its weekly receipt
pending; TradeLab's 16:15 run correctly refused it before invoking the analyst.

The repaired default emits an explicit `retired` workbook marker bound to the
passing app-owned facts receipt. Weekly packet readiness and TradeLab validation
accept only that exact marker and binding; pending, failed, mismatched and preview
receipts remain non-production evidence. Explicit custom workbook commands remain
supported. No retired workbook service is restored.

When removing a publisher, search both producers and consumers for its executable
path and readiness field. Verify the next app-owned packet and downstream published
report, not merely deletion of the writer. Regression tests cover the retired
binding, rejection of bad facts, and no invocation of the removed writer.

A successful manual recovery does not reset launchd's last exit code. Health now
binds verified manual recovery to the exact idle launchd run count and exit code,
so the old failure clears while a newer scheduler failure still pages. Never suppress
a nonzero launchd exit solely because an arbitrary success record exists.
