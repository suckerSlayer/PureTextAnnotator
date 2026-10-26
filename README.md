# Pure Text Annotator

Pure Text Annotator is a keyboard-first terminal interface for UTF-8 text and
Markdown files. It displays Markdown source as plain text, provides Vim-style
navigation and selection, and saves notes in a human-readable Markdown sidecar
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
| `w` `b` | Move by word |
| `0` `$` or Home/End | Move to line start/end |
| `g` `G` | Move to document start/end |
| Ctrl+F / Ctrl+B or Page Down / Page Up | Move one page |
| `v` | Start or finish visual selection |
| `a` | Add a note for the selected source text |
| Tab / Shift+Tab | Switch between source and annotation panes |
| Ctrl+W, then `w` | Switch between source and annotation panes |
| Shift+arrow keys | Extend a selection |
| Mouse drag | Select text |
| `y`, Ctrl+C, or Cmd+C | Copy selected text |
| Escape | Clear the selection |
| `q` or Ctrl+Q | Quit |

The viewer uses Textual's terminal clipboard support and also invokes the
native clipboard utility on macOS, Windows, Wayland, or X11 when one is
available.

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
exact-distance tie keeps the current annotation focused. Moving through
annotation entries with the same Vim/page keys returns the source pane to the
corresponding recorded range.

The focused annotation is visually distinct in the right pane: its
`## Annotation N` heading uses the normal text color, its original quotation has
a separate highlight color, and only its note body receives the active-note
highlight. Metadata is muted, and complete non-focused annotation blocks are
dimmed so the current note remains easy to identify.

## Current scope

- One file per application launch
- `.txt`, `.md`, and `.markdown` files
- UTF-8 input, including files with a UTF-8 byte-order mark
- Read-only display with line numbers and optional wrapping
- Persistent Markdown sidecar annotations with atomic saving
- Original quotation and source range recorded for every note
- No annotation editing/deletion, source-change recovery, or search yet

## Test

```bash
.venv/bin/python -m pytest
```

