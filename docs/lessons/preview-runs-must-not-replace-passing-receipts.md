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
