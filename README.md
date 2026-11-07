# Pure Text Annotator

Pure Text Annotator is a keyboard-first terminal interface for UTF-8 text,
Markdown, and text-layer PDF files. It provides Vim-style navigation,
selection, guarded text editing, and notes in a human-readable Markdown sidecar
shown beside the source.

## Set up

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

## Run

```bash
pta-view path/to/file.txt
pta-view path/to/file.md
pta-view path/to/paper.pdf
```

You can also run the package directly:

```bash
python -m pure_text_annotator path/to/file.md
```

Use `--no-wrap` to keep long lines unwrapped.

## PDF support

PDFs with an embedded text layer open as a deterministic plain-text projection.
Each page starts with a visible `--- PDF page N of M ---` heading, followed by
text in the extractor's top-to-bottom, left-to-right reading order. Search,
Vim-style movement, selection, copy, annotation, alignment, and rematching then
use the same interface as text and Markdown files.

The PDF itself is immutable: pressing `i` in the PDF source pane shows a
read-only warning instead of opening Edit mode. The annotation pane remains
editable, and notes are saved beside `paper.pdf` as
`paper.pdf.annotations.md`. A PDF with no extractable text layer is rejected
with a clear message; OCR and a rendered page view are not part of this version.

Every PDF annotation stores two complementary anchors:

- The normal Unicode code-point range, quotation hash, surrounding context, and
  source-text hash used by the current viewer and rematcher.
- A `pdf_anchor` containing the raw PDF SHA-256, extractor version and
  profile, zero-based page index, page-text hash, crop box, rotation, selected
  text range, PDF-space rectangle, and standard text-markup quad points.

Multi-page selections create one geometry fragment per page. Coordinates use
PDF user space with a bottom-left origin. Geometry is valid only for the exact
recorded PDF hash.

### PDF extraction cache

The first opening still performs complete text and character-geometry
extraction. Pure Text Annotator then stores a compressed cache in the operating
system's user-cache directory. On later openings it reads and hashes the PDF,
then reuses the cached canonical text, page metadata, and geometry only when all
of these values match:

- The SHA-256 of the complete PDF file.
- The cache schema version.
- The extraction profile.
- The `pdfplumber` version.

Changing even one PDF byte therefore causes a cache miss and a fresh
extraction. A corrupt, missing, or unwritable cache is ignored safely; opening
the PDF continues through the normal extraction path. Cache files use private
permissions, are written atomically, and contain extracted document text, so
they should be handled as private local data. Deleting them is safe because
they can always be regenerated.

Use these diagnostic controls when needed:

```bash
pta-view --rebuild-pdf-cache path/to/paper.pdf
pta-view --no-pdf-cache path/to/paper.pdf
python -c "from pure_text_annotator.pdfs import pdf_cache_directory; print(pdf_cache_directory())"
```

`PTA_PDF_CACHE_DIR` may be set to choose another cache directory. The cache is
also used automatically when `pta-embed` verifies PDF geometry.

Use `pta-embed` to write the sidecar notes as standard yellow `/Highlight`
annotations in a separate PDF copy:

```bash
pta-embed path/to/paper.pdf
pta-embed path/to/paper.pdf \
  --annotations path/to/notes.md \
  --output path/to/review-copy.pdf
```

The default input is `paper.pdf.annotations.md` and the default output is
`paper.annotated.pdf`. Each page fragment becomes one PDF highlight carrying
the note text, stable annotation ID, author, subject, and creation timestamp.
Consequently, a single note that crosses two pages produces two highlights
with the same ID and note text.

Every successful PDF annotation save triggers an immediate refresh of the
conventional `paper.annotated.pdf` copy. This includes adding a note, pressing
Ctrl+S after editing annotations, applying a confirmed rematch, and choosing
Save while quitting with annotation changes. The refresh may atomically replace
only that generated copy; it never replaces the source PDF or sidecar. An
ordinary quit, or choosing Discard, does not perform embedding.

After a successful export, a private receipt records exact SHA-256 fingerprints
of the source, sidecar, and generated copy. A later annotation save reports
`Annotated PDF already current` without rebuilding only when all three files
still match. Changing or replacing any one of them invalidates the receipt and
the next annotation save triggers a fully validated atomic refresh. Receipt
corruption or an unwritable state directory merely disables the shortcut; it
never bypasses validation or blocks export. Set `PTA_EMBED_STATE_DIR` to choose
another receipt directory.

Use this option to skip the automatic refresh for one session:

```bash
pta-view --no-embed-on-save path/to/paper.pdf
```

Export is deliberately fail-closed. Every sidecar record must be a valid note
with a current text anchor and `pdf_anchor`; the quotation, hashes, page data,
range, and recomputed geometry must all match. No partial export is produced.
The generated copy is reopened before replacement and checked for unchanged
page content streams, page boxes, rotation, resource names, annotation counts,
note data, and highlight geometry. This avoids a second full PDF text
extraction while still detecting unintended page-content changes.
The standalone `pta-embed` command refuses encrypted or signed PDFs, an
existing output unless `--force` is supplied, and any request to overwrite the
source PDF. `--force` may replace only the separate output. Existing
non-conflicting PDF annotations are preserved, and both the source PDF and
Markdown sidecar remain unchanged. If a PDF has been replaced or a range
becomes stale, run the confirmed rematch workflow before exporting; applying a
PDF rematch regenerates that record's geometry automatically.

## Resume cursor position

On a confirmed quit, the source cursor row and column are written atomically to
the operating system's private user-state directory. Reopening the same
resolved source path restores and centers that cursor only when the canonical
source fingerprint still matches. Any text change—or a PDF hash, extractor, or
extraction-profile change—invalidates the saved position instead of risking a
jump to unrelated content.

The small state file contains the resolved source path, fingerprint, row, and
column. It uses private permissions and may be deleted safely. Set
`PTA_SESSION_STATE_DIR` to choose another directory, or print the active one:

```bash
python -c "from pure_text_annotator.session_state import session_state_directory; print(session_state_directory())"
```

## Controls

| Keys | Action |
| --- | --- |
| `h` `j` `k` `l` or arrow keys | Move the cursor |
| `w` | Move to the start of the next Vim word |
| `b` | Move to the previous word |
| `e` | Move to the end of the word |
| `0` `$` or Home/End | Move to line start/end |
| `gg` / `G` | Jump to document start/end |
| `[count]gg` | Jump to the numbered line, for example `25gg` |
| Ctrl+F / Ctrl+B or Page Down / Page Up | Move one page |
| Ctrl+E / Ctrl+Y | Scroll down/up by one line |
| `zt` / `zz` / `zb` | Place the cursor line at the top/middle/bottom |
| Ctrl+O / Ctrl+I | Jump to the older/newer position in the focused pane's history |
| `v` | Start or finish visual selection |
| `a` | Add a note for the selected source text |
| `i` | Enter Edit mode for the focused pane |
| Ctrl+W in Edit mode | Delete the previous word |
| Ctrl+H in Edit mode | Delete the previous character |
| Ctrl+S | Save dirty source and annotation buffers without quitting |
| Escape or Ctrl+`[` in Edit mode | Return to Normal mode |
| `u` in Normal mode | Undo the most recent edit batch |
| Ctrl+R in Normal mode | Redo the most recently undone batch |
| `/` | Search within the focused pane |
| `n` / `N` | Repeat the pane's search forward / backward |
| Ctrl+N / Ctrl+P | Jump to the next / previous annotation |
| `m` | Open the annotation action menu |
| `R` | Scan stale annotations and open rematch review |
| `S` | Cycle alignment mode: Normal → Sync → Off |
| `.` | Manually align and center both panes' cursor lines |
| `zi` in annotations | Toggle metadata folding on/off |
| `zo` / `zc` in annotations | Open/close the metadata fold at the cursor |
| Ctrl+W, then `w` | Switch between source and annotation panes |
| Shift+arrow keys | Extend a selection |
| Mouse drag | Select text |
| `y`, Ctrl+C, or Cmd+C | Copy selected text |
| Escape | Clear the selection |
| `q` or Ctrl+Q | Quit; prompt for unsaved changes and remember the cursor |

The `w`, `b`, and `e` motions are available in both panes and follow Vim's
lowercase-word boundaries: letters, numbers, and underscores form one kind of
word, while adjacent non-blank symbols form another. Whitespace is skipped.

A single `g` is only a pending prefix and does not move the cursor. `gg`, `G`,
counted `gg`, annotation navigation, searches, and explicit cross-pane alignment
record jumps. Ctrl+O walks backward and Ctrl+I walks forward through a separate
history for each pane. Because terminals encode Ctrl+I and Tab as the same key,
Tab also performs a forward-history jump; Tab and Shift+Tab no longer switch
panes.

Both panes provide trailing scroll space equal to half their visible height.
This lets the final document line move to the middle of its pane with `zz` or
manual alignment; the blank space is visual only and is never saved to either
file.

## Annotation action menu

Press `m` in Normal mode from either pane to open a compact menu while keeping
the source and annotation panes visible. **Add annotation** is focused by
default. The menu provides these actions:

- **Add annotation** opens the note prompt for the current source selection,
  with the same validation as the `a` key.
- **Enter Edit mode** acts on the pane from which the menu was opened. Source
  editing still requires the literal `Yes` warning confirmation; annotation
  editing starts immediately.
- **Switch Sync mode** cycles Normal, Sync, and Off alignment just like `S`.
- **Delete current annotation** asks for confirmation, atomically removes the
  selected sidecar block, renumbers the remaining display headings, and leaves
  the source unchanged. Save or discard unrelated edits before deleting.
- **Rematch annotations** opens the same review-first workflow as `R`.
- **Show keybindings** opens a scrollable in-application reference.
- **Embed annotations into PDF copy** manually refreshes the default
  `.annotated.pdf` copy from all saved annotations. It is available only for PDF
  sources, never overwrites the original PDF, and remains available when
  `--no-embed-on-save` disables automatic refreshes.

In Normal mode, Ctrl+N and Ctrl+P move through annotations with wraparound. In
the source pane, navigation follows source-range order; in the annotation pane,
it follows sidecar order. Cross-pane movement continues to respect the current
Normal, Sync, or Off alignment mode.

## Edit text

With a text or Markdown source pane focused, press `i` to request Edit mode.
Because this mode changes the original file, a compact warning panel opens at
the bottom while both text panes remain visible. Type the exact case-sensitive
text `Yes` and press Enter (or select **Edit original**) to proceed; any other
input is rejected. Escape cancels the warning. PDF source projections are
always read-only.

With the annotation pane focused, press `i` to enter Edit mode immediately; no
warning or confirmation is shown. Changes are reparsed in memory so recorded
ranges and source highlights immediately reflect the edited annotation document.

In either pane's Edit mode, printable keys and Enter edit at the cursor. Ctrl+W
deletes the previous word, Ctrl+H deletes the previous character, and Escape or
Ctrl+`[` returns to Normal mode. Ctrl+Q remains available to request quitting.
Changed buffers are marked `MODIFIED` in the status line. Press Ctrl+S in either
Normal or Edit mode to atomically save every dirty pane without quitting; Edit
mode remains active.

In Normal mode, `u` reverts the most recent in-memory edit batch in the focused
pane and Ctrl+R reapplies the most recently undone batch. Separate Edit-mode
sessions create separate undo steps.

When quitting with modified text, choose **Save** to atomically write every dirty
buffer and quit, **Discard** to quit without writing, or **Cancel** to return to
the application. Source saves preserve the UTF-8 BOM, newline style, and file
permissions. A new annotation cannot be added while either text buffer remains
modified; save or discard those edits first.

Source edits do not silently rewrite existing annotation ranges or quoted
original text. After saving with Ctrl+S and returning to Normal mode, press `R`
to scan for stale annotations and review proposed rematches.

The viewer uses Textual's terminal clipboard support and also invokes the
native clipboard utility on macOS, Windows, Wayland, or X11 when one is
available.

Search is literal and case-sensitive. Press `/` in either pane, enter the text,
and press Enter. The current match is highlighted and centered; `n` and `N`
continue through matches with wraparound. Escape closes the search prompt
without moving the cursor. Each pane remembers its own most recent query.

## Recover stale annotation ranges

Press `R` in Normal mode with clean, saved buffers. The scan is read-only: it
classifies each annotation as Healthy, Shifted, Modified, Ambiguous, Orphaned,
or Malformed, then searches exact text, conservatively normalized text, saved
prefix/suffix context, deterministic fuzzy candidates, and neighboring
annotation order. No network or AI service is used.

The review is a compact panel at the bottom, leaving both text panes visible.
For each proposal, the source pane scrolls to the proposed passage and marks it
in green, while the annotation pane shows the stored quotation and note. The
panel itself is limited to the old/proposed ranges, line and column, method,
evidence, rounded approximate correctness estimate, and decision controls.

Use `J`/`K` to navigate annotations, Space to accept or skip, and `C` /
`Shift+C` to move forward/backward through candidates. The two panes update
immediately, so alternatives can be checked in their real source context before
applying anything. Proposals estimated at 90% or higher are preselected;
lower-confidence and ambiguous proposals require an explicit choice. Orphaned
and malformed annotations remain unchanged.

For a manual match, cancel review, make a Visual selection in the source pane,
press `R`, navigate to the intended annotation, and press `M`. The selected span
receives an estimate and follows the same confirmation path.

Ctrl+S from review opens the only rematch Apply confirmation. This is another
compact bottom panel, so both text panes remain visible and the proposed source
span stays highlighted. Use `J`/`K` to preview every selected change. The panel
reports each choice's method and estimate, plus the batch's average, lowest,
ambiguous, and unresolved counts. Cancel and Review do not write anything.
**Apply** rechecks source and sidecar fingerprints, creates a timestamped
`before-rematch` backup, updates only the selected sidecar ranges and quoted
text, records rematch history, atomically writes, reparses, and rolls back on
failed validation. For PDF sources, Apply also regenerates every selected
record's `pdf_anchor` from the current PDF, including its document hash, page
fragments, text ranges, boxes, rotation, and highlight quads. The written
geometry is validated before the transaction completes. If PDF geometry is not
available, an old anchor is removed so later export fails safely instead of
using stale coordinates. The source file is never changed by rematching.

Estimates are transparent heuristics rather than guarantees. Short, duplicated,
or substantially changed quotations are deliberately penalized; leaving a note
unresolved is preferred to attaching it to weakly related text.

### Try the resettable cases

```bash
pta-view examples/rematch_cases/cases.md
```

This marked sample covers a shifted range, a slight edit, duplicate quotation,
and deletion. Its first `R` opens a separate reset warning; after confirming the
reset, press `R` again for rematch review. See
[`examples/rematch_cases/README.md`](examples/rematch_cases/README.md) for the
walkthrough. Applying a demo rematch makes its next `R` offer the baseline reset
again.

## Alignment modes

The status line shows the current alignment mode. Press `S` to cycle through:

| Mode | Automatic alignment |
| --- | --- |
| Normal (default) | Source movement aligns the annotation pane; annotation movement stays local. |
| Sync | Movement in either pane aligns the other pane. |
| Off | Movement in either pane stays local. |

In every mode, press `.` to align manually from the focused pane: from the
source it centers the corresponding annotation on `### Note`; from an annotation
it centers the source on that annotation's recorded range. Manual alignment
always scrolls both panes so their cursor lines are at their viewport midpoints
when the document boundaries allow it.

## Annotation metadata folds

Metadata starts folded. The document front matter and each annotation's YAML
metadata initially collapse to one muted summary line. With the annotation pane
focused, press `zi` to toggle metadata folding globally. Move onto a metadata
region and use `zc` to close it or `zo` to open it; opening and closing one fold
preserves the state of the others. All three commands retain the cursor's visual
row in the viewport while the surrounding metadata expands or collapses.

Folding changes only the annotation pane's visual projection. Editing, saving,
undo/redo, rematching, alignment, and dirty tracking continue to use the complete
sidecar. Entering Edit mode temporarily unfolds the full sidecar; returning to
Normal mode restores the selected folds. Both transitions preserve the cursor's
visual row in the viewport, so expanding or collapsing hidden metadata does not
make the editing position jump on screen.

## Add an annotation

1. Press `v` and move the cursor to select source text.
2. Press `a` to open the note dialog.
3. Write a Markdown note.
4. Press Ctrl+S or Ctrl+Enter to save, or Escape to cancel.

The right pane mirrors the complete annotation file. For a source named
`chapter.md`, notes are saved beside it as `chapter.md.annotations.md`; for
`paper.pdf`, the sidecar is `paper.pdf.annotations.md`. Each entry contains a
stable identifier, creation time, the original quotation, the note, and a
zero-based half-open source range measured in Unicode code points. PDF entries
created by this version also include the page geometry described above.
Recorded ranges are highlighted in yellow in the source pane when the file opens
and immediately after a new note is saved. The currently aligned range uses the
same blue as the focused annotation's original quotation. Moving through the
source centers the matching annotation on its `### Note` heading. When the cursor
is between annotated ranges, the nearest annotation remains aligned; an
exact-distance tie keeps the current annotation focused. Annotation movement
returns the source pane to the corresponding recorded range in Sync mode, or
when requested manually with `.` in Normal and Off modes.

The focused annotation is visually distinct in the right pane: its
`## Annotation N` heading uses the normal text color, its original quotation has
a separate highlight color, and only its note body receives the active-note
highlight. Metadata is muted, and complete non-focused annotation blocks are
dimmed so the current note remains easy to identify.

## Current scope

- One file per application launch
- `.txt`, `.md`, `.markdown`, and text-layer `.pdf` files
- UTF-8 input, including files with a UTF-8 byte-order mark
- PDF source viewing, geometry-aware annotation, and safe highlight export;
  no OCR or rendered-page pane
- Exact-hash PDF extraction caching for faster repeat opens
- Exact-source cursor restoration across viewer sessions
- Independent Vim-style jump histories for the source and annotation panes
- Automatic refresh of the default annotated PDF copy after annotation saves
- Guarded source editing and direct annotation editing with dirty tracking
- Line numbers in both panes and optional wrapping
- Persistent Markdown sidecar annotations with atomic saving
- Original quotation, source range, hashes, and bounded context recorded for
  new notes
- Review-first deterministic rematching with confidence estimates, backup,
  validation, and rollback
- Structured deletion of the current annotation with confirmation
- Literal search with forward/backward repetition in both panes
- No structured annotation-type filtering or semantic/AI matching yet

## Test

```bash
.venv/bin/python -m pytest
```

