# PDF Support Manual Test Cases

These cases exercise the first text-layer PDF implementation without requiring
you to prepare your own PDF.

## Files

- `pta-text-layer-manual-test.pdf` is a four-page PDF with a real selectable
  text layer. Use it for navigation, search, selection, annotation, sidecar,
  geometry-anchor, and read-only-source tests.
- `pta-image-only-negative-test.pdf` contains visible rasterized words but no
  PDF text layer. The current version should reject it and explain that OCR is
  not supported yet.

## Preparation

From the repository root, install the current working tree if needed:

```bash
python -m pip install -e ".[dev]"
```

Remove a sidecar left by an earlier run if you want a clean test:

```bash
rm -f output/pdf/pta-text-layer-manual-test.pdf.annotations.md
```

## Case 1 Open and navigate a text-layer PDF

Run:

```bash
pta-view output/pdf/pta-text-layer-manual-test.pdf
```

Expected results:

- The left border title is `Extracted PDF text`.
- The title bar reports `4 PDF pages`.
- The source contains headings from `--- PDF page 1 of 4 ---` through
  `--- PDF page 4 of 4 ---`.
- Normal Vim-style movement, scrolling, page movement, `zt`, `zz`, and `zb`
  continue to work.

## Case 2 Search and copy extracted PDF text

1. Press `/`.
2. Enter `anchor-ready phrase` and press Enter.
3. Press `n` or `N` to repeat the search.
4. Select `stable geometry anchor` with Visual mode or the mouse.
5. Press `y`, Ctrl+C, or Cmd+C.

Expected results:

- Search moves to and highlights the phrase on PDF page 1.
- The selected text can be copied exactly as ordinary source text.

## Case 3 Confirm that PDF source editing is blocked

1. Focus the left source pane.
2. Press `i`.

Expected results:

- The source remains in Normal mode and read-only.
- A warning says that PDF source text is an extracted read-only view.
- The original PDF bytes are not changed.

The annotation pane remains editable: press Ctrl+W, then `w`, then `i`, and
confirm that it enters Edit mode without the original-source warning.

## Case 4 Create a single-page PDF annotation

1. Select the exact words `stable geometry anchor` on page 1.
2. Press `a`.
3. Enter `Single-page PDF geometry test.`
4. Press Ctrl+S or Ctrl+Enter.

Expected results:

- The note appears in the right pane.
- The selected source text is highlighted.
- `output/pdf/pta-text-layer-manual-test.pdf.annotations.md` is created.
- The original PDF is unchanged.

Inspect the saved record:

```bash
rg -n "anchor:|pdf_anchor:|document_sha256|page_index|text_range|crop_box|rotation|rect_pdf|quad_points_pdf" \
  output/pdf/pta-text-layer-manual-test.pdf.annotations.md
```

The record should contain both the normal text anchor and a `pdf_anchor`. The
single-page annotation should have one geometry fragment with `page_index: 0`.

## Case 5 Create a multi-page PDF annotation

1. Press `/`, search for `BEGIN CROSS PAGE`, and press Enter.
2. Press Escape to close the search prompt while leaving the cursor at the
   result.
3. Press `v` to begin Visual selection.
4. Press `G` to extend the selection through page 4.
5. Press `a` and save the note `Multi-page PDF geometry test.`

Expected results:

- The note is saved normally even though the selection crosses a generated page
  heading.
- Its `pdf_anchor.fragments` contains two fragments.
- The fragment page indexes are `2` and `3`, corresponding to PDF pages 3 and 4.
- Each fragment has its own `rect_pdf` and `quad_points_pdf` values.

Run this command to see the relevant metadata:

```bash
rg -n "Multi-page PDF geometry test|page_index|rect_pdf|quad_points_pdf" \
  output/pdf/pta-text-layer-manual-test.pdf.annotations.md
```

## Case 6 Repeated text

1. Search for `Shared phrase for ambiguity`.
2. Use `n` and `N` to move between the two occurrences on PDF page 2.
3. Annotate either occurrence.

Expected results:

- Both search occurrences are reachable.
- The saved range and geometry correspond to the occurrence you selected.
- Moving between the source and annotation panes aligns with the selected
  occurrence.

## Case 7 Reject an image-only PDF

Quit the viewer and run:

```bash
pta-view output/pdf/pta-image-only-negative-test.pdf
```

Expected result:

```text
pta-view: PDF has no extractable text layer; scanned PDFs require OCR, which is not supported yet
```

The process should exit with status 2 and should not open the TUI.

## Case 8 Synchronize notes into the canonical PDF

Complete Cases 4 and 5 first so the sidecar contains both a single-page note
and a multi-page note. Use a disposable canonical pair for this destructive
manual check, then synchronize it:

```bash
cp output/pdf/pta-text-layer-manual-test.pdf /tmp/pta-sync-test.pdf
cp output/pdf/pta-text-layer-manual-test.pdf.annotations.md /tmp/pta-sync-test.pdf.annotations.md
pta-import /tmp/pta-sync-test.pdf
shasum -a 256 /tmp/pta-sync-test.pdf
pta-embed /tmp/pta-sync-test.pdf
shasum -a 256 /tmp/pta-sync-test.pdf
```

Expected results:

- The command reports the number of sidecar annotations and generated page
  highlights.
- The two printed hashes differ because the source PDF's annotation layer is
  now synchronized atomically.
- Opening `/tmp/pta-sync-test.pdf` in Preview or another annotation-aware viewer
  shows a yellow highlight over `stable geometry anchor` on page 1.
- The cross-page note appears as one yellow highlight on page 3 and one on
  page 4. Selecting either fragment shows `Multi-page PDF geometry test.`
- The Markdown sidecar receives current `pdf_sync`, `pdf_anchor`,
  `pdf_sync_document_sha256`, and `pdf_sync_sidecar_sha256` metadata for the
  synchronized pair.

An explicit disposable export is still available when needed:

```bash
pta-embed /tmp/pta-sync-test.pdf --output /tmp/review-copy.pdf
pta-embed /tmp/pta-sync-test.pdf --output /tmp/review-copy.pdf --force
```

As a negative check, copy the sidecar to a temporary file, change one
`document_sha256` or `rect_pdf` value, and pass that copy with `--annotations`.
The command should report a stale hash or geometry error and leave the source
PDF byte-for-byte unchanged.

## Case 9 Reopen through the PDF extraction cache

Force one complete extraction, wait for the viewer, and then quit:

```bash
pta-view --rebuild-pdf-cache output/pdf/pta-text-layer-manual-test.pdf
```

Open the same PDF normally:

```bash
pta-view output/pdf/pta-text-layer-manual-test.pdf
```

Expected results:

- The second launch reuses cached canonical text and geometry and should reach
  the viewer more quickly.
- Text, page headings, search results, selection, and annotation geometry are
  identical to the rebuilt result.
- The PDF and sidecar remain unchanged.

For comparison, `--no-pdf-cache` forces extraction without reading or writing
the cache:

```bash
pta-view --no-pdf-cache output/pdf/pta-text-layer-manual-test.pdf
```

Changing any PDF byte, changing the extraction profile or `pdfplumber` version,
or corrupting the cache must also trigger safe re-extraction. A cache read or
write failure must never prevent the PDF from opening.

## Case 10 Save-triggered PDF synchronization and cursor restoration

Open the source, add a PDF annotation or edit an existing note, and save it.
Then search for `BEGIN CROSS PAGE`, leave the source cursor on that result, and
quit with `q`:

```bash
pta-view output/pdf/pta-text-layer-manual-test.pdf
```

Expected results immediately after the annotation save, while the TUI remains
open:

- `pta-text-layer-manual-test.pdf` is atomically replaced with a verified PDF
  whose annotation layer matches the sidecar.
- The sidecar is reconciled once more so its PDF hash and identity metadata
  match the written source exactly.
- Adding a note, pressing Ctrl+S after an annotation edit, or applying a PDF
  rematch all trigger the same refresh.
- If quitting with an unsaved annotation edit, choosing Save synchronizes the PDF
  before exiting; choosing Discard does not embed anything.

Record the source PDF's modification time, then quit without changing or saving
annotations. Its modification time should remain unchanged because an ordinary
quit performs no embedding. Edit and save one note; the receipt must be
invalidated and the source synchronized.

Open the source again. The cursor should return to and center the saved
`BEGIN CROSS PAGE` position. Cursor restoration is ignored safely if the PDF,
extractor version, or extraction profile has changed.

For an opt-out check, launch with:

```bash
pta-view --no-embed-on-save output/pdf/pta-text-layer-manual-test.pdf
```

Save an annotation change and then quit. The sidecar and cursor position should
still be saved, but the source PDF should not be synchronized.

Open the same session again with `--no-embed-on-save`, press `m`, and choose
**Synchronize annotations into source PDF** (or press `E` in the menu). The
source should update immediately even though automatic embedding is disabled.

## Case 11 PDF rematch regenerates export geometry

The automated fixture replaces a one-page source PDF with a two-page revision
that moves the annotated quotation onto page 2, applies the reviewed rematch,
and then exports the result:

```bash
python -m pytest tests/test_pdfs.py -k pdf_rematch_refreshes_geometry -q
```

Expected result: the rematched sidecar contains the revised PDF byte hash and a
`pdf_anchor` fragment with `page_index: 1`. `pta-embed` then succeeds and places
the standard highlight on PDF page 2, rather than retaining the old page-1
coordinates. The old document hash must not remain in the selected record.

## Case 12 Import editable Preview text markup

Make a disposable copy and open that copy in Preview on macOS:

```bash
cp output/pdf/pta-text-layer-manual-test.pdf /tmp/pta-preview-import.pdf
open -a Preview /tmp/pta-preview-import.pdf
```

In Preview, highlight `stable geometry anchor`, add the comment
`Imported from Preview.`, underline another phrase if desired, and save the PDF
normally. Do not use Print > Save as PDF, because printing flattens the markup.
Then run:

```bash
shasum -a 256 /tmp/pta-preview-import.pdf
pta-import /tmp/pta-preview-import.pdf
shasum -a 256 /tmp/pta-preview-import.pdf
pta-view /tmp/pta-preview-import.pdf
```

Expected results:

- `pta-import` reports each matched subtype, range, and confidence.
- `/tmp/pta-preview-import.pdf.annotations.md` contains `pdf_import`,
  `pdf_anchor`, the Preview comment, and the quoted source text.
- The two PDF hashes are identical; import changes only the sidecar.
- The imported quotation and note appear and align normally in `pta-view`.
- A bare highlight or underline receives an explicitly generated no-comment
  note instead of being discarded.
- Existing sidecar records whose text evidence is still exact receive refreshed
  `pdf_anchor` blocks for Preview's newly saved PDF byte hash.

Run `pta-import /tmp/pta-preview-import.pdf` again. It should report that the
PDF and sidecar already match and leave the sidecar byte-for-byte unchanged.
Change the Preview comment and run `pta-import` again; the same sidecar block
must update. Delete the highlight in Preview and import again; the importer must
report a deletion conflict and leave its sidecar block intact for `R` review.
Running `pta-embed /tmp/pta-preview-import.pdf` after resolving conflicts then
normalizes the supported PDF markup to stable sidecar IDs without creating
duplicates.

For the merge check, first synchronize at least one annotation, edit that note
directly in the Markdown file, and add a separate highlight in Preview.
`pta-import` must preserve the Markdown note edit and import the new Preview
highlight in one atomic sidecar update. This must also work for a sidecar
created before record-level `pdf_state_sha256` and `sidecar_state_sha256`
fingerprints were introduced; the next reconciliation adds those fields.

For the conflict check, edit the comment for the *same* annotation in both the
Markdown file and Preview after synchronization. `pta-import` must identify
that annotation, report a record-level conflict, and leave both files
byte-for-byte untouched. Deleting an annotation in Preview always requires the
same explicit review, even when its sidecar record was otherwise unchanged. A
Markdown-only edit with an unchanged PDF must remain intact.

Open the conflicted pair with `pta-view`. The compact conflict review must open
automatically after the two panes appear and identify the affected annotation
while leaving both panes visible. No choice is preselected. Cancel must leave
both files untouched; pressing `R` must reopen the review. For a deleted PDF
annotation:

1. Choose **Restore PDF**, continue to confirmation, and apply. The Markdown
   record and edited note must remain, the annotation must reappear in Preview,
   and a `before-pdf-conflict` sidecar backup must exist.
2. Recreate the conflict on a disposable copy and choose **Delete MD**. The PDF
   remains without that annotation and its Markdown block is removed.

For a same-record note conflict, the corresponding buttons read **Use
Markdown** and **Use PDF**. The chosen note must be identical in both files
after Apply. Canceling either panel must leave both files byte-for-byte
unchanged. If other text anchors are also stale, press `R` again after resolving
the PDF conflict to enter normal range-rematch review.

If embedding first reports `PDF byte hash does not match the source document`,
run `pta-import` once and retry embedding. An annotation-only Preview save
should produce a `Refreshed ... existing PDF anchor(s)` message. If Preview also
changed the extracted text, the importer deliberately leaves the old anchor
alone and `R` must be used for reviewed text rematching.

## Pass checklist

| Check | Expected |
| --- | --- |
| Four pages extracted with visible page headings | Pass |
| PDF text search and copy | Pass |
| Source Edit mode blocked | Pass |
| Annotation pane remains editable | Pass |
| Single-page note stores one PDF fragment | Pass |
| Multi-page note stores page indexes 2 and 3 | Pass |
| Saving annotations atomically updates the canonical source PDF | Pass |
| Image-only PDF is rejected with OCR message | Pass |
| `pta-embed` creates standard markup in the source PDF | Pass |
| Cross-page note creates one highlight on each source page | Pass |
| Explicit output collisions and stale geometry fail closed | Pass |
| A failed synchronization leaves the source PDF unchanged | Pass |
| Unchanged PDF reopens from the exact-hash extraction cache | Pass |
| Changed or corrupt cache data falls back to extraction | Pass |
| Successful annotation saves synchronize the source PDF | Pass |
| `m`, then `E` manually embeds even with automatic embedding disabled | Pass |
| Deleting a PDF annotation removes its generated highlight on refresh | Pass |
| Ordinary quit performs no PDF embedding | Pass |
| Any source or sidecar change invalidates the skip | Pass |
| Reopening an unchanged source restores and centers its cursor | Pass |
| Confirmed PDF rematch regenerates geometry and embeds on the new page | Pass |
| Preview text markup adds/updates/deletes sidecar records | Pass |
| Reconciliation and later embedding do not duplicate source markup | Pass |
| `R` reviews PDF deletion/edit conflicts without hiding either pane | Pass |
| Restore PDF and Delete MD choices create a backup and reconcile both files | Pass |
