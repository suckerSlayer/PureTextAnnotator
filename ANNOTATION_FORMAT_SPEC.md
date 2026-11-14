# Pure Text Annotator 标注文件格式规范

规范版本：1.0
对应软件版本：Pure Text Annotator 0.11.0
状态：正式项目规范

## 1. 范围

本文定义 Pure Text Annotator（以下简称 PTA）外置 Markdown 标注文件的版本 1 格式，包括：

- 文件发现、命名和原文绑定；
- UTF-8 编码及规范化文本；
- `note`、`highlight`、`bookmark` 三种标注类型；
- Unicode 码点范围、原文引用、上下文锚点和重匹配历史；
- PDF 几何锚点、导入来源和双向同步状态；
- 外部编辑、验证、兼容和安全写入要求。

本文中的“必须”“不得”表示格式合规要求；“应当”表示强烈建议；“可以”表示可选行为。普通 Markdown 编辑器不需要理解 PTA 元数据，也可以阅读标注文件。

## 2. 术语

| 术语 | 定义 |
| --- | --- |
| 原文 | 被阅读和标注的 `.txt`、`.md`、`.markdown` 或可提取文字的 `.pdf` 文件 |
| 侧车 | 保存标注的相邻 Markdown 文件，默认名称为 `<原文件名>.annotations.md` |
| 规范文本 | PTA 实际显示、计算范围和哈希的 Unicode 字符串 |
| 记录 | 一对 PTA 起止注释标记包围的单条标注 |
| 范围 | 相对规范文本的零基、左闭右开区间 `[start, end)` |
| 稳定 ID | 标注在编辑、重匹配和 PDF 同步期间保持不变的唯一字符串 |
| 锚点 | 用于确认或恢复标注位置的引文、哈希、上下文和可选 PDF 几何信息 |

## 3. 文件发现与命名

### 3.1 默认侧车

默认侧车必须与原文位于同一目录，并在完整原文件名后追加 `.annotations.md`：

| 原文 | 默认侧车 |
| --- | --- |
| `chapter.md` | `chapter.md.annotations.md` |
| `notes.txt` | `notes.txt.annotations.md` |
| `paper.pdf` | `paper.pdf.annotations.md` |

程序使用 `pta-view 原文` 启动时只自动查找这个名称，不扫描其他目录或其他文件名。

### 3.2 显式侧车

非默认侧车通过以下方式仅对本次启动指定：

```console
pta-view --annotations path/to/notes.annotations.md path/to/source.md
```

该参数不是持久化的反向索引。若侧车仍使用非默认名称或位置，下次启动仍须再次传入 `--annotations`。

### 3.3 原文路径绑定

规范侧车在 YAML front matter 中保存一个 `source` 字段。PTA 应优先写入相对于侧车目录的 POSIX 风格路径；不同 Windows 驱动器等无法表示为相对路径的情况可以写入绝对路径。

移动原文和侧车时：

- 若二者相对位置不变，绑定继续有效；
- 若原文单独移动或改名，应使用 `--annotations` 打开并经过 Rebind 确认；
- Rebind 只能修改 `source`，不得静默修改范围、引文、正文或 PDF 几何锚点。

## 4. 规范文本与范围单位

### 4.1 文本和 Markdown

PTA 按以下顺序建立规范文本：

1. 读取原文件字节；
2. 使用 `utf-8-sig` 解码，因此可接受 UTF-8 BOM，但 BOM 不进入文本；
3. 将 CRLF 和单独 CR 统一为 LF（U+000A）；
4. 不执行 NFC、NFD、大小写或空白规范化。

源文件保存时可以恢复原有 BOM 和换行风格，但持久化范围始终针对上述规范文本。

### 4.2 PDF

PDF 的规范文本是 PTA 文本层提取器产生的确定性投影。每页包含可见的页标题及按提取规则排序的文字。范围针对该投影，而不是 PDF 内容流字节、字形编号或屏幕像素。

### 4.3 Unicode 码点

`start`、`end`、`position`、`original_start` 和 `original_end` 均以 Unicode 码点计数：

- 第一个码点的偏移是 `0`；
- 换行 LF 占一个码点；
- 一个组合字符序列可能包含多个码点；
- UTF-8 多字节字符仍只按其 Unicode 码点数计数；
- 范围必须满足 `0 <= start < end <= len(规范文本)`。

范围是左闭右开区间，引用内容必须满足：

```text
quote == canonical_text[start:end]
```

## 5. 文件编码与文档外壳

### 5.1 编码

侧车必须是有效 UTF-8 文本，不得含 NUL 字节。PTA 读取时接受 UTF-8 BOM，规范写出使用无 BOM 的 UTF-8 和 LF 换行。

### 5.2 Front matter

规范版本 1 文件必须以以下 front matter 开始：

```yaml
---
format: pure-text-annotator
version: 1
source: "chapter.md"
range_unit: unicode-code-point
range_convention: zero-based-half-open
---
```

字段定义如下：

| 字段 | 要求 | 类型 | 含义 |
| --- | --- | --- | --- |
| `format` | 必须 | 字符串 | 固定为 `pure-text-annotator` |
| `version` | 必须 | 整数 | 当前固定为 `1` |
| `source` | 必须且只能出现一次 | JSON/YAML 字符串 | 相对侧车目录或绝对原文路径 |
| `range_unit` | 必须 | 字符串 | 固定为 `unicode-code-point` |
| `range_convention` | 必须 | 字符串 | 固定为 `zero-based-half-open` |
| `pdf_sync_document_sha256` | PDF 同步后可选 | 64 位小写十六进制字符串 | 上次完整协调的 PDF 原始字节 SHA-256 |
| `pdf_sync_sidecar_sha256` | PDF 同步后可选 | 64 位小写十六进制字符串 | 排除两个 `pdf_sync_*` front matter 字段后的侧车内容 SHA-256 |

两个 `pdf_sync_*` 字段应成对出现。字段顺序不影响含义，但规范写入器使用上表顺序，并把 PDF 同步字段放在 front matter 末尾。

### 5.3 普通 Markdown

Front matter 之后可以包含任意普通 Markdown。规范写入器创建：

```markdown
# Annotations
```

该标题便于阅读但不是记录身份的一部分。记录之外的 Markdown 应由实现原样保留；它不能使用 PTA 保留标记伪装成记录。

## 6. 记录边界与基本结构

### 6.1 保留标记

每条规范记录必须以如下行开始：

```markdown
<!-- pta:annotation id="stable-id" -->
```

并以如下独立行结束：

```markdown
<!-- /pta:annotation -->
```

要求如下：

- `id` 值使用 JSON 字符串转义；
- 起止标记必须配对，不得嵌套；
- ID 必须非空并在整个侧车内唯一；
- 正文、引文和普通 Markdown 不得包含匹配上述保留语法的独立行。

### 6.2 规范记录模板

````markdown
<!-- pta:annotation id="550e8400-e29b-41d4-a716-446655440000" -->
## Annotation 1

```yaml
id: "550e8400-e29b-41d4-a716-446655440000"
type: note
range:
  start: 10
  end: 18
created_at: "2026-11-14T01:23:45Z"
anchor:
  source_sha256: "<64 hex characters>"
  quote_sha256: "<64 hex characters>"
  prefix: "previous context"
  suffix: "following context"
  original_start: 10
  original_end: 18
```

### Original text

> selected

### Note

Why this passage matters.

<!-- /pta:annotation -->
````

上例使用四个反引号包裹整条 Markdown 记录，以便在其中展示真实的三反引号 `yaml` 元数据块。完整可运行示例见 `examples/annotation_types/`。

### 6.3 标题序号

`## Annotation N` 是面向人的显示标题：

- `N` 从 1 开始，按记录文档顺序递增；
- 删除记录后 PTA 会重新编号；
- 程序身份只能依赖稳定 ID，不得依赖标题序号。

## 7. 核心元数据

记录的 fenced YAML 元数据使用唯一键。规范字符串应使用 JSON 双引号及 JSON 转义，以保证换行、引号和非 ASCII 内容的确定性表示。

| 字段 | Note | Highlight | Bookmark | 类型与约束 |
| --- | --- | --- | --- | --- |
| `id` | 必须 | 必须 | 必须 | 非空唯一字符串，必须与起始标记 ID 相同 |
| `type` | 必须 | 必须 | 必须 | `note`、`highlight`、`bookmark` 之一 |
| `position` | 禁止 | 禁止 | 必须 | 零基 Unicode 光标位置，满足 `start <= position <= end` |
| `range.start` | 必须 | 必须 | 必须 | 非负整数 |
| `range.end` | 必须 | 必须 | 必须 | 大于 `start` 的整数 |
| `created_at` | 必须 | 必须 | 必须 | ISO 8601 字符串；规范写入使用 UTC `Z` |
| `anchor` | 应当 | 应当 | 应当 | 文本恢复锚点，见第 9 节 |
| `pdf_anchor` | PDF 写回必须 | PDF 写回必须 | PDF 写回必须 | PDF 几何锚点，见第 11 节 |
| `pdf_import` | 外部 PDF 导入时可选 | 外部 PDF 导入时可选 | 外部 PDF 导入时可选 | 外部标记来源，见第 11 节 |
| `pdf_sync` | PDF 协调后可选 | PDF 协调后可选 | PDF 协调后可选 | 记录级同步身份，见第 11 节 |
| `rematch_history` | 重匹配后可选 | 重匹配后可选 | 重匹配后可选 | 只追加的审计历史，见第 10 节 |

兼容读取器可以接受缺少 `type`、`created_at` 或新式锚点的旧记录，并把缺少 `type` 的记录视为 `note`；新写入器不得省略本节标为“必须”的字段。

## 8. 三种标注类型

### 8.1 Note

- `type` 必须为 `note`；
- `range` 覆盖用户选择的非空原文；
- `### Note` 必须包含至少一个非空白字符；
- 可以使用多段 Markdown、列表、代码和普通标题。

### 8.2 Highlight

- `type` 必须为 `highlight`；
- `range` 覆盖非空原文；
- `### Note` 可以为空，也可以包含补充说明；
- 空正文不得被自动替换成解释性占位句。

### 8.3 Bookmark

- `type` 必须为 `bookmark`；
- `position` 表示用户光标位置；
- 为支持高亮、搜索和恢复，`range` 仍必须非空，通常是位置附近的一个码点；
- `position` 可以等于 `end`，但不得位于范围之外；
- `### Note` 可以为空，也可以包含说明；
- PDF 中的 Bookmark 使用最小文本标记，不等同于 PDF outline/bookmark 条目。

不同记录可以使用相同范围或重叠范围，只要稳定 ID 不同。实现不得自动合并这些记录。

## 9. 文本锚点

规范 `anchor` 结构如下：

```yaml
anchor:
  source_sha256: "..."
  quote_sha256: "..."
  prefix: "up to 80 code points before the range"
  suffix: "up to 80 code points after the range"
  original_start: 10
  original_end: 18
```

| 字段 | 定义 |
| --- | --- |
| `source_sha256` | 规范文本 UTF-8 编码的 SHA-256；不是文本文件原始磁盘字节哈希 |
| `quote_sha256` | `canonical_text[start:end]` 的 UTF-8 SHA-256 |
| `prefix` | 原范围前最多 80 个 Unicode 码点 |
| `suffix` | 原范围后最多 80 个 Unicode 码点 |
| `original_start` | 创建或最近一次确认重匹配时的起点 |
| `original_end` | 创建或最近一次确认重匹配时的终点 |

SHA-256 使用 64 位小写十六进制。字符串使用 JSON 转义，因此换行表示为 `\n`。重匹配确认后 PTA 用当前规范文本重新生成整个 `anchor`。

## 10. 引文、正文与重匹配历史

### 10.1 Original text

`### Original text` 后必须是非空 Markdown blockquote。每个原文物理行的编码方式是：

- 非空行：`> ` 加原始行内容；
- 空行：单独的 `>`；
- 解码时只移除第一个 `>` 和紧随其后的一个可选空格。

跨行引文示例：

```markdown
### Original text

> first line
>
> third line
```

解析后的引文必须逐码点等于范围中的规范文本。用户不得直接改写引文来掩盖范围失效；应保存原文后使用 `R` 审查重匹配。

### 10.2 Note

`### Note` 后至结束标记前是用户 Markdown 正文。解析时会忽略该区域首尾的空白行，但保留内部内容。正文可以包含类似 `## Annotation 99` 的普通标题；不得包含 PTA 起止保留标记行。

### 10.3 Rematch history

每次确认应用重匹配都会追加：

```yaml
rematch_history:
  - rematched_at: "2026-11-14T02:00:00Z"
    previous_start: 10
    previous_end: 18
    method: "exact"
    estimated_correctness: 100
```

| 字段 | 含义 |
| --- | --- |
| `rematched_at` | 确认应用时间 |
| `previous_start` / `previous_end` | 写入新范围前的旧范围 |
| `method` | 选择候选的方法，例如 `exact`、上下文或人工选择 |
| `estimated_correctness` | 当时显示给用户的 0–100 整数估计值，不是统计保证 |

历史是审计信息，不参与当前范围解释；不得把估算正确率描述为真实概率保证。

## 11. PDF 扩展

PDF 扩展只对文本层 PDF 有意义。文本/Markdown 记录可以完全省略本节字段。

### 11.1 `pdf_anchor`

```yaml
pdf_anchor:
  version: 1
  document_sha256: "<raw PDF byte hash>"
  extractor:
    name: "pdfplumber"
    version: "..."
    profile: "pta-canonical-v1"
  coordinate_system: "pdf-user-space-bottom-left"
  fragments:
    - page_index: 0
      page_label: "1"
      page_text_sha256: "..."
      text_range:
        start: 100
        end: 120
      crop_box: [0.0, 0.0, 612.0, 792.0]
      rotation: 0
      rect_pdf: [72.0, 700.0, 240.0, 716.0]
      quad_points_pdf:
        - [72.0, 716.0, 240.0, 716.0, 72.0, 700.0, 240.0, 700.0]
```

要求如下：

- `version` 当前必须为 `1`；
- `document_sha256` 是 PDF 原始文件字节哈希；
- `coordinate_system` 固定为 `pdf-user-space-bottom-left`；
- `fragments` 必须非空、按 `page_index` 升序排列，每页最多一个片段；
- `page_index` 为零基页索引；`page_label` 可以为字符串或 `null`；
- `text_range` 必须落在记录总范围内；
- `crop_box` 和 `rect_pdf` 是四个数值 `[x0, y0, x1, y1]`；
- `rotation` 只能是 `0`、`90`、`180`、`270`；
- 每个 quad 是八个 PDF 用户空间坐标，列表不得为空。

PDF 任意字节变化都会使 `document_sha256` 失效。只有文本、页面和几何重新验证成功后才能刷新锚点。

### 11.2 `pdf_import`

外部 PDF 程序创建的标准文本标记可以保存以下来源信息：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `version` | 整数 | 当前为 1 |
| `source_document_sha256` | SHA-256 字符串 | 导入时 PDF 字节版本 |
| `fingerprint` | SHA-256 字符串 | 外部标记稳定指纹 |
| `subtype` | 字符串 | `Highlight`、`Underline`、`Squiggly` 或 `StrikeOut` |
| `page_index` | 整数 | 首片段零基页号 |
| `annotation_name` | 字符串或 `null` | PDF `/NM` |
| `author` | 字符串或 `null` | PDF `/T` |
| `subject` | 字符串或 `null` | PDF `/Subj` |
| `creation_date` / `modified_date` | 字符串或 `null` | 原始 PDF 日期文本 |
| `mapping_confidence` | 整数 | 几何到文字映射的估计分数 |
| `generated_note` | 布尔值 | 旧版兼容字段；新导入不再生成占位说明 |
| `color` | 数值数组 | PDF 标记颜色 |
| `rect_pdf` | 四数数组 | 原始矩形 |
| `quad_points_pdf` | 八数数组列表 | 原始 quad points |

### 11.3 `pdf_sync`

记录级双向同步身份如下：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `version` | 整数 | 当前为 1 |
| `origin` | 字符串 | `pta` 或 `external` |
| `annotation_name` | 字符串或 `null` | 对应 PDF `/NM` |
| `fingerprint` | SHA-256 字符串 | 已确认 PDF 标记身份 |
| `last_seen_document_sha256` | SHA-256 字符串 | 最近确认存在该记录的 PDF 版本 |
| `pdf_state_sha256` | SHA-256 字符串 | PDF 侧记录状态 |
| `sidecar_state_sha256` | SHA-256 字符串 | Markdown 侧记录状态 |

这些字段用于区分单边修改、可自动合并的不同记录修改，以及同一记录双边冲突。用户不应手工伪造同步指纹。

## 12. 外部编辑与保留规则

### 12.1 可以安全编辑的内容

用户可以：

- 编辑 `### Note` 正文；
- 在记录之外增加普通 Markdown；
- 在理解本规范后修改 `type`，但 Bookmark 的 `position` 必须同步满足约束；
- 删除一整条完整记录；
- 使用普通文本编辑器查看和版本控制侧车。

用户不应手工修改 `id`、范围、引文、哈希、PDF 几何和同步指纹。位置变化应通过 PTA 重匹配流程处理。

### 12.2 保留行为

当前 PTA 的行为是：

- 新增记录时保留原侧车内容并在末尾追加；
- 结构化修改类型或正文时保留该记录的 ID、范围、锚点及未知 YAML 字段；
- 删除时只移除指定完整记录并重新编号显示标题；
- 重匹配时保留用户正文和未知字段，但会重建 `range`、`anchor`、可选 `pdf_anchor` 并追加历史；
- PDF 协调可以规范化被更新记录，第三方未知字段在该记录被重写时不保证保留；
- 记录之外的普通 Markdown不参与解析，应尽量原样保留。

需要扩展格式的外部工具应使用明确命名空间，例如 `x-example-*`，并在写入前备份。不得依赖 PTA 永久保留未规范化的记录内部扩展。

## 13. 验证与错误处理

侧车保存或重载前至少验证：

- 保留起止标记格式正确、数量配对且不嵌套；
- 每条记录具有非空稳定 ID，且 ID 唯一；
- 标记 ID 与 YAML `id` 一致；
- `type` 受支持；
- 范围存在、非空且格式有效；
- Bookmark 的 `position` 有效，非 Bookmark 不得含 `position`；
- `### Original text` 和 `### Note` 结构存在；
- Original text 是非空 Markdown blockquote；
- PDF 写回时 YAML、哈希、范围、引文和几何全部与当前 PDF 匹配。

结构无效时 PTA 必须停止覆盖磁盘原文件并报告记录或行号。校验失败不得通过删除未知内容或静默修复范围继续保存。

## 14. 并发修改、原子写入与备份

PTA 对加载时文件字节保存 SHA-256 指纹。写入原文或侧车前以及原子替换前必须再次比较磁盘指纹：

- 磁盘变化时不得静默覆盖；
- 用户可以取消、重载磁盘，或先保存唯一命名的冲突副本；
- 写入使用同目录临时文件、刷新到磁盘和原子替换；
- 重匹配和 PDF 冲突应用前创建带时间戳备份；
- 写后验证失败时恢复原文件；若文件再次并发变化，不得覆盖该更新。

这些规则是 PTA 的安全写入契约，不要求普通 Markdown 阅读器实现。

## 15. 兼容性

### 15.1 版本 1 读取

合规读取器必须理解本文核心字段。读取器应忽略不影响核心结构的未知 YAML 字段，但在写回未知字段时遵守第 12 节限制。

### 15.2 旧记录

PTA 可以读取以下旧式情况：

- 缺少 `type`，按 `note` 处理；
- 缺少 `anchor`、PDF 字段或 `rematch_history`；
- 稳定 ID 只在标记或 YAML 一处存在。

一旦由当前版本新建或规范化写出，记录应符合本文完整格式。重复 ID、无效范围、未知类型、空引文或不配对标记不是兼容情况，而是格式错误。

### 15.3 未知文档版本

`version` 不等于 1 的文档不属于本规范。工具不得假设未来版本与版本 1 可安全写回；应只读打开或拒绝写入，并提示升级。

## 16. 合规示例与验证

仓库提供以下材料：

- `examples/annotation_types/types.md`：稳定原文；
- `examples/annotation_types/types.md.annotations.md`：Note、Highlight、Bookmark 各一条的完整版本 1 侧车；
- `examples/rematch_cases/`：位移、局部修改、重复文本和删除引文案例；
- `acceptance/manual/generated/`：已完成人工验收的创建和重匹配证据；
- `acceptance/data/rematch_cases.json`：120 个带标准答案的机器验收案例。

解析一个示例不等于确认锚点健康。合规检查还必须读取对应原文，验证 `range`、引文和哈希的一致性。
