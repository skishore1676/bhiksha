# Entry windows and exit authority must be coherent

**Date:** 2026-09-19  
**Type:** Runtime contract / experiment integrity

## Failure mode

A strategy can keep admitting entries after a fixed native time exit is already
true. Each new position then closes on the next supervisor loop, and the lane
may re-enter again because the previous position is terminal.

Separately, a compiled plan can describe a shadow exit profile as the selected
winner while native thesis-exit management still runs first. The observed
policy is then "first exit to act," not the selected profile, so the result is
not attributable to one policy.

## Required checks

- Compare the derived signal window, operator execution window, every fixed
  native exit time, and the profile-exit gate during compilation.
- Reject or prominently warn when new entries remain possible at or after an
  authoritative fixed time exit.
- Represent exit authority explicitly (`native`, `profile`, or intentionally
  paired/comparator-only); do not infer it from descriptive metadata.
- Report configured selection and the exit path that actually acted as
  separate fields.
- Test a signal arriving after the configured fixed exit and verify that it
  cannot create an immediately terminal/re-entering position.

## Safe containment

Until authority is explicit, end the operator-owned entry window strictly
before the fixed exit. Do not hide the defect by deleting exit evidence or by
claiming profile results when native management pre-empted the profile.

## Evidence

The September 17, 2026 META shadow lane admitted nine signals through 10:58 ET
while its native time stop was 10:15 ET. The associated analysis is in
`artifacts/playbook/reports/cartographer_shadow_execution_deep_dive_2026-09-17_2026-09-18.md`.
