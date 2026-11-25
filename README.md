# Pure Text Annotator

Pure Text Annotator is a keyboard-first terminal interface for UTF-8 text,
Markdown, and text-layer PDF files. It provides Vim-style navigation,
selection, guarded text editing, and notes in a human-readable Markdown sidecar
shown beside the source. Annotations have three first-class types: Markdown
notes, range highlights, and cursor-position bookmarks.

## Set up

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```
activate venv in Powershell
```bash
.\.venv\Scripts\Activate.ps1
```

### Older Intel Macs

#### Dependency installation

On older Intel (`x86_64`) Macs, dependency installation may fall back to
building `cryptography` from source and then fail with a Cargo/LibreSSL error
while connecting to `index.crates.io`. Install the last `cryptography` release
series that provides a compatible Intel macOS wheel before installing Pure
Text Annotator:

```bash
python -m pip install --only-binary=cryptography "cryptography<49"
python -m pip install -e ".[dev]"
```

The `--only-binary` option prevents another source-build attempt. This
workaround is intended for older Intel Macs; current Apple silicon Macs can use
the standard setup instructions above.

#### Terminal colors

On some legacy macOS systems, highlight colors may render differently in a
third-party terminal emulator even when its `TERM` and true-color settings are
correct. A tested workaround is to create the tmux session and launch
`pta-view` from Apple's Terminal.app, then detach and attach that session from
the preferred terminal emulator.

In Terminal.app, start a named session and run the viewer inside it:

```bash
tmux new-session -s pta
pta-view path/to/file.txt
```

Detach from the session by pressing Ctrl+B, then D. In the preferred terminal
emulator, attach to the same running session:

```bash
tmux attach-session -t pta
```

When `pta` is the only tmux session, the shorter `tmux a` command can be used
instead. This keeps the color environment established through Terminal.app
while allowing the session to be viewed and controlled from another terminal.

## Run

```bash
pta-view path/to/file.txt
pta-view path/to/file.md
pta-view path/to/paper.pdf
pta-view --annotations path/to/old.annotations.md path/to/renamed-file.md
```

You can also run the package directly:

```bash
python -m pure_text_annotator path/to/file.md
```

Use `--no-wrap` to keep long lines unwrapped.

## Formal acceptance results

The reproducible thesis acceptance run uses 120 ground-truth rematch cases and
a generated 1,000,000-byte source with 1000 annotations. On the recorded Apple
M4 Mac mini environment, unique unchanged quotations were located correctly in
65/65 cases, all 10 complete loads finished within 2 seconds, and 197/200
common operations (98.5%) responded within 100 ms. The complete regression
suite passed 181 tests.

Read [the formal acceptance report](FORMAL_ACCEPTANCE_TEST_REPORT.md) for the
measurement boundaries, all results, limitations, and the separate
full-animation diagnostic. Reproduce the run with:

```bash
.venv/bin/python acceptance/run_acceptance.py
```

The deterministic cases, machine-readable results, fixture hashes, and more
detailed instructions are under [`acceptance/`](acceptance/README.md).

For the separate human check of Note, Highlight, and Bookmark workflows, run:

```bash
.venv/bin/python acceptance/generate_manual_acceptance.py
```

Follow the generated
[`START_HERE.md`](acceptance/manual/generated/START_HERE.md). The recorded
acceptance run completed all 13 checks successfully and retained 20 real PNG
screenshots; see the completed
[`EVIDENCE_RECORD.md`](acceptance/manual/generated/EVIDENCE_RECORD.md). This
suite is intentionally separate from the automated benchmark and does not
fabricate screenshots.

## Documentation

- [User manual](USER_MANUAL.md): installation, reading, editing, three
  annotation types, filtering, rematching, conflict handling, rebinding, and
  PDF workflows.
- [Annotation format specification](ANNOTATION_FORMAT_SPEC.md): normative
  version-1 sidecar structure, Unicode ranges, anchors, PDF extensions,
  validation, and compatibility.
- [Example index](examples/README.md): format examples, resettable rematch
  cases, recorded human evidence, and machine acceptance data.
- [Formal acceptance report](FORMAL_ACCEPTANCE_TEST_REPORT.md): accuracy,
  performance, regression, and completed manual-acceptance results.

## PDF support

PDFs with an embedded text layer open as a deterministic plain-text projection.
Each page starts with a visible `--- PDF page N of M ---` heading, followed by
text in the extractor's top-to-bottom, left-to-right reading order. Search,
Vim-style movement, selection, copy, annotation, alignment, and rematching then
use the same interface as text and Markdown files.

The PDF page content remains read-only: pressing `i` in the PDF source pane
shows a warning instead of opening Edit mode. Its standard annotation layer is
mutable and is synchronized with the editable Markdown sidecar saved beside
`paper.pdf` as `paper.pdf.annotations.md`. The source PDF and that sidecar are
the canonical pair; `.annotated.pdf` files are no longer the default working
document. A PDF with no extractable text layer is rejected with a clear
message; OCR and a rendered page view are not part of this version.

Every PDF annotation stores two complementary anchors:

- The normal Unicode code-point range, quotation hash, surrounding context, and
  source-text hash used by the current viewer and rematcher.
- A `pdf_anchor` containing the raw PDF SHA-256, extractor version and
  profile, zero-based page index, page-text hash, crop box, rotation, selected
  text range, PDF-space rectangle, and standard text-markup quad points.

Multi-page selections create one geometry fragment per page. Coordinates use
PDF user space with a bottom-left origin. Geometry is valid only for the exact
recorded PDF hash.

### Import existing PDF text markup

Use `pta-import` to reconcile editable text-markup annotations created by
Preview, Acrobat, Pure Text Annotator, or another standards-compatible PDF
program into the Markdown sidecar:

```bash
pta-import path/to/reviewed.pdf
pta-import path/to/reviewed.pdf \
  --annotations path/to/notes.md
```

The importer supports `/Highlight`, `/Underline`, `/Squiggly`, and
`/StrikeOut`. It maps each annotation's `/QuadPoints` onto the same extracted
character geometry used by the viewer. Only continuous text matches at or above
the conservative confidence threshold are imported. Standalone notes, free
text, drawings, stamps, links, and flattened page graphics are reported as
unsupported and left unchanged.

Each imported record contains the recovered Unicode range and quotation, normal
text and PDF anchors, mapping confidence, subtype, external annotation name,
author, subject, dates, color, original rectangle and quad points, source-PDF
hash, and a stable import fingerprint. `pdf_sync` metadata records the durable
identity confirmed in the PDF. A markup annotation with no `/Contents` comment
is classified as `type: highlight` and keeps an empty `### Note` section. PTA
does not synthesize an explanatory sentence, so imported provenance cannot be
mistaken for text written by the user.

PTA-written markup carries the sidecar type redundantly: `/PTAType` stores the
machine-readable value `note`, `highlight`, or `bookmark`, while `/Subj` stores
the human-readable value `Pure Text Annotator <type>`. Import resolves type in
that order: valid `/PTAType`, then the typed `/Subj`, then the legacy contents
heuristic (non-empty comment means note; no comment means highlight). This lets
a PDF written by this version reconstruct all three types even when its sidecar
is missing. It also survives PDF tools that discard an unknown custom key but
retain `/Subj`.
Imported third-party markup retains its original author, subject, subtype, and
color when written back; PTA still adds `/PTAType` so its sidecar type can round
trip. A bookmark remains a one-character standard text-markup anchor rather
than becoming a PDF outline entry.

Reconciliation is bidirectional and identity-aware. New PDF markup is appended,
and changed range or comment data replaces the corresponding sidecar block.
Deleting a previously confirmed annotation from the PDF is never propagated
silently: the sidecar record remains until the user chooses **Restore PDF** or
**Delete MD** in `pta-view`'s `R` review. The reverse direction is also guarded:
if an externally edited sidecar removes a synchronized record while its PDF
markup remains, choose **Delete PDF** to accept the Markdown deletion or
**Restore MD** to recover the record from the PDF. A sidecar-only record that
has never been embedded is not mistaken for a deletion. Every non-conflicting
change is committed with one atomic sidecar replacement. `pta-view` performs
the same PDF-to-sidecar reconciliation before opening the panes. The import
command itself never modifies the PDF. PDF files flattened through printing no
longer contain editable annotation dictionaries and therefore cannot be
reconciled.

Saving markup in another PDF program changes the PDF byte hash and can make
older sidecar `pdf_anchor` blocks stale even when the extracted text and layout
did not change. During reconciliation, existing anchors are refreshed for the
current PDF only when their complete canonical-source hash, range, quotation,
and quotation hash are still exact. The sidecar front matter records the exact
`pdf_sync_document_sha256` revision that was completely scanned and a
`pdf_sync_sidecar_sha256` baseline excluding those self-referential fields. A
Markdown-only edit is therefore preserved when the PDF is unchanged. Each
confirmed record also stores separate PDF-state and sidecar-state fingerprints,
so independent edits merge automatically: for example, a Markdown note edit is
kept while a different highlight added in Preview is imported. Reconciliation
stops without modifying either file only when the same annotation changed
differently on both sides, or whenever the PDF deleted a confirmed annotation.
Existing sidecars without record-level fingerprints are
migrated conservatively from their retained PDF provenance. If a supported
markup cannot be mapped confidently, the new revision is not marked
synchronized and no missing sidecar records are deleted. If text evidence
differs, the record remains untouched and must use the normal reviewed rematch
workflow. Opening the pair with `pta-view` immediately shows unresolved
PDF/Markdown conflicts instead of resolving them blindly at the command line.
If the review is canceled, press `R` to reopen it later.

Type participates in both record-level state fingerprints and conflict review.
When the PDF and Markdown change the same annotation to different types, the
review and final confirmation show both values before **Use Markdown** or
**Use PDF** is applied. Version-1 synchronization fingerprints written before
PDF type tags existed are recognized and migrated without a false conflict.

The importer uses the normal exact-hash PDF extraction cache. Use
`--no-pdf-cache` or `--rebuild-pdf-cache` for the same diagnostics available to
`pta-view`.

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

Use `pta-embed` to reconcile the Markdown records into the source PDF's
standard annotation layer:

```bash
pta-embed path/to/paper.pdf
pta-embed path/to/paper.pdf \
  --annotations path/to/notes.md \
  --output path/to/review-copy.pdf
```

The default sidecar is `paper.pdf.annotations.md`, and the default target is
`paper.pdf` itself. Each page fragment becomes one standard text-markup
annotation carrying the optional body text and stable sidecar ID. PTA-created
notes, highlights, and bookmarks use yellow `/Highlight`; imported records
retain their supported subtype, color, author, and subject. A cross-page range
produces one PDF annotation per page with the same ID, body text, and
`/PTAType`. `--output`
remains available for an
explicit disposable export; `--force` applies only to replacing that explicit
output.

Every successful PDF annotation save triggers immediate source-PDF
synchronization. This includes adding or deleting a note, pressing Ctrl+S after
editing annotations, applying a confirmed rematch, and choosing Save while
quitting with annotation changes. An ordinary quit, or choosing Discard, does
not rewrite the PDF.

The source is never edited in place while validation is pending. PTA builds a
temporary candidate, reopens it, verifies unchanged page content and geometry,
and then atomically replaces the source. It immediately reconciles the written
PDF back into the sidecar so byte hashes, identities, and geometry anchors all
describe the new revision. If post-write reconciliation fails, the original
PDF bytes are restored. A private receipt records the exact synchronized PDF
and sidecar hashes so an unchanged save can be skipped safely. Receipt
corruption or an unwritable state directory merely disables that shortcut. Set
`PTA_EMBED_STATE_DIR` to choose another receipt directory.

Use this option to skip the automatic refresh for one session:

```bash
pta-view --no-embed-on-save path/to/paper.pdf
```

Synchronization is deliberately fail-closed. Every sidecar record must have a
supported type and a current text anchor and `pdf_anchor`; notes additionally
require non-empty body text. The quotation, hashes, page data,
range, and recomputed geometry must all match. No partial update is produced.
The candidate PDF is reopened before replacement and checked for unchanged
page content streams, page boxes, rotation, resource names, annotation counts,
note data, subtype, color, and geometry. Unsupported annotations such as links,
drawings, and stamps are preserved. The machine-readable `/PTAType` marker is
also verified before replacement. The standalone command refuses encrypted
or signed PDFs. It also refuses to overwrite PDF-side edits made after the last
complete reconciliation: run `pta-import`, inspect the Markdown changes, and
then embed. If a range becomes stale, use the confirmed rematch workflow;
applying a PDF rematch regenerates that record's geometry automatically.

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
| `m`, then `L` | Reload source and annotation files from disk |
| `m`, then `P` | Review and confirm a moved or renamed source binding |
| Escape or Ctrl+`[` in Edit mode | Return to Normal mode |
| `u` in Normal mode | Undo the most recent edit batch |
| Ctrl+R in Normal mode | Redo the most recently undone batch |
| `/` | Search within the focused pane |
| `n` / `N` | Repeat the pane's search forward / backward |
| Ctrl+N / Ctrl+P | Jump to the next / previous annotation |
| `m` | Open the annotation action menu for types, filtering, deletion, and tools |
| `R` | Review PDF annotation conflicts, then scan stale text ranges |
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
the source and annotation panes visible. **Add note** is focused by
default. The menu provides these actions:

- **Add note** opens the note prompt for the current source selection,
  with the same validation as the `a` key.
- **Add highlight** saves the current source selection without requiring a note
  body.
- **Add bookmark** records the source cursor position and a minimal text anchor;
  it does not require a visual selection or note body.
- **Filter annotations** chooses All, Notes, Highlights, or Bookmarks. Filtering
  changes only the right-pane projection; it never rewrites the sidecar. Search
  and Ctrl+N/Ctrl+P operate on the visible type.
- **Edit current annotation** opens a structured form for changing that record's
  type and optional Markdown body while retaining its stable ID and anchor.
- **Enter Edit mode** acts on the pane from which the menu was opened. Source
  editing still requires the literal `Yes` warning confirmation; annotation
  editing starts immediately.
- **Switch Sync mode** cycles Normal, Sync, and Off alignment just like `S`.
- **Delete current annotation** asks for confirmation, atomically removes the
  selected sidecar block, renumbers the remaining display headings, and leaves
  the source unchanged. Save or discard unrelated edits before deleting.
- **Rematch annotations** opens the same review-first workflow as `R`.
- **Show keybindings** opens a scrollable in-application reference.
- **Synchronize annotations into source PDF** manually performs the same
  verified, atomic Markdown-to-PDF reconciliation. It is available only for PDF
  sources and remains available when `--no-embed-on-save` disables automatic
  synchronization.
- **Reload files from disk** rereads both the source and annotation sidecar. If
  a PTA buffer is modified, the menu first offers to cancel, discard it by
  reloading, or preserve it as a conflict copy before reloading.
- **Rebind source path** is enabled when the loaded managed sidecar records a
  different source. It opens the same explicit review shown at startup.

In Normal mode, Ctrl+N and Ctrl+P move through visible annotations and stop at the last
or first annotation instead of wrapping. In the source pane, navigation follows
source-range order; in the annotation pane, it follows sidecar order. Cross-pane
movement continues to respect the current Normal, Sync, or Off alignment mode.

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

## External edits and safe saving

PTA records an exact SHA-256 fingerprint of the source and annotation file bytes
when each file is loaded. Before Ctrl+S, quit-time Save, structured annotation
creation/editing/deletion, or manual PDF synchronization can replace a tracked
file, PTA rereads the disk bytes and compares them with that baseline. The
atomic writer checks the fingerprint again immediately before replacement. A
change made by another editor is therefore never overwritten silently, even if
the visible text would normalize to the same newlines.

PTA distinguishes four states for each pane: unchanged, memory-only change,
disk-only change, and changes on both sides. A detected disk change opens a
compact panel while both text panes remain visible:

- **Cancel** leaves the disk and PTA buffers untouched.
- **Reload disk** accepts the external file. For a both-sides conflict, this
  discards that pane's unsaved PTA buffer.
- **Conflict copy + reload** first writes the PTA buffer beside the original as
  `name.pta-conflict-YYYYMMDDTHHMMSSZ.ext`, without replacing either the
  external file or an older conflict copy, and then reloads the external file.

Press `m`, then `L` at any time for an explicit reload. With clean buffers it
reloads immediately; with unsaved buffers it presents the same preservation
choices. PTA intentionally uses this explicit command rather than background
file watching, so a partially written external file is never adopted without a
user action.

### Move or rename a source

Managed annotation files store `source` as a path relative to the sidecar when
the platform permits it. Moving the source and sidecar together while keeping
their relative layout therefore needs no repair. If the source was renamed, or
the two files were moved separately, open the new source with the existing
sidecar explicitly:

```bash
pta-view --annotations path/to/existing.annotations.md path/to/new-source.md
```

`--annotations` selects a non-standard sidecar for that launch only. Rebinding
is deliberately one-way: it records "this sidecar belongs to this source" in
the sidecar's `source` field, but it does not store a reverse association that
would let the source find that sidecar later. PTA does not scan for alternative
sidecar names. On the next launch, `pta-view path/to/new-source.md` again looks
only for the standard adjacent file
`new-source.md.annotations.md`; reuse of any differently named or differently
located sidecar requires `--annotations` again.

Accordingly, **Rebind source path** is not a sidecar picker. It is enabled only
after a managed sidecar has already been loaded and its recorded `source` path
does not resolve to the currently open source. It is gray when the loaded pair
already matches, after a successful confirmation, or when PTA opened the
standard default instead of the intended non-standard sidecar. To avoid passing
`--annotations` on future launches, keep the sidecar beside the source with the
exact standard name `<source filename>.annotations.md`; after relocating it,
confirm a path mismatch once if PTA requests it.

When the recorded and current resolved paths differ, PTA leaves both documents
visible and opens a compact confirmation panel. It shows both paths and an
approximate compatibility percentage based on saved whole-document hashes,
exact recorded ranges, and quotations still found in the current source. Press
`R` in that panel to confirm, or Escape to cancel. The same panel can be reopened
with `m`, then `P`.

Confirmation atomically rewrites only the front matter `source` value. It does
not change a range, quotation, note, PDF geometry anchor, or source byte. Until
confirmation, editing, saving, annotation creation/deletion, rematching, and PDF
synchronization are blocked; navigation, search, copy, and explicit reload stay
available. If the source content also changed, confirm the path first and then
press `R` in the normal interface to review range rematches separately. A disk
change detected during confirmation stops the rewrite and enters the normal
external-change review instead of overwriting it.

Before an edited annotation sidecar is saved or reloaded, every reserved PTA
block is validated for balanced markers, stable identity, unique IDs, supported
type, valid range metadata, and required quotation/note sections. Ordinary
Markdown outside PTA blocks remains unrestricted. If validation fails, the
existing disk file is retained byte-for-byte and the error identifies the
malformed record; repair it externally or preserve the in-memory text as a
conflict copy.

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

For a PDF source, record-level conflicts between the PDF annotation layer and
the Markdown sidecar open automatically after both panes appear. If that panel
was canceled, `R` checks again before ordinary text rematching. The compact
conflict panel leaves both panes visible and previews the affected quotation
and note. Every conflict requires an explicit decision; none is preselected:

PDF reconciliation, annotation-range scanning, and confirmed conflict
application run off the UI thread. A compact animated **Processing** overlay
blocks duplicate input while they run and disappears automatically on success
or failure, so a slow PDF operation is not mistaken for a frozen interface.

- If an annotation was deleted from the PDF but its Markdown record changed,
  choose **Restore PDF** to write the Markdown annotation back, or **Delete MD**
  to accept the PDF deletion and remove the sidecar record.
- If a synchronized Markdown record was deleted externally but its PDF markup
  remains, choose **Delete PDF** to accept the Markdown deletion, or **Restore
  MD** to reconstruct the record from the PDF.
- If the same annotation changed on both sides, choose **Use Markdown** to write
  that version into the PDF, or **Use PDF** to replace the sidecar record.

Use `J`/`K` to navigate, `M` to keep Markdown, `P` to keep the PDF, and Ctrl+S
to continue after every item has a decision. A second compact panel shows each
chosen effect before **Apply**. Applying rechecks both fingerprints, creates a
timestamped `before-pdf-conflict` sidecar backup, reconciles the chosen records,
and rolls both files back if PDF synchronization fails. Cancel and Review are
read-only. After resolving PDF conflicts, press `R` again to perform ordinary
text-range rematching if needed.

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
using stale coordinates. Ordinary text-range rematching never changes the
source file; the separately confirmed **Restore PDF** or **Use Markdown** PDF
conflict decisions intentionally update the PDF annotation layer.

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

## Annotation types and filtering

Pure Text Annotator stores `type: note`, `type: highlight`, or `type: bookmark`
in every structured block. Notes and highlights use the exact selected
half-open range. A newly created bookmark stores an explicit `position` plus a
one-character range anchor, quotation, hashes, and bounded context so that the normal
rematcher can relocate it after edits. At end-of-file, its saved position may
equal the anchor's end.

Press `m` to add any type. The direct `a` key remains the fast path for a note.
The same menu opens the type filter. The status line shows `FILTER ALL`,
`FILTER NOTE`, `FILTER HIGHLIGHT`, or `FILTER BOOKMARK`. Entering annotation
Edit mode temporarily reveals the complete canonical sidecar; leaving Edit mode
reapplies the filter. Same-range annotations retain independent stable IDs, and
overlapping annotations remain separate records and navigation stops.

For text and Markdown, all three types are sidecar-native. For a text-layer PDF,
all three retain canonical text and geometry anchors and synchronize as standard
PDF text markup; a bookmark is represented by its minimal character anchor.
PTA type markers take priority on import, typed subjects provide a portable
fallback, and only untyped markup uses the legacy rule where an external mark
without a comment becomes a highlight and one with a comment becomes a note.

## Add a note

1. Press `v` and move the cursor to select source text.
2. Press `a` to open the note dialog.
3. Write a Markdown note.
4. Press Ctrl+S or Ctrl+Enter to save, or Escape to cancel.

The right pane mirrors the complete annotation file. For a source named
`chapter.md`, annotations are saved beside it as `chapter.md.annotations.md`; for
`paper.pdf`, the sidecar is `paper.pdf.annotations.md`. Each entry contains a
stable identifier, type, creation time, the original quotation, optional body, and a
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
- PDF source viewing, geometry-aware annotation, and verified in-place markup
  synchronization;
  no OCR or rendered-page pane
- Bidirectional reconciliation of standard PDF text markup with provenance,
  edit/delete propagation, and geometry confidence reporting
- Exact-hash PDF extraction caching for faster repeat opens
- Exact-source cursor restoration across viewer sessions
- Independent Vim-style jump histories for the source and annotation panes
- Automatic source-PDF annotation synchronization after annotation saves
- Guarded source editing and direct annotation editing with dirty tracking
- Line numbers in both panes and optional wrapping
- Persistent Markdown sidecar annotations with atomic saving
- Note, highlight, and bookmark records with stable IDs; original quotation,
  source range, hashes, and bounded context
- Review-first deterministic rematching with confidence estimates, backup,
  validation, and rollback
- Structured deletion of the current annotation with confirmation
- Literal search with forward/backward repetition in both panes
- Non-destructive in-view type filtering for All, Note, Highlight, and Bookmark
- No semantic/AI matching; rematching remains local and deterministic

## Test

```bash
.venv/bin/python -m pytest
```

