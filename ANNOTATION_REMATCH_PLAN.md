# Annotation Rematching Strategy — Audit and Implementation Record

Status: **approved and implemented as the deterministic v0.3 first release**
Created: 2026-11-01
Scope: reconnect stale annotation ranges to modified source text

This strategy was not present in the current implementation plan. The current
application originally recorded source offsets and quoted text without
source-change recovery. This document became the audited design for the v0.3
implementation. Its safety contract, deterministic matching ladder, review UI,
confidence-bearing Apply confirmation, backup, validation, and rollback are now
implemented. Statistical calibration remains future evaluation work; displayed
estimates are still explicitly labeled heuristic approximations.

## 1. Problem

An annotation currently connects to the source through:

- a stable annotation ID;
- a zero-based, half-open Unicode code-point range `[start, end)`;
- the quoted original source text;
- the note and its creation time.

The range becomes stale when text is inserted, deleted, moved, or edited before
or inside it. The quotation may also differ because the source text or the
annotation's `### Original text` block was edited. A rematcher must find the most
likely new source span without silently attaching a note to the wrong passage.

Typical cases include:

1. Text was inserted before an annotation, so the quote is unchanged but its
   offsets shifted.
2. The annotated sentence was edited slightly.
3. A paragraph was moved elsewhere in the document.
4. The same quotation occurs more than once, making an exact match ambiguous.
5. The annotated passage was deleted and has no responsible automatic match.
6. Annotation metadata or the quoted text was manually edited.

## 2. Non-negotiable safety contract

The rematching workflow must obey these rules:

1. Mismatch detection, candidate search, confidence calculation, and preview are
   read-only. They do **not** require confirmation and must not change either
   file.
2. Applying a rematch is the only rematching operation that requires a
   confirmation prompt.
3. A rematch is never applied automatically, even when its estimated confidence
   is 100%.
4. The confirmation prompt must show the approximate chance that the proposed
   rematch is correct, the evidence behind it, and the number of ambiguous or
   unresolved annotations.
5. Canceling the prompt must leave both files byte-for-byte unchanged.
6. Applying a rematch may update only the annotation sidecar. It must never edit
   the source text.
7. Application must use an atomic write, create a recoverable backup, and verify
   the written sidecar before reporting success.
8. Rematching is unavailable while either pane has unrelated unsaved edits. The
   user must first Save or Discard those edits so the scan operates on stable
   file revisions.
9. If the source or sidecar changes after scanning, all proposals are invalidated
   and must be recalculated before confirmation.

Within this workflow, there is exactly one rematch confirmation: **Apply
rematch**. The application's existing source-edit warning and quit-time
Save/Discard prompt remain separate safety mechanisms.

## 3. Backward-compatible anchor data

### 3.1 Data already available

Version 1 sidecars already contain enough information for a useful first pass:

- stored range;
- stored quotation;
- stable ID;
- annotation order;
- note content.

The parser currently exposes the range and the sidecar line numbers, but not a
complete structured annotation record. The first implementation step would add
a read-only parser that reconstructs the ID, range, quoted text, note, and
metadata without rewriting the file.

### 3.2 Optional anchors for newly created or rematched annotations

Future records should add optional metadata while remaining valid Markdown and
compatible with version 1 readers:

```yaml
anchor:
  source_sha256: "..."
  quote_sha256: "..."
  prefix: "up to 80 code points before the quote"
  suffix: "up to 80 code points after the quote"
  original_start: 1234
  original_end: 1288
```

Recommended details:

- `prefix` and `suffix` use exact source text, not normalized text.
- Context is capped at 80 Unicode code points on each side.
- Hashes detect exact identity; they are not used as fuzzy similarity scores.
- Missing anchor fields are allowed, so all existing sidecars remain usable.
- Notes, stable IDs, and creation timestamps are never used as text-match
  evidence and are never rewritten by matching.
- For PDF sources, a confirmed rematch also replaces `pdf_anchor` with geometry
  computed from the current PDF. If geometry is unavailable, stale PDF anchor
  data is removed so embedding fails closed.

## 4. Read-only mismatch detection

For each annotation:

1. Validate that `0 <= start < end <= len(source)`.
2. Decode the stored `### Original text` quotation.
3. Compare `source[start:end]` with the stored quotation.
4. Check the optional source and quote hashes when present.
5. Assign one status:

| Status | Meaning |
| --- | --- |
| Healthy | Range is valid and source text exactly equals the stored quote. |
| Shifted | Quote exists exactly elsewhere but not at the stored range. |
| Modified | A strong approximate candidate exists. |
| Ambiguous | Multiple candidates have similar evidence. |
| Orphaned | The original passage appears deleted or no safe candidate exists. |
| Malformed | Required metadata or quotation cannot be parsed. |

Healthy annotations need no rematch and must not appear in the Apply set.
Detection may show a non-blocking mismatch count when a file opens, but it must
not open a confirmation prompt by itself.

## 5. Candidate generation pipeline

Candidate generation proceeds from strongest, cheapest evidence to weaker
evidence. A weaker stage runs only when stronger stages do not produce a unique,
credible result.

### Stage A — edit-delta projection

For edits made during the current application session, retain an edit map from
the last saved source revision to the current revision:

- edits entirely before a range shift both offsets deterministically;
- edits entirely after a range do not change it;
- edits overlapping a range mark it as touched and provide an expected search
  window rather than an automatic answer.

This projection is a hint and still requires confirmation before application.
External edits fall through to the later stages.

### Stage B — exact quotation search

Search for the exact stored quotation in this order:

1. at the projected range;
2. in a local window around the projected or old offset;
3. throughout the complete source.

A unique exact match is strong evidence. Repeated exact matches continue to
context scoring rather than choosing the first occurrence.

### Stage C — normalized exact search

Search a normalized representation while retaining a map back to original
Unicode offsets. Initial normalization should be conservative:

- normalize line endings;
- collapse runs of whitespace;
- apply Unicode NFC normalization.

Case, punctuation, Markdown markers, and words must not be removed in the first
version because aggressive normalization increases false matches.

### Stage D — context-anchor search

Compare prefix and suffix context around each candidate. Context can distinguish
repeated quotations and passages that moved. Both-side context agreement is much
stronger than one-side agreement.

For old records without stored context, derive provisional context only when the
old range still points to a partially recognizable neighborhood. Do not pretend
that derived context is as reliable as context saved at annotation creation.

### Stage E — fuzzy quotation search

Generate bounded candidate windows with lengths near the stored quotation and
score them using:

- Unicode character sequence similarity;
- token similarity;
- longest matching blocks;
- matching prefix and suffix context;
- distance from the projected offset;
- uniqueness relative to the second-best candidate;
- consistency with neighboring annotations.

The first version should use deterministic local algorithms. It should not use
network services, language models, or semantic embeddings. Semantic matching can
be evaluated later, but it is too difficult to explain and calibrate for the
first safety-sensitive release.

### Stage F — document-level consistency

Score candidates together instead of accepting each annotation independently:

- preserve relative order when the surrounding passages remain ordered;
- prefer a coherent shared offset shift for nearby annotations;
- prevent two unrelated annotations from being assigned to the same candidate;
- allow overlapping candidates when the original annotations genuinely
  overlapped;
- treat a moved section as a possible reorder rather than forcing old order.

This stage may break a tie, but it must not turn weak text evidence into a
high-confidence match.

## 6. Confidence estimate

The prompt must call the number an **estimated correctness**, not a guaranteed
probability. Before real acceptance/rejection data exists, it is a transparent
heuristic rounded to about the nearest 5%; false precision such as `93.72%` must
not be shown.

### 6.1 Initial scoring model

Each candidate receives component scores from 0 to 1:

- quotation similarity: 45%;
- prefix/suffix context agreement: 20%;
- uniqueness versus competing candidates: 15%;
- proximity to the delta-projected position: 10%;
- consistency with neighboring annotations: 10%.

Penalties apply when:

- the best and second-best candidates are close;
- only a very short quote is available;
- the match depends on aggressive normalization;
- source text appears deleted or substantially rewritten;
- annotation order must be violated without evidence of a moved section;
- metadata and quoted text contradict each other.

### 6.2 Display bands

| Estimated correctness | Treatment |
| --- | --- |
| 98–100% | Unique exact match with strong context; still requires confirmation. |
| 90–97% | Strong proposal; preselected for review. |
| 75–89% | Review required; not preselected for batch apply. |
| 60–74% | Ambiguous; show alternatives and require a manual choice. |
| Below 60% | Unresolved; do not offer an automatic candidate. |

An exact quotation with several identical occurrences cannot receive a high
score from exactness alone.

### 6.3 Batch estimate

For a batch, show:

- rounded average estimated correctness of selected matches;
- the lowest selected estimate;
- counts in High, Review, Ambiguous, and Unresolved bands;
- a per-annotation estimate in the review list.

Do not show a misleading single “all matches are correct” probability. Candidate
errors are correlated, so multiplying individual estimates would not be a
trustworthy batch probability.

After enough real user decisions exist, anonymized feature/outcome data can be
used to calibrate the displayed bands. No source or note content should be
collected for calibration.

## 7. Review interface

A read-only review screen should show, for each stale annotation:

- annotation number and stable ID;
- old range and proposed range;
- stored quotation and proposed current source text as a diff;
- surrounding source context;
- approximate correctness;
- concise evidence, such as “unique exact quote + both contexts”;
- competing candidates when ambiguity exists.

Available review choices:

- accept the proposed candidate;
- choose an alternative candidate;
- manually select a source span;
- leave the annotation unresolved;
- mark it orphaned without deleting the note.

High-confidence proposals may be preselected. Medium- and low-confidence items
must not be preselected.

## 8. Required confirmation prompt

Scanning and reviewing do not prompt. Pressing **Apply rematch** opens the only
rematch confirmation prompt. Example:

```text
Apply annotation rematch?

12 annotations will be updated.
Estimated correctness: ~95% per selected annotation
Lowest selected estimate: ~80%
High confidence: 9   Review accepted: 3
Ambiguous skipped: 2   Unresolved: 1

This changes annotation ranges and quoted original text.
The source file will not be changed.
A backup will be created before the atomic write.

[Review again]  [Cancel]  [Apply rematch]
```

For one annotation, show that candidate's approximate correctness directly.
The default focused action should be **Cancel**, not Apply.

## 9. Applying an approved rematch

After explicit confirmation:

1. Recheck source and sidecar fingerprints to prevent applying a stale scan.
2. Create a timestamped sidecar backup, for example
   `chapter.md.annotations.md.before-rematch-20261101T120000Z`.
3. Build the complete new sidecar in memory.
4. For every accepted match:
   - update `range.start` and `range.end`;
   - replace `### Original text` with the exact current source span;
   - preserve the stable ID, note, type, and original creation time;
   - record rematch history, including old range, method, displayed confidence,
     and rematch time.
   - for a PDF, regenerate the selected record's document hash, page fragments,
     page-local ranges, boxes, rotation, rectangle, and highlight quads.
5. Leave skipped, ambiguous, malformed, and orphaned annotations unchanged.
6. Validate every rewritten range and quotation against the current source; for
   PDF records, also recompute and compare the complete geometry anchor.
7. Atomically replace the sidecar.
8. Reparse the written file and verify IDs, notes, ranges, and counts.
9. On any failure, retain or restore the backup and report that no complete
   rematch was applied.

The confirmed Apply action should write the sidecar directly and update the
application's saved baseline. It should not create a second rematch-specific
confirmation later at quit.

## 10. Important edge cases

### Duplicate quotations

Use context, projected position, neighboring annotations, and candidate
uniqueness. If two candidates remain close, require manual selection.

### Slightly edited quotation

Show a source diff and lower confidence according to the amount and location of
the change. A short quote with one changed word is riskier than a long paragraph
with one changed word.

### Deleted passage

Do not attach the note to merely related text. Mark it unresolved/orphaned and
retain the note and old quotation.

### Moved or reordered section

Allow a strong exact/context match far from the old offset. Distance must lower
confidence less when both contexts and neighboring annotations support a move.

### Manually edited `### Original text`

Treat the edited quotation as user-provided anchor evidence, but show that it
conflicts with the recorded range or hash. Never overwrite it without the Apply
confirmation and preview.

### Unicode and line endings

All persisted ranges remain Unicode code-point offsets. Candidate mapping must
return offsets in the original, non-normalized source text. CRLF normalization
must not create off-by-one ranges.

### Overlapping annotations

Preserve legitimate overlap. Conflict detection should distinguish intentional
overlap from two unrelated annotations competing for one candidate.

## 11. Proposed user flow

Recommended key: `R` in Normal mode.

1. `R` runs a read-only mismatch scan.
2. If everything is healthy, show a short status message and stop.
3. Otherwise open the read-only candidate review screen.
4. The user accepts, changes, manually links, or skips proposals.
5. Choosing Apply opens the confidence-bearing confirmation prompt.
6. Confirmed Apply performs the backed-up atomic sidecar update.
7. Cancel returns without changing files.

Opening a file may display a passive “N stale annotations” indicator, but it
must not start rematching or open the confirmation prompt automatically.

## 12. Implementation phases

### Phase 0 — audit and approval

- Review this document.
- Decide the open policy questions in section 14.
- Make no code or format changes.

### Phase 1 — structured read model and mismatch report

- Parse complete annotation records without rewriting them.
- Reconstruct blockquoted original text correctly.
- Add fingerprints and optional context for newly created records.
- Produce read-only Healthy/Shifted/Modified/Ambiguous/Orphaned/Malformed
  reports.

### Phase 2 — deterministic matching

- Add edit-delta projection.
- Add local/global exact search.
- Add normalized exact search and offset mapping.
- Add duplicate detection and context scoring.

### Phase 3 — fuzzy candidates and confidence

- Add bounded fuzzy candidate generation.
- Add document-level consistency.
- Implement explainable component scores, penalties, bands, and alternatives.

### Phase 4 — review and confirmation UI

- Add `R`, mismatch summary, candidate diff review, manual linking, and skip.
- Add the required confirmation prompt with approximate correctness.
- Ensure no mutation is reachable without that prompt.

### Phase 5 — safe application and rollback

- Add backup, rematch history, atomic write, validation, and rollback.
- Refresh highlights and alignment only after verified success.

### Phase 6 — evaluation and calibration

- Build a labeled corpus of synthetic and manually reviewed edits.
- Measure precision by confidence band.
- Adjust displayed estimates only from measured results.

## 13. Acceptance tests

At minimum, automated tests must cover:

1. insertion before a quote shifts its range;
2. deletion before a quote shifts it backward;
3. insertion/deletion inside a quote produces a fuzzy candidate;
4. unique exact quotation moved to another section;
5. duplicated quotation resolved by both-side context;
6. duplicated quotation remains ambiguous when context is insufficient;
7. reordered paragraphs do not cause forced old-order matches;
8. deleted passage becomes unresolved;
9. manually edited quotation is surfaced as conflicting evidence;
10. Unicode, emoji, combining characters, CRLF, and multiline quotes;
11. overlapping annotations remain valid;
12. source change after scan invalidates proposals;
13. Cancel leaves both files byte-for-byte unchanged;
14. no write path is reachable without Apply confirmation;
15. prompt displays per-item and batch confidence information;
16. backup and rollback survive an injected write failure;
17. IDs, notes, and creation times remain unchanged after rematch.
18. replacing a PDF and moving a quote to another page regenerates the document
    hash and page geometry, and the rematched sidecar exports successfully.

Evaluation should prioritize precision over recall: leaving an annotation
unresolved is safer than confidently attaching it to the wrong text.

## 14. Audited decisions

The recommended choices were approved and implemented:

1. Trigger rematch review with `R`. **Implemented**
2. Store 80 code points of prefix and suffix context. **Implemented**
3. Update `### Original text` to the confirmed current source span. **Implemented**
4. Create a timestamped backup for every applied rematch. **Implemented**
5. Preselect only proposals estimated at 90% or higher. **Implemented**
6. Require manual review below 90% and offer no automatic candidate below 60%.
   **Implemented**
7. Require clean saved buffers before scanning or applying. **Implemented**
8. Label confidence as a heuristic estimate until calibrated with measured data.
   **Implemented**
9. Record rematch history inside annotation metadata. **Implemented**
10. Keep semantic/AI matching out of the first release. **Implemented**
11. Regenerate and validate PDF geometry anchors during confirmed PDF rematches;
    remove stale geometry when no PDF document is available. **Implemented**

Implementation approval never authorizes applying a rematch to user data. Every
real Apply operation still requires the confidence-bearing confirmation prompt.
