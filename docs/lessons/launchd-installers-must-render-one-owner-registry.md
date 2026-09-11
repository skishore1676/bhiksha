---
title: Launchd installers and status must render one owner registry
type: pattern
area: launchd ownership
date: 2026-09-11
tags: [launchd, registry, installer, status]
refs: [src/bhiksha/ops/launchd_registry.py, scripts/launchd/install_bhiksha_launchd.sh, scripts/launchd/install_cartographer_shadow_launchd.sh, src/bhiksha/tools/launchd_status.py, 636685f]
---

# Launchd Installers Must Render One Owner Registry

## What We Learned

An optional job may need its own installation gate and environment, but it must
not acquire a second schedule or command definition. The registry should render
the installed plist and the status command; installer scripts should only add
configuration and perform the bootstrap.

## Context and Evidence

Cartographer appeared in `launchd_registry.py` as the generic
`run_bhiksha_job.sh cartographer-shadow` command while its dedicated installer
rendered a separate XML template that directly called
`run_cartographer_shadow.sh` with four path arguments. The schedules happened to
match and the deployed job was healthy, but an operator could validate or repair
one command while launchd executed the other.

Commit `636685f` makes both the standard and optional installers render
`LaunchdJobSpec.plist_payload`. The optional installer still owns its explicit
Sheet and source-path gate, but it installs the shared runner and no longer owns
a template. Status uses the same spec's `status_command`. The full suite passed
1,225 tests, and oldmac readback showed exact registry parity for program
arguments, schedule, logs, process type, and I/O priority.

## When It Applies

Use this pattern for every Bhiksha launchd job, including opt-in, experimental,
auth-maintenance, reporting, and trading-runtime jobs. A distinct installer is
acceptable when enablement or environment is distinct; a distinct executable
contract is not.

## Apply It Next Time

Add the job once in `src/bhiksha/ops/launchd_registry.py`. Render its plist with
`plist_payload`, project its operator command with `status_command`, and test the
rendered plist against the registry. If an installer needs credentials or
source roots, inject only those environment values and keep them out of status
output and tests.

## Dead Ends

- A hand-maintained XML template makes schedule parity a coincidence.
- Advertising a generic wrapper while installing a dedicated wrapper leaves two
  plausible repair commands.
- Folding an opt-in job into the standard installer broadens deployment scope;
  keep installer scopes disjoint while sharing the registry.
