# Pure Text Annotator

Pure Text Annotator is a keyboard-first terminal interface for UTF-8 text and
Markdown files. It displays Markdown source as plain text, provides Vim-style
navigation, selection, and guarded editing, and saves notes in a human-readable
Markdown sidecar shown beside the source.

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
```

You can also run the package directly:

```bash
python -m pure_text_annotator path/to/file.md
```

Use `--no-wrap` to keep long lines unwrapped.

## Controls

| Keys | Action |
| --- | --- |
| `h` `j` `k` `l` or arrow keys | Move the cursor |
| `w` | Move to the start of the next Vim word |
| `b` | Move to the previous word |
| `e` | Move to the end of the word |
| `0` `$` or Home/End | Move to line start/end |
| `g` `G` | Move to document start/end |
| Ctrl+F / Ctrl+B or Page Down / Page Up | Move one page |
| Ctrl+E / Ctrl+Y | Scroll down/up by one line |
| `zt` / `zz` / `zb` | Place the cursor line at the top/middle/bottom |
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
| `R` | Scan stale annotations and open rematch review |
| `S` | Cycle alignment mode: Normal → Sync → Off |
| `.` | Manually align and center both panes' cursor lines |
| `zi` in annotations | Toggle metadata folding on/off |
| `zo` / `zc` in annotations | Open/close the metadata fold at the cursor |
| Tab / Shift+Tab | Switch between source and annotation panes |
| Ctrl+W, then `w` | Switch between source and annotation panes |
| Shift+arrow keys | Extend a selection |
| Mouse drag | Select text |
| `y`, Ctrl+C, or Cmd+C | Copy selected text |
| Escape | Clear the selection |
| `q` or Ctrl+Q | Quit; prompt if either pane has unsaved changes |

The `w`, `b`, and `e` motions are available in both panes and follow Vim's
lowercase-word boundaries: letters, numbers, and underscores form one kind of
word, while adjacent non-blank symbols form another. Whitespace is skipped.

Both panes provide trailing scroll space equal to half their visible height.
This lets the final document line move to the middle of its pane with `zz` or
manual alignment; the blank space is visual only and is never saved to either
file.

## Edit text

With the source pane focused, press `i` to request Edit mode. Because this mode
changes the original file, a warning dialog opens every time. Type the exact
case-sensitive text `Yes` and press Enter (or select **Edit original**) to proceed;
any other input is rejected. Escape cancels the warning.

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

Ctrl+S from review opens the only rematch Apply confirmation. It reports the
average and lowest selected estimates plus ambiguous and unresolved counts.
Cancel and Review again do not write anything. **Apply rematch** rechecks source
and sidecar fingerprints, creates a timestamped `before-rematch` backup, updates
only the selected sidecar ranges and quoted text, records rematch history,
atomically writes, reparses, and rolls back on failed validation. The source file
is never changed by rematching.

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
preserves the state of the others.

Folding changes only the annotation pane's visual projection. Editing, saving,
undo/redo, rematching, alignment, and dirty tracking continue to use the complete
sidecar. Entering Edit mode temporarily unfolds the full sidecar; returning to
Normal mode restores the selected folds.

## Add an annotation

1. Press `v` and move the cursor to select source text.
2. Press `a` to open the note dialog.
3. Write a Markdown note.
4. Press Ctrl+S or Ctrl+Enter to save, or Escape to cancel.

The right pane mirrors the complete annotation file. For a source named
`chapter.md`, notes are saved beside it as `chapter.md.annotations.md`. Each
entry contains a stable identifier, creation time, the original quotation, the
note, and a zero-based half-open source range measured in Unicode code points.
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
- `.txt`, `.md`, and `.markdown` files
- UTF-8 input, including files with a UTF-8 byte-order mark
- Guarded source editing and direct annotation editing with dirty tracking
- Line numbers in both panes and optional wrapping
- Persistent Markdown sidecar annotations with atomic saving
- Original quotation, source range, hashes, and bounded context recorded for
  new notes
- Review-first deterministic rematching with confidence estimates, backup,
  validation, and rollback
- Literal search with forward/backward repetition in both panes
- No structured annotation deletion or semantic/AI matching yet

## Test

```bash
.venv/bin/python -m pytest
```

