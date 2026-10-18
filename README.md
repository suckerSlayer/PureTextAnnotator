# Pure Text Annotator

The first project milestone is a read-only terminal viewer for UTF-8 text and
Markdown files. It displays Markdown source as plain text and provides
Vim-style navigation, keyboard text selection, mouse selection, and clipboard
copying. Annotation storage is intentionally not included yet.

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
| Shift+arrow keys | Extend a selection |
| Mouse drag | Select text |
| `y`, Ctrl+C, or Cmd+C | Copy selected text |
| Escape | Clear the selection |
| `q` or Ctrl+Q | Quit |

The viewer uses Textual's terminal clipboard support and also invokes the
native clipboard utility on macOS, Windows, Wayland, or X11 when one is
available.

## Current scope

- One file per application launch
- `.txt`, `.md`, and `.markdown` files
- UTF-8 input, including files with a UTF-8 byte-order mark
- Read-only display with line numbers and optional wrapping
- No annotation, editing, search, or persistence features

## Test

```bash
.venv/bin/python -m pytest
```

