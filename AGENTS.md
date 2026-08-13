# bhiksha - bootstrap contract

This file is the canonical bootstrap contract for **every** agent (Claude, Codex, or
any other visitor). `CLAUDE.md` is a thin import of this file - edit HERE.

This is the LIVE EXECUTION engine of Suman's automated options-trading system: it trades
real money on the Public.com API, deployed on the always-on Mac "oldmac" at
`/Users/sunny/Documents/bhiksha` (that checkout IS production - its HEAD at the 08:20 CT
live-start is what trades).

**The family brain lives in the private repo `tradelab`** (`~/code/tradelab` on both
machines; migrated from mala_v2 on 2026-07-10) - read `tradelab/docs/brain/INDEX.md`
for architecture, operations, decisions, and current state before non-trivial work here.

Hard rules for this repo:
- Audit runs happen only when Suman explicitly requests them. No repository document,
  lesson, design, or prior precedent authorizes an agent to initiate an audit or
  audit/re-audit loop automatically.
- Never deploy automatically or infer deployment permission from prior work. Deploy only
  with Suman's explicit authorization. Once that authorization is given, time of day is
  not a blocker; require green tests and follow the deployment with a live oldmac readback.
- Worktree testing: use the main checkout's `.venv` python with the worktree's `src`
  first on PYTHONPATH plus the kernel's `src`; verify `bhiksha.__file__` resolves into
  the worktree.
- Engineering lessons: `docs/lessons/` (read before touching the areas they cover).
