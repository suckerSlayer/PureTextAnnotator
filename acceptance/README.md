# Formal acceptance package

This directory contains the reproducible data and measurements used by the
thesis acceptance report.

## Reproduce the complete run

From the repository root, with the development environment installed:

```console
.venv/bin/python acceptance/run_acceptance.py
```

The command deterministically regenerates both machine-readable artifacts:

- `data/rematch_cases.json`: 120 independently constructed rematch cases with
  ground-truth ranges.
- `results/acceptance_results.json`: environment details, case-by-case results,
  the 1,000,000-byte/1000-annotation fixture manifest, timings, and regression
  output.

The large performance source and sidecar are created in a temporary directory
for every run. Their hashes and construction parameters are retained in the
result file, so a 1 MB generated fixture does not need to be committed.

## Workloads and timing boundaries

The rematch corpus uses seed `20261114` and covers unchanged text, prefix
insertions, deletion before the target, local edits, duplicates with usable
context, truly ambiguous duplicates, deleted quotations, and file migration.
It compares offset-only, quotation-only, and PTA's combined anchor strategy.

The performance fixture is exactly 1,000,000 UTF-8 bytes, has 10,000 fixed-size
lines, and contains 1000 annotations distributed across note, highlight, and
bookmark records. The complete run performs:

- 10 cold application construction and mount measurements;
- 200 response measurements: 70 line scrolls, 60 selection operations, and 70
  annotation switches;
- the same 200 operations using Textual's stricter full-animation-settlement
  boundary as a supplemental diagnostic;
- 20 no-op keys to expose the test-driver baseline;
- 3 full scans that shift all 1000 annotation anchors;
- the complete pytest regression suite.

For the thesis's 100 ms response requirement, timing starts immediately before
the real Textual key event is sent and ends after all screen messages queued by
that operation have been processed. Cosmetic animation completion is excluded.
The supplemental `Pilot.press` result includes all animations and a whole-screen
barrier; it is reported separately and is not substituted for the defined
first-response metric.

Use `--skip-ui` or `--skip-regression` only for development checks. Such a run
is explicitly marked as incomplete and cannot produce an overall acceptance
result.
