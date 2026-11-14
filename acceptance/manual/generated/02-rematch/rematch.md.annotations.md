---
format: pure-text-annotator
version: 1
source: "rematch.md"
range_unit: unicode-code-point
range_convention: zero-based-half-open
---

# Annotations

<!-- pta:annotation id="manual-note" -->
## Annotation 1

```yaml
id: "manual-note"
type: note
range:
  start: 77
  end: 96
created_at: "2026-11-14T00:00:00Z"
anchor:
  source_sha256: "8eb6d8fbe08f4f75d067f47299ab4a5824a1f918afa25b856953582105b781af"
  quote_sha256: "40d7130aa570982e4e105d8f5179caadca9f32d951410f9e9b13c2de78a6e6e7"
  prefix: "人工验收插入行：三种锚点都应向后移动。\n# 三类型人工验收：重匹配场景\n\n在执行步骤 B-03 前，不要修改这一行以上的内容。\n\n## Note 锚点\n\n"
  suffix: "\n\n## Highlight 锚点\n\n黄色高亮用于标记重要内容，不要求填写说明。\n\n## Bookmark 锚点\n\n书签目标位于这一行的“返回”二字，便于检查精"
  original_start: 77
  original_end: 96
rematch_history:
  - rematched_at: "2026-11-14T01:16:00Z"
    previous_start: 57
    previous_end: 76
    method: "exact"
    estimated_correctness: 100
```

### Original text

> 组合锚点让批注与原文保持可审查的联系。

### Note

人工验收预置批注：重匹配后正文和稳定 ID 都应保留。

<!-- /pta:annotation -->

<!-- pta:annotation id="manual-highlight" -->
## Annotation 2

```yaml
id: "manual-highlight"
type: highlight
range:
  start: 115
  end: 136
created_at: "2026-11-14T00:00:00Z"
anchor:
  source_sha256: "8eb6d8fbe08f4f75d067f47299ab4a5824a1f918afa25b856953582105b781af"
  quote_sha256: "f5755c0caefbe7df809a747a62767cf161aec1c6e5d7a3fb5231358096a27a77"
  prefix: "\n\n在执行步骤 B-03 前，不要修改这一行以上的内容。\n\n## Note 锚点\n\n组合锚点让批注与原文保持可审查的联系。\n\n## Highlight 锚点\n\n"
  suffix: "\n\n## Bookmark 锚点\n\n书签目标位于这一行的“返回”二字，便于检查精确光标位置。\n\n## 末尾检查\n\n三条预置记录应分别显示为 Note、Highl"
  original_start: 115
  original_end: 136
rematch_history:
  - rematched_at: "2026-11-14T01:16:00Z"
    previous_start: 95
    previous_end: 116
    method: "exact"
    estimated_correctness: 100
```

### Original text

> 黄色高亮用于标记重要内容，不要求填写说明。

### Note



<!-- /pta:annotation -->

<!-- pta:annotation id="manual-bookmark" -->
## Annotation 3

```yaml
id: "manual-bookmark"
type: bookmark
position: 165
range:
  start: 165
  end: 166
created_at: "2026-11-14T00:00:00Z"
anchor:
  source_sha256: "8eb6d8fbe08f4f75d067f47299ab4a5824a1f918afa25b856953582105b781af"
  quote_sha256: "954154be936283672faa45cbaada101ad324415e2e45f0a426b9f994ad200fa1"
  prefix: "原文保持可审查的联系。\n\n## Highlight 锚点\n\n黄色高亮用于标记重要内容，不要求填写说明。\n\n## Bookmark 锚点\n\n书签目标位于这一行的“"
  suffix: "回”二字，便于检查精确光标位置。\n\n## 末尾检查\n\n三条预置记录应分别显示为 Note、Highlight 和 Bookmark。\n"
  original_start: 165
  original_end: 166
rematch_history:
  - rematched_at: "2026-11-14T01:16:00Z"
    previous_start: 145
    previous_end: 146
    method: "exact"
    estimated_correctness: 80
```

### Original text

> 返

### Note



<!-- /pta:annotation -->
