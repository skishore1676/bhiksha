# Bhiksha Deploy Runbook

The canonical operator checklist now lives in:

- [bionic_loop_checklist.md](/Users/suman/kg_env/projects/mala_v1/docs/bionic_loop_checklist.md)

Use that document for the full end-to-end Bionic loop.

This file is now only a short redirect so there is one checklist instead of two.

Bhiksha-specific reminders:

- pre-open wrapper:
  - `PYTHONPATH=src .venv/bin/python -m bhiksha.tools.bionic_session prepare`
- live run:
  - `PYTHONPATH=src .venv/bin/python -m bhiksha.tools.bionic_session run --live`
- post-close review:
  - `PYTHONPATH=src .venv/bin/python -m bhiksha.tools.bionic_session review`
- in `--session-payload` mode, Bhiksha ignores `config/deployments/` and treats `active_session.json` as the sole live authority
