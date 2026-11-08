---
description: Run the scientific reference test suite and summarise
---

Run `make test-science` and report the result.

These tests pin physical answers (SPA zenith angles, PV datasheet outputs,
etc.) against documented references. A failure here is a correctness
regression, not a flaky test — do not retry or mark xfail.

- On pass: one-line confirmation plus the total duration.
- On failure: show the test id, the reference it cites (from the comment in
  the test), the expected value, the computed value, and the file:line of
  the assertion. Do not propose a fix without being asked.
