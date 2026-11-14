---
format: pure-text-annotator
version: 1
source: "types.md"
range_unit: unicode-code-point
range_convention: zero-based-half-open
---

# Annotations

<!-- pta:annotation id="demo-note" -->
## Annotation 1

```yaml
id: "demo-note"
type: note
range:
  start: 53
  end: 68
created_at: "2026-11-13T12:00:00Z"
anchor:
  source_sha256: "02dbc5d64d08d4cace4622eabbb1aa11e0e65d57bb46169a3bd96dbf3f72575b"
  quote_sha256: "7136cd9a8103b37fab854d4c4c1cadfcb43c350466fd0b3f95859e7ccff6ed92"
  prefix: "# Annotation Type Demo\n\nThis sentence demonstrates a "
  suffix: ".\n\nThis phrase is highlighted without a note body.\n\nA bookmark is attached to th"
  original_start: 53
  original_end: 68
```

### Original text

> note annotation

### Note

Explain why this sentence matters.

<!-- /pta:annotation -->

<!-- pta:annotation id="demo-highlight" -->
## Annotation 2

```yaml
id: "demo-highlight"
type: highlight
range:
  start: 86
  end: 117
created_at: "2026-11-13T12:00:00Z"
anchor:
  source_sha256: "02dbc5d64d08d4cace4622eabbb1aa11e0e65d57bb46169a3bd96dbf3f72575b"
  quote_sha256: "553c6d2cad315fb9be74b4edec3a38337e9913e421a8ec1469988f140d214f62"
  prefix: "tation Type Demo\n\nThis sentence demonstrates a note annotation.\n\nThis phrase is "
  suffix: ".\n\nA bookmark is attached to the word return near this line.\n\nOverlapping ranges"
  original_start: 86
  original_end: 117
```

### Original text

> highlighted without a note body

### Note



<!-- /pta:annotation -->

<!-- pta:annotation id="demo-bookmark" -->
## Annotation 3

```yaml
id: "demo-bookmark"
type: bookmark
position: 155
range:
  start: 155
  end: 156
created_at: "2026-11-13T12:00:00Z"
anchor:
  source_sha256: "02dbc5d64d08d4cace4622eabbb1aa11e0e65d57bb46169a3bd96dbf3f72575b"
  quote_sha256: "454349e422f05297191ead13e21d3db520e5abef52055e4964b82fb213f593a1"
  prefix: " phrase is highlighted without a note body.\n\nA bookmark is attached to the word "
  suffix: "eturn near this line.\n\nOverlapping ranges can coexist, and the same range can ho"
  original_start: 155
  original_end: 156
```

### Original text

> r

### Note


<!-- /pta:annotation -->
