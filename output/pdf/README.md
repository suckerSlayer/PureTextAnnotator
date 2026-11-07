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
rm -f output/pdf/pta-text-layer-manual-test.annotated.pdf
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

## Case 8 Embed the notes into a PDF copy

Complete Cases 4 and 5 first so the sidecar contains both a single-page note
and a multi-page note. Record the source hash, then export:

```bash
shasum -a 256 output/pdf/pta-text-layer-manual-test.pdf
pta-embed output/pdf/pta-text-layer-manual-test.pdf
shasum -a 256 output/pdf/pta-text-layer-manual-test.pdf
```

Expected results:

- The command reports the number of sidecar annotations and generated page
  highlights.
- `output/pdf/pta-text-layer-manual-test.annotated.pdf` is created.
- The two printed hashes of the source PDF are identical.
- Opening the annotated copy in Preview or another annotation-aware PDF viewer
  shows a yellow highlight over `stable geometry anchor` on page 1.
- The cross-page note appears as one yellow highlight on page 3 and one on
  page 4. Selecting either fragment shows `Multi-page PDF geometry test.`
- The Markdown sidecar is unchanged.

Run the same command again. It should stop with an `output already exists`
message. Explicit replacement is available only for the separate copy:

```bash
pta-embed output/pdf/pta-text-layer-manual-test.pdf --force
```

As a negative check, copy the sidecar to a temporary file, change one
`document_sha256` or `rect_pdf` value, and pass that copy with `--annotations`.
The command should report a stale hash or geometry error and create no output.
It must also reject `--output output/pdf/pta-text-layer-manual-test.pdf`, even
when `--force` is supplied.

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

## Case 10 Save-triggered export and cursor restoration

Open the source, add a PDF annotation or edit an existing note, and save it.
Then search for `BEGIN CROSS PAGE`, leave the source cursor on that result, and
quit with `q`:

```bash
pta-view output/pdf/pta-text-layer-manual-test.pdf
```

Expected results immediately after the annotation save, while the TUI remains
open:

- `pta-text-layer-manual-test.annotated.pdf` is created or atomically refreshed.
- The embedding step does not modify the original PDF or make any further
  change to the newly saved sidecar.
- Adding a note, pressing Ctrl+S after an annotation edit, or applying a PDF
  rematch all trigger the same refresh.
- If quitting with an unsaved annotation edit, choosing Save refreshes the copy
  before exiting; choosing Discard does not embed anything.

Record the generated copy's modification time, then quit without changing or
saving annotations. Its modification time should remain unchanged because an
ordinary quit no longer performs embedding. Edit and save one note; the receipt
must be invalidated and the copy refreshed. Replacing or modifying the generated
copy must likewise force a refresh on the next annotation save.

Open the source again. The cursor should return to and center the saved
`BEGIN CROSS PAGE` position. Cursor restoration is ignored safely if the PDF,
extractor version, or extraction profile has changed.

For an opt-out check, launch with:

```bash
pta-view --no-embed-on-save output/pdf/pta-text-layer-manual-test.pdf
```

Save an annotation change and then quit. The sidecar and cursor position should
still be saved, but the annotated PDF copy should not be refreshed.

Open the same session again with `--no-embed-on-save`, press `m`, and choose
**Embed annotations into PDF copy** (or press `E` in the menu). The copy should
refresh immediately even though automatic embedding is disabled. The source PDF
must remain byte-for-byte unchanged.

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

## Pass checklist

| Check | Expected |
| --- | --- |
| Four pages extracted with visible page headings | Pass |
| PDF text search and copy | Pass |
| Source Edit mode blocked | Pass |
| Annotation pane remains editable | Pass |
| Single-page note stores one PDF fragment | Pass |
| Multi-page note stores page indexes 2 and 3 | Pass |
| Original PDF remains byte-for-byte unchanged | Pass |
| Image-only PDF is rejected with OCR message | Pass |
| `pta-embed` creates standard yellow highlights in a copy | Pass |
| Cross-page note creates one highlight on each source page | Pass |
| Existing output and stale geometry fail closed | Pass |
| Source PDF and sidecar remain byte-for-byte unchanged | Pass |
| Unchanged PDF reopens from the exact-hash extraction cache | Pass |
| Changed or corrupt cache data falls back to extraction | Pass |
| Successful annotation saves refresh the default annotated PDF copy | Pass |
| `m`, then `E` manually embeds even with automatic embedding disabled | Pass |
| Deleting a PDF annotation removes its generated highlight on refresh | Pass |
| Ordinary quit performs no PDF embedding | Pass |
| Any source, sidecar, or generated-copy change invalidates the skip | Pass |
| Reopening an unchanged source restores and centers its cursor | Pass |
| Confirmed PDF rematch regenerates geometry and embeds on the new page | Pass |
