# Annotation type demo

Run:

```bash
pta-view examples/annotation_types/types.md
```

The sidecar starts with one note, one body-less highlight, and one cursor-position
bookmark. Press `Ctrl-N` / `Ctrl-P` to visit them. Press `m`, then `f`, and choose
All, Notes, Highlights, or Bookmarks with `a`, `n`, `h`, or `b`.

With one entry focused, press `m`, then `x`, to change its type or Markdown body
without editing the full sidecar.

To add another type, select source text and use `m` → **Add note** or **Add
highlight**. Move the source cursor without selecting and use `m` → **Add
bookmark**. Deleting or editing this sample changes its checked-in sidecar, so
copy the directory first if you want a disposable exercise.
