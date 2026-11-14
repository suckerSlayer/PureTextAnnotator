# Annotation type format example

Run:

```bash
pta-view examples/annotation_types/types.md
```

The checked-in sidecar is a complete version-1 format example with one Note,
one body-less Highlight, and one cursor-position Bookmark. Its ranges, quoted
text, source hash, quotation hashes, and context anchors match `types.md`.

Press Ctrl+N / Ctrl+P to visit the records. Press `m`, choose **Filter
annotations**, and inspect All, Notes, Highlights, and Bookmarks.

With one entry focused, press `m` and choose **Edit current annotation** to
change its type or Markdown body without editing the full sidecar.

To add another type, select source text and use `m` → **Add note** or **Add
highlight**. Move the source cursor without selecting and use `m` → **Add
bookmark**.

Deleting or editing this sample changes the format reference. For a disposable
exercise, copy both `types.md` and `types.md.annotations.md` to a temporary
directory first. See the repository's [user manual](../../USER_MANUAL.md) for
workflows and the
[format specification](../../ANNOTATION_FORMAT_SPEC.md) for every field and
compatibility rule.
