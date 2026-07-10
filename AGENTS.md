# bhiksha — bootstrap contract

This file is the canonical bootstrap contract for **every** agent (Claude, Codex, or
any other visitor). `CLAUDE.md` is a thin import of this file — edit HERE.

This is the LIVE EXECUTION engine of Suman's automated options-trading system: it trades
real money on the Public.com API, deployed on the always-on Mac "oldmac" at
`/Users/sunny/Documents/bhiksha` (that checkout IS production — its HEAD at the 08:20 CT
live-start is what trades).

**The system's brain lives in the sibling repo `mala_v2`** — read
`mala_v2/AGENTS.md` and `mala_v2/docs/brain/INDEX.md` for architecture, operations,
decisions, and current state before non-trivial work here.

Hard rules for this repo:
- Money-path changes (order path, exits, risk, reconciliation, compiler gating) require
  adversarial audit rounds before merge — a green suite is not proof.
- Deploys only at session boundaries (after ~15:00 CT hard-flat), gated on green tests +
  passed audit, followed by a live readback on oldmac.
- Worktree testing: use the main checkout's `.venv` python with the worktree's `src`
  first on PYTHONPATH plus the kernel's `src`; verify `bhiksha.__file__` resolves into
  the worktree.
- Engineering lessons: `docs/lessons/` (read before touching the areas they cover).
