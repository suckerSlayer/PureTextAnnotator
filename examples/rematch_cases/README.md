# Rematch experience suite

Run the marked sample from the repository root:

```bash
pta-view examples/rematch_cases/cases.md
```

The sample contains four deliberately stale annotations:

1. an unchanged quotation shifted by an insertion;
2. a lightly edited quotation requiring fuzzy matching;
3. duplicate text resolved with saved surrounding context;
4. a deleted passage that should remain unresolved.

The title bar labels this file as a demo. On the first press of `R`, a warning
asks whether to reset both sample files to their checked-in stale baseline.
Cancel leaves the files untouched. Choose **Reset demo**, then press `R` again
to open the read-only rematch review.

In review, use `J`/`K` to move, Space to accept or skip, and `C` / `Shift+C` to
inspect the next/previous candidate. The compact bottom panel leaves the source
and annotation panes visible: the source candidate is marked in green and the
corresponding stored quotation and note are focused beside it. Only estimates
of 90% or higher are preselected. Press Ctrl+S to continue to the final
confirmation; Cancel still makes no changes. Applying writes only
`cases.md.annotations.md` and creates a timestamped backup.

To try manual recovery, cancel review, make a Visual selection in the source,
press `R`, navigate to an annotation, and press `M` to use that source span.
The manually selected candidate still receives an approximate correctness
estimate and still passes through the same Apply confirmation.

After an applied demo rematch, pressing `R` offers the reset again so the suite
can be repeated.
