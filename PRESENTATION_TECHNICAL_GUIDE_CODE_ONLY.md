# Pure Text Annotator 验收视频：开发过程与技术细节导览

这份文档用于视频中的“开发过程与具体技术细节”部分。建议控制在 **7–8 分钟**，只讲最能说明设计思路的六个技术点，不逐行解释界面代码。

## 如何边讲边跳代码

先在项目根目录打开本文档：

```bash
vim PRESENTATION_TECHNICAL_GUIDE_CODE_ONLY.md
```

- 把光标放到下文单独一行的相对路径上，按 `gf` 打开文件。
- 看完代码后按 `Ctrl-o` 返回本文档。
- 每节给出的 `/函数名` 可以直接在 Vim 中搜索。
- 路径没有附加行号，避免影响 `gf` 识别。
- 下文代码只节选主线；少量类型标注和非关键参数被省略，录制时以 `gf` 打开的真实文件为准。
- 如果 `gf` 找不到文件，先执行 `:cd /Users/sean/pureTextAnnotator`。

建议讲解顺序：程序入口与分层 → 标注模型 → 三类型交互与筛选 → 保存安全 → 重匹配 → PDF 扩展 → 验收结果。

## 1. 程序入口与整体分层

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

 

 

 
 
 

 

 

## 2. 标注数据模型与三种类型

代码文件：

src/pure_text_annotator/annotations.py

 

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

 

 

 
 
 
 

正式格式规范可在需要时打开：

ANNOTATION_FORMAT_SPEC.md

 

 

## 3. 三类型创建与非破坏性筛选

代码文件：

src/pure_text_annotator/app.py

 

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

 

筛选只改变当前显示投影，不改写侧车文件：

```python
selected = None if choice == "all" else AnnotationType(choice)
self.annotation_type_filter = selected
annotator.set_annotation_type_filter(selected)
visible = self._valid_annotation_links()
```

代码块概括：建立非破坏性的类型筛选

 

 

 
 
 

 

 

## 4. 外部修改检测与原子保存

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

 

 

 
 
 
 

 

 

## 5. 标注失配检测与重匹配

算法文件：

src/pure_text_annotator/rematch.py

 

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

 

 

 
 
 
 
 
 

事务写入位置：

src/pure_text_annotator/annotations.py

搜索：

```text
/def transactional_write
```

 

 

## 6. PDF 文本投影、缓存与双向标注

PDF 是扩展能力，视频中只说明数据流，不展开 PDF 对象结构。

文本提取与锚点：

src/pure_text_annotator/pdfs.py

 

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

 

 

 
 
 
 
 

 

 

## 7. 正式测试与验收证据

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

 
