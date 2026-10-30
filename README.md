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
| Escape or Ctrl+`[` in Edit mode | Return to Normal mode |
| `u` in Normal mode | Undo the most recent edit batch |
| `/` | Search within the focused pane |
| `n` / `N` | Repeat the pane's search forward / backward |
| `S` | Cycle alignment mode: Normal → Sync → Off |
| `.` | Manually align the other pane from the focused pane |
| Tab / Shift+Tab | Switch between source and annotation panes |
| Ctrl+W, then `w` | Switch between source and annotation panes |
| Shift+arrow keys | Extend a selection |
| Mouse drag | Select text |
| `y`, Ctrl+C, or Cmd+C | Copy selected text |
| Escape | Clear the selection |
| `q` or Ctrl+Q | Quit |

The `w` and `e` motions are available in both panes and follow Vim's
lowercase-word boundaries: letters, numbers, and underscores form one kind of
word, while adjacent non-blank symbols form another. Whitespace is skipped.

## Edit text

With the source pane focused, press `i` to request Edit mode. Because this mode
changes the original file, a warning dialog opens every time. Type the exact
case-sensitive text `Yes` and press Enter (or select **Edit original**) to proceed;
any other input is rejected. Escape cancels the warning.

With the annotation pane focused, press `i` to enter Edit mode immediately; no
warning or confirmation is shown. Changes are saved atomically to the complete
Markdown sidecar, then reparsed so recorded ranges and source highlights reflect
the edited annotation document.

In either pane's Edit mode, printable keys and Enter edit at the cursor. Ctrl+W
deletes the previous word, Ctrl+H deletes the previous character, and Escape or
Ctrl+`[` returns to Normal mode. Ctrl+Q remains available to quit. Source changes
are saved atomically to the original file while preserving its UTF-8 BOM, newline
style, and permissions.

In Normal mode, `u` reverts the most recent edit batch in the focused pane and
atomically saves the reverted source or annotation file. Separate Edit-mode
sessions create separate undo steps.

Source edits do not rewrite existing annotation ranges or quoted original text.
Those annotations therefore continue to refer to their previously recorded
Unicode offsets until source-change recovery is implemented.

The viewer uses Textual's terminal clipboard support and also invokes the
native clipboard utility on macOS, Windows, Wayland, or X11 when one is
available.

Search is literal and case-sensitive. Press `/` in either pane, enter the text,
and press Enter. The current match is highlighted and centered; `n` and `N`
continue through matches with wraparound. Escape closes the search prompt
without moving the cursor. Each pane remembers its own most recent query.

## Alignment modes

The status line shows the current alignment mode. Press `S` to cycle through:

| Mode | Automatic alignment |
| --- | --- |
| Normal (default) | Source movement aligns the annotation pane; annotation movement stays local. |
| Sync | Movement in either pane aligns the other pane. |
| Off | Movement in either pane stays local. |

In every mode, press `.` to align manually from the focused pane: from the
source it centers the corresponding annotation on `### Note`; from an annotation
it centers the source on that annotation's recorded range.

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
- Guarded source editing and direct annotation editing with atomic saving
- Line numbers and optional wrapping
- Persistent Markdown sidecar annotations with atomic saving
- Original quotation and source range recorded for every note
- Literal search with forward/backward repetition in both panes
- No annotation editing/deletion or automatic source-change recovery yet

## Test

```bash
.venv/bin/python -m pytest
```

