# Pure Text Annotator 验收视频：开发过程与技术细节导览

这份文档用于视频中的“开发过程与具体技术细节”部分。建议控制在 **7–8 分钟**，只讲最能说明设计思路的六个技术点，不逐行解释界面代码。

## 如何边讲边跳代码

先在项目根目录打开本文档：

```bash
vim PRESENTATION_TECHNICAL_GUIDE.md
```

- 把光标放到下文单独一行的相对路径上，按 `gf` 打开文件。
- 看完代码后按 `Ctrl-o` 返回本文档。
- 每节给出的 `/函数名` 可以直接在 Vim 中搜索。
- 路径没有附加行号，避免影响 `gf` 识别。
- 下文代码只节选主线；少量类型标注和非关键参数被省略，录制时以 `gf` 打开的真实文件为准。
- 如果 `gf` 找不到文件，先执行 `:cd /Users/sean/pureTextAnnotator`。

建议讲解顺序：程序入口与分层 → 标注模型 → 三类型交互与筛选 → 保存安全 → 重匹配 → PDF 扩展 → 验收结果。

## 1. 程序入口与整体分层（约 40 秒）

代码入口：

src/pure_text_annotator/cli.py

进入文件后搜索：

```text
/def main
```

重点代码：

```python
source = load_source_file(
    args.file,
    use_pdf_cache=not args.no_pdf_cache,
    rebuild_pdf_cache=args.rebuild_pdf_cache,
)
annotation_store = AnnotationStore(
    source.path,
    sidecar_path=args.annotations,
)
app = ViewerApp(
    source,
    soft_wrap=not args.no_wrap,
    initial_source_cursor=load_cursor_position(source),
    embed_on_annotation_save=not args.no_embed_on_save,
    annotation_store=annotation_store,
)
app.run()
```

代码块概括：组装并启动应用

该片段依次加载原文、创建侧车存储、恢复光标位置并启动 `ViewerApp`，是命令行层连接文件层和界面层的入口。

技术要点：

- `load_source_file` 负责原文加载，TXT、Markdown 和 PDF 在这里形成统一的文本视图。
- `AnnotationStore` 负责独立的 Markdown 标注侧车文件。
- `ViewerApp` 只负责终端交互和流程编排，核心数据与算法放在独立模块中，便于测试。

可直接口播：

> 项目没有把读取、标注和界面逻辑全部写在一个文件中。命令行入口先加载原文，再建立标注存储，最后启动终端界面。这样的分层让标注格式、重匹配算法和 PDF 处理都可以脱离界面单独测试。

## 2. 标注数据模型与三种类型（约 1 分钟）

代码文件：

src/pure_text_annotator/annotations.py

依次搜索：

```text
/class AnnotationType
/def create_record
```

三种类型由枚举统一约束：

```python
class AnnotationType(str, Enum):
    """The three annotation kinds required by the project specification."""

    NOTE = "note"
    HIGHLIGHT = "highlight"
    BOOKMARK = "bookmark"
```

代码块概括：定义三种标注类型

该枚举把 Note、Highlight 和 Bookmark 限定为三个合法持久化值，解析、筛选以及导入导出都复用这一定义。

创建记录时再验证各类型的规则：

```python
annotation_type = AnnotationType(annotation_type)
if annotation_type is AnnotationType.NOTE and not note.strip():
    raise ValueError("annotation note cannot be empty")
if annotation_type is AnnotationType.BOOKMARK:
    if bookmark_position is None:
        bookmark_position = draft.start
```

代码块概括：校验不同类型的必需字段

该片段先规范化输入类型，再检查 Note 的说明不能为空、Bookmark 必须具有位置；非法输入会在写入前立即报错。

技术要点：

- Note 保存“选区 + 用户说明”；Highlight 允许没有说明；Bookmark 以光标位置为语义锚点。
- 文本范围统一使用 Unicode 字符偏移的半开区间 `[start, end)`，避免中英文混排时按字节计数造成错位。
- 每条记录还保存 UUID、原文引用、前后文、哈希和原始位置，为后续重匹配提供多重证据。
- 标注写入独立的 `.annotations.md` 文件，原文和标注内容互不混写。

正式格式规范可在需要时打开：

ANNOTATION_FORMAT_SPEC.md

可直接口播：

> 我把笔记、高亮和书签建模为同一个标注记录的三种类型。三者共用稳定 ID、文本范围和锚点信息，但各自有不同约束。侧车文件采用可读的 Markdown 和 YAML 元数据，因此用户不运行程序也能查看和备份标注。

## 3. 三类型创建与非破坏性筛选（约 1 分钟）

代码文件：

src/pure_text_annotator/app.py

依次搜索：

```text
/def _start_typed_annotation
/def _finish_annotation_filter
```

Bookmark 使用当前光标，其他类型使用选区：

```python
if annotation_type is AnnotationType.BOOKMARK:
    bookmark_position = location_to_offset(
        source_text, viewer.cursor_location
    )
    if bookmark_position < len(source_text):
        start = bookmark_position
        end = start + 1
    else:
        start = len(source_text) - 1
        end = len(source_text)
    draft = AnnotationDraft(start, end, source_text[start:end])
else:
    current_selection = selection or viewer.selection
    draft = draft_from_selection(
        self.source.text,
        current_selection.start,
        current_selection.end,
    )
```

代码块概括：把光标或选区转换为标注草稿

Bookmark 从当前光标生成单字符锚点，Note 和 Highlight 从选择范围生成草稿，最终统一得到 `start`、`end` 和 `quote`。

筛选只改变当前显示投影，不改写侧车文件：

```python
selected = None if choice == "all" else AnnotationType(choice)
self.annotation_type_filter = selected
annotator.set_annotation_type_filter(selected)
visible = self._valid_annotation_links()
```

代码块概括：建立非破坏性的类型筛选

该片段把 All 或具体类型转换为界面筛选状态并重算可见链接，只改变显示和跳转范围，不改写侧车文件。

技术要点：

- 创建流程共用一个入口，避免三种类型分别实现后产生行为差异。
- 类型筛选保存的是界面状态；`visible` 只决定哪些记录显示和参与跳转，不删除磁盘数据。
- 左右窗格共享同一条 `AnnotationLink` 映射，所以高亮、焦点和 `Ctrl-n` / `Ctrl-p` 跳转能够保持一致。

可直接口播：

> 三种标注没有复制三套交互代码，而是先统一生成 draft，再根据类型补充字段。筛选功能也不是重新生成标注文件，而是对已经解析出的记录建立显示投影，所以切换筛选不会造成数据丢失。

## 4. 外部修改检测与原子保存（约 1 分 15 秒）

状态检测代码：

src/pure_text_annotator/file_safety.py

搜索：

```text
/class BufferChangeState
/def require_unchanged
```

```python
class BufferChangeState(str, Enum):
    UNCHANGED = "unchanged"
    MEMORY_ONLY = "memory-only"
    DISK_ONLY = "disk-only"
    BOTH = "memory-and-disk"

def require_unchanged(path, expected, *, label):
    current = fingerprint_path(path)
    if current != expected:
        raise ConcurrentFileChangeError(
            f"{label} changed on disk after it was loaded: {path}"
        )
```

代码块概括：识别内存与磁盘变化

`BufferChangeState` 表示内存和磁盘的四种变化组合；`require_unchanged` 重新计算磁盘指纹，不一致时中止写入。

原文保存代码：

src/pure_text_annotator/files.py

搜索：

```text
/def save_text_file
```

```python
require_unchanged(path, expected_fingerprint, label="source file")

with tempfile.NamedTemporaryFile(
    mode="wb", dir=path.parent, delete=False
) as temporary:
    temporary.write(encoded)
    temporary.flush()
    os.fsync(temporary.fileno())

require_unchanged(path, expected_fingerprint, label="source file")
os.replace(temporary_path, path)
```

代码块概括：以双重校验完成原子保存

该片段在保存前核对指纹，把完整内容写入临时文件并执行 `fsync`，再次核对后使用 `os.replace` 原子替换原文件。

技术要点：

- 加载文件时记录“是否存在、字节数、SHA-256”，保存前比较磁盘当前指纹。
- 第一次检查防止覆盖已经发生的外部修改；写临时文件后再次检查，缩小检查与替换之间的竞态窗口。
- `fsync` 后用 `os.replace` 原子替换，避免程序中断时留下半个文件。
- 内存和磁盘同时变化时不擅自合并，而是提示 Reload、Overwrite、Save Copy 或 Cancel；Save Copy 会保留冲突副本。

可直接口播：

> 编辑器最危险的情况不是保存失败，而是悄悄覆盖外部编辑器的新内容。程序因此把内存变化和磁盘变化分成四种状态，并在保存前后两次核验文件指纹。真正写盘时采用同目录临时文件加原子替换，既防并发覆盖，也防止只写入一半。

## 5. 标注失配检测与重匹配（约 1 分 40 秒）

算法文件：

src/pure_text_annotator/rematch.py

依次搜索：

```text
/class RematchStatus
/def _candidate_confidence
/def scan_rematches
/def apply_rematches
```

扫描结果先被分类，而不是直接修改文件：

```python
class RematchStatus(str, Enum):
    HEALTHY = "Healthy"
    SHIFTED = "Shifted"
    MODIFIED = "Modified"
    AMBIGUOUS = "Ambiguous"
    ORPHANED = "Orphaned"
    MALFORMED = "Malformed"
```

代码块概括：分类重匹配结果

该枚举描述锚点从健康、平移、修改到歧义、失效或格式错误的状态，供预览界面决定是否需要用户处理。

候选位置使用多种证据评分：

```python
raw = 100 * (
    0.45 * quote_similarity
    + 0.20 * context
    + 0.15 * uniqueness
    + 0.10 * proximity
    + 0.10 * neighbor_consistency
)
```

代码块概括：为候选位置计算综合分数

该公式将引用相似度、上下文、候选唯一性、旧位置距离和相邻标注关系按权重合成为 0–100 的候选分数。

扫描阶段只生成报告，并记录原文和侧车文件的哈希：

```python
def scan_rematches(source_text: str, sidecar_text: str) -> RematchReport:
    records = parse_annotation_records(sidecar_text)
    items = tuple(_scan_record(source_text, record) for record in records)
    return RematchReport(
        source_sha256=text_sha256(source_text),
        sidecar_sha256=text_sha256(sidecar_text),
        items=_apply_document_consistency(items, len(source_text)),
    )
```

代码块概括：生成只读的重匹配报告

该函数解析侧车记录、逐条扫描候选并进行文档级一致性校正，同时保存原文和侧车哈希供应用前再次验证。

技术要点：

- 搜索顺序是原引用精确匹配、Unicode/空白归一化匹配、模糊匹配，最后才判定无法定位。
- 评分主要考虑引用相似度，同时结合前后文、候选唯一性、与旧位置的距离和相邻标注关系。
- 先给用户展示候选位置与估计正确度，只有用户确认后才执行 `apply_rematches`。
- 应用前再次核验报告中的两个哈希，避免用户确认期间文件又被修改。
- 最终写入通过 `AnnotationStore.transactional_write` 先备份、再写入、再验证；验证失败则回滚。
- 界面中的百分比是启发式“估计正确度”，不是经过概率校准的统计学概率，介绍时不要称为绝对准确率。

事务写入位置：

src/pure_text_annotator/annotations.py

搜索：

```text
/def transactional_write
```

可直接口播：

> 原文发生插入、删除或轻微改写后，只保存偏移量是不够的。我的方案先尝试精确引用，再尝试规范化和模糊匹配，并把引用、上下文、唯一性和位置距离组合成候选分数。算法只生成预览，不会自动改文件；用户看到估计正确度并确认后，程序才以带备份和回滚的事务方式应用结果。

## 6. PDF 文本投影、缓存与双向标注（约 1 分 10 秒）

PDF 是扩展能力，视频中只说明数据流，不展开 PDF 对象结构。

文本提取与锚点：

src/pure_text_annotator/pdfs.py

依次搜索：

```text
/def anchor_for_range
/def load_pdf_file
```

缓存以原始 PDF 的 SHA-256 和提取器版本共同命名：

```python
document_sha256 = hashlib.sha256(raw).hexdigest()
cache_path = pdf_cache_path(document_sha256, cache_dir)
cached = _load_pdf_cache(
    cache_path,
    expected_document_sha256=document_sha256,
    expected_size_bytes=len(raw),
)
```

代码块概括：按 PDF 内容哈希复用提取缓存

该片段先对原始 PDF 字节计算 SHA-256，再用文档哈希和提取器身份定位缓存；校验通过即可跳过全文重新提取。

嵌入 PDF 标注：

src/pure_text_annotator/embed.py

搜索：

```text
/def _text_markup
```

```python
annotation[NameObject("/Contents")] = TextStringObject(record.note)
annotation[NameObject("/NM")] = TextStringObject(record.annotation_id)
annotation[NameObject(PTA_TYPE_KEY)] = TextStringObject(
    record.annotation_type.value
)
```

代码块概括：把侧车记录写成 PDF 标注对象

该片段把说明、稳定 ID 和 PTA 类型写入 PDF 注释字典，使导出结果既能显示，也能被后续导入和对账识别。

导入外部 PDF 标注：

src/pure_text_annotator/pdf_import.py

搜索：

```text
/def _effective_annotation_type
```

```python
if part.annotation_type is not None:
    return part.annotation_type
return (
    AnnotationType.HIGHLIGHT
    if part.generated_note
    else AnnotationType.NOTE
)
```

代码块概括：恢复导入标注的业务类型

该逻辑优先采用 PTA 写入的显式类型；没有该字段时，再根据是否使用自动占位说明推断 Highlight 或 Note。

技术要点：

- PDF 首先被转换为稳定的文本投影，同时保留每个词在页面中的矩形坐标。
- `anchor_for_range` 把字符范围反向映射为页码、矩形和 QuadPoints，使侧车标注可以嵌入 PDF。
- PDF 内容未变化时直接复用精确哈希缓存，避免每次打开都重新提取全文。
- `/NM` 保存稳定标注 ID，私有 `/PTAType` 保存三类型信息；导入时优先读取明确类型，再兼容其他软件创建的传统高亮。
- 保存侧车后执行 PDF 对账，使 Preview 等外部软件中的增加、修改或删除能够进入冲突处理流程。

可直接口播：

> PDF 支持没有改变核心标注模型，而是增加了一层文本到页面几何坐标的映射。打开 PDF 时提取文字和坐标，并按文件哈希缓存；嵌入时再把字符范围转换为 PDF 的 QuadPoints。稳定 ID 和类型字段让程序能够区分自身标注，同时也兼容 Preview 等软件创建的普通高亮。

## 7. 正式测试与验收证据（约 45 秒）

自动验收脚本：

acceptance/run_acceptance.py

搜索：

```text
/CASE_COUNTS
/def generate_rematch_cases
/def _write_performance_fixture
```

脚本使用固定随机种子和明确规模，测试结果可以重复生成：

```python
SEED = 20261114
PERFORMANCE_SOURCE_BYTES = 1_000_000
PERFORMANCE_ANNOTATIONS = 1_000
FORMAL_LOAD_RUNS = 10
FORMAL_OPERATION_COUNT = 200
```

代码块概括：固定正式验收的规模与次数

这些常量固定随机种子、1 MB 原文、1000 条标注和运行次数，使重匹配及性能验收能够在相同条件下复现。

正式报告：

FORMAL_ACCEPTANCE_TEST_REPORT.md

人工证据记录：

acceptance/manual/generated/EVIDENCE_RECORD.md

视频中只报以下结果即可：

- 120 个独立构造的重匹配案例；唯一且未改写的引用 65/65 自动正确。
- 综合结果为 80 个自动正确、30 个要求确认、10 个无效锚点被识别、0 个错误自动应用。
- 1,000,000 字节原文和 1,000 条标注下，10 次加载全部小于 2 秒，最大值约 1.802 秒。
- 200 次常用操作中 197 次不超过 100 毫秒，通过率 98.5%。
- 自动回归测试共 181 项；人工验收 13/13 通过，并保留 20 张截图。

可直接口播：

> 除了单元测试，我还编写了可重复运行的正式验收脚本。它用固定种子生成 120 个带真实答案的重匹配案例，并现场生成 1 MB 原文和 1000 条标注的性能数据。最终结果记录在正式报告中，人工验收也有独立记录和截图，而不是只凭演示判断功能正确。

## 推荐的 7–8 分钟时间分配

| 时间 | 内容 | 必须展示的代码 |
| --- | --- | --- |
| 0:00–0:40 | 程序入口与分层 | `cli.py` 的 `main` |
| 0:40–1:40 | 三种标注类型与侧车模型 | `AnnotationType`、`create_record` |
| 1:40–2:40 | 创建流程与筛选 | `_start_typed_annotation`、`_finish_annotation_filter` |
| 2:40–3:55 | 外部修改检测和原子保存 | `require_unchanged`、`save_text_file` |
| 3:55–5:35 | 重匹配算法和确认机制 | `RematchStatus`、评分公式、`scan_rematches` |
| 5:35–6:45 | PDF 锚点、缓存和导入导出 | `load_pdf_file`、`_text_markup` |
| 6:45–7:30 | 正式测试结果 | `run_acceptance.py`、测试报告 |

## 为了方便口播，主动省略的内容

- 不逐个介绍所有 Vim 键位；使用方法部分已经负责演示键位。
- 不逐行解释 Textual 控件、CSS 和消息事件，只说明左右窗格共享标注映射。
- 不解释 Markdown/YAML 解析器的每个正则表达式，只展示正式格式规范。
- 不展开 PDF 坐标变换、对象字典和冲突分支的全部实现，只讲“文本范围—页面几何—稳定 ID”主线。
- 不把启发式候选分数说成绝对概率，也不把 PDF 扩展说成任务书的基础要求。

## 一句话收尾

> 这个项目的重点不只是实现一个终端标注界面，而是用可读的侧车格式、可恢复的保存流程、需人工确认的重匹配机制和可复现的验收数据，保证文本发生变化后标注仍然可管理、可追踪、可验证。
