---
format: pure-text-annotator
version: 1
source: "cases.md"
range_unit: unicode-code-point
range_convention: zero-based-half-open
---

# Annotations

<!-- pta:annotation id="case-shifted" -->
## Annotation 1

```yaml
id: "case-shifted"
type: note
range:
  start: 253
  end: 308
created_at: "2026-11-01T10:00:00Z"
anchor:
  source_sha256: "f67131edf88fd8c1ff84a09b79676ed58dcbef562528cb5df2e4e8a54c9933cf"
  quote_sha256: "1167d47f56803c62c6e8844d550ac9377709f936c444859370c11f88ced6d7b9"
  prefix: "vision.\n\n## Case 1 — shifted range\n\nThe brass clock starts the observation log.\n"
  suffix: "\nThe night crew files the completed report.\n\n## Case 2 — lightly edited quotatio"
  original_start: 253
  original_end: 308
```

### Original text

> The observatory opens at dawn for careful measurements.

### Note

Expected: a unique exact match at a shifted range, with a high estimate.

<!-- /pta:annotation -->

<!-- pta:annotation id="case-edited" -->
## Annotation 2

```yaml
id: "case-edited"
type: note
range:
  start: 442
  end: 482
created_at: "2026-11-01T10:01:00Z"
anchor:
  source_sha256: "f67131edf88fd8c1ff84a09b79676ed58dcbef562528cb5df2e4e8a54c9933cf"
  quote_sha256: "3a6f7a58446b86df32fe51f6e4a42bb37e8a3066095756dc3e1c0996c5fc5c6a"
  prefix: " — lightly edited quotation\n\nBefore the archive sentence, the hallway is quiet.\n"
  suffix: "\nAfter the archive sentence, the lamps are checked.\n\n## Case 3 — duplicated quot"
  original_start: 442
  original_end: 482
```

### Original text

> The archive closes every evening at six.

### Note

Expected: a fuzzy proposal for the lightly edited archive sentence.

<!-- /pta:annotation -->

<!-- pta:annotation id="case-duplicate" -->
## Annotation 3

```yaml
id: "case-duplicate"
type: note
range:
  start: 724
  end: 744
created_at: "2026-11-01T10:02:00Z"
anchor:
  source_sha256: "f67131edf88fd8c1ff84a09b79676ed58dcbef562528cb5df2e4e8a54c9933cf"
  quote_sha256: "5858076d32a50b083c272949f9b90bf6512f2282ff16ed6d76967cb9de9d1a6e"
  prefix: "ha section ends beside the eastern gate.\n\nBeta section begins with a blue card.\n"
  suffix: "\nBeta section ends beside the western gate.\n\n## Case 4 — deleted passage\n\nA plai"
  original_start: 724
  original_end: 744
```

### Original text

> The signal is clear.

### Note

Expected: saved Beta context should distinguish this from the identical Alpha sentence.

<!-- /pta:annotation -->

<!-- pta:annotation id="case-orphaned" -->
## Annotation 4

```yaml
id: "case-orphaned"
type: note
range:
  start: 864
  end: 936
created_at: "2026-11-01T10:03:00Z"
anchor:
  source_sha256: "f67131edf88fd8c1ff84a09b79676ed58dcbef562528cb5df2e4e8a54c9933cf"
  quote_sha256: "c0b8780ac2622b3f9b741e354ee1058d4eee26c6c7a21ff4de270361c3a274f2"
  prefix: "te.\n\n## Case 4 — deleted passage\n\nA plain sentence appears before the deletion.\n"
  suffix: "\nA plain sentence appears after the deletion.\n"
  original_start: 864
  original_end: 936
```

### Original text

> A singular cobalt paragraph will be removed from this document entirely.

### Note

Expected: no safe candidate; leave this annotation unresolved.

<!-- /pta:annotation -->
