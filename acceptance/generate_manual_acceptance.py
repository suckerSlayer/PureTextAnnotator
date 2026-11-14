#!/usr/bin/env python3
"""Generate a disposable, standalone three-type human acceptance suite."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from pure_text_annotator.annotations import (
    AnnotationDraft,
    AnnotationStore,
    AnnotationType,
    create_record,
)
from pure_text_annotator.rematch import parse_annotation_records


DEFAULT_OUTPUT = REPOSITORY_ROOT / "acceptance/manual/generated"
MARKER_NAME = ".pta-manual-acceptance-fixture"
NOTE_QUOTE = "组合锚点让批注与原文保持可审查的联系。"
HIGHLIGHT_QUOTE = "黄色高亮用于标记重要内容，不要求填写说明。"
BOOKMARK_SENTENCE = "书签目标位于这一行的“返回”二字，便于检查精确光标位置。"
BOOKMARK_CHARACTER = "返"
FIXED_TIME = "2026-11-14T00:00:00Z"


CREATE_SOURCE = f"""# 三类型人工验收：创建场景

这是一个可丢弃的工作文件。请严格按照同目录的 `START_HERE.md` 操作。

## Note 目标

{NOTE_QUOTE}

## Highlight 目标

{HIGHLIGHT_QUOTE}

## Bookmark 目标

{BOOKMARK_SENTENCE}

## 额外滚动内容

第一段用于检查页面滚动后标注颜色仍然稳定。

第二段用于检查在右侧筛选标注时原文不会被改写。

第三段用于检查保存、退出和重新打开之后三种类型仍然存在。
"""


REMATCH_SOURCE = f"""# 三类型人工验收：重匹配场景

在执行步骤 B-03 前，不要修改这一行以上的内容。

## Note 锚点

{NOTE_QUOTE}

## Highlight 锚点

{HIGHLIGHT_QUOTE}

## Bookmark 锚点

{BOOKMARK_SENTENCE}

## 末尾检查

三条预置记录应分别显示为 Note、Highlight 和 Bookmark。
"""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def _prepare_output(output: Path, *, force: bool) -> None:
    if not output.exists():
        output.mkdir(parents=True)
        return
    marker = output / MARKER_NAME
    if not force:
        raise FileExistsError(
            f"manual acceptance output already exists: {output}; "
            "use --force only after preserving screenshots and results"
        )
    if not marker.is_file():
        raise RuntimeError(
            f"refusing to replace unrecognized directory without {MARKER_NAME}: "
            f"{output}"
        )
    shutil.rmtree(output)
    output.mkdir(parents=True)


def _fixed_record(
    source_text: str,
    quote: str,
    *,
    annotation_id: str,
    annotation_type: AnnotationType,
    note: str = "",
    bookmark_position: Optional[int] = None,
):
    start = source_text.index(quote)
    draft = AnnotationDraft(start=start, end=start + len(quote), quote=quote)
    return create_record(
        draft,
        note,
        annotation_type=annotation_type,
        bookmark_position=bookmark_position,
        annotation_id=annotation_id,
        created_at=FIXED_TIME,
        source_text=source_text,
    )


def _write_rematch_case(directory: Path) -> Dict[str, object]:
    source_path = directory / "rematch.md"
    _write_text(source_path, REMATCH_SOURCE)
    bookmark_position = REMATCH_SOURCE.index(BOOKMARK_CHARACTER)
    records = (
        _fixed_record(
            REMATCH_SOURCE,
            NOTE_QUOTE,
            annotation_id="manual-note",
            annotation_type=AnnotationType.NOTE,
            note="人工验收预置批注：重匹配后正文和稳定 ID 都应保留。",
        ),
        _fixed_record(
            REMATCH_SOURCE,
            HIGHLIGHT_QUOTE,
            annotation_id="manual-highlight",
            annotation_type=AnnotationType.HIGHLIGHT,
        ),
        _fixed_record(
            REMATCH_SOURCE,
            BOOKMARK_CHARACTER,
            annotation_id="manual-bookmark",
            annotation_type=AnnotationType.BOOKMARK,
            bookmark_position=bookmark_position,
        ),
    )
    store = AnnotationStore(source_path)
    sidecar_text = store.append_to_text(store.initial_text(), records)
    store.write(sidecar_text)
    return {
        "source": str(source_path.relative_to(directory.parent)),
        "source_sha256": _sha256(source_path.read_bytes()),
        "sidecar": str(store.path.relative_to(directory.parent)),
        "sidecar_sha256": _sha256(store.path.read_bytes()),
        "records": [
            {
                "id": record.annotation_id,
                "type": record.annotation_type.value,
                "start": record.start,
                "end": record.end,
                "quote": record.quote,
            }
            for record in records
        ],
    }


def _checklist_text() -> str:
    return f"""# 三类型人工验收执行脚本

本文件是人工操作脚本，不是自动化测试输出。执行者必须亲自完成每个步骤，
在 `EVIDENCE_RECORD.md` 填写结果，并按指定文件名保存真实截图。

## 0. 准备

1. 使用至少 140 列 × 42 行的终端。
2. 从仓库根目录运行生成器；开始新一轮前才能使用 `--force`。
3. 不要直接修改 `examples/` 中的正式样例。
4. 截图保存到本目录的 `evidence/`；不要在完成记录前重新生成目录。

打开创建场景：

```console
pta-view acceptance/manual/generated/01-create/three-types.md
```

## A. 创建、筛选、编辑、删除和持久化

### A-01 初始状态

- 确认左侧可以阅读，右侧尚无标注记录。
- 预期：状态栏无错误；原文件内容与本清单列出的目标句一致。
- 证据：![A-01 初始界面](evidence/A-01-initial.png)

### A-02 创建 Note

- 在左侧完整选择：`{NOTE_QUOTE}`
- 按 `a`，输入：`人工验收批注：组合锚点需要保留原文和说明。`
- 提交批注。
- 预期：右侧新增 `type: note`；原文引用和说明均可见；左侧目标变为当前蓝色。
- 证据：![A-02 创建 Note](evidence/A-02-note.png)

### A-03 创建 Highlight

- 在左侧完整选择：`{HIGHLIGHT_QUOTE}`
- 按 `m`，选择 **Add highlight**。
- 预期：无需填写说明；右侧新增 `type: highlight`，`### Note` 内容为空。
- 证据：![A-03 创建 Highlight](evidence/A-03-highlight.png)

### A-04 创建 Bookmark

- 取消选择，把光标放到句子 `{BOOKMARK_SENTENCE}` 中“返”字的位置。
- 按 `m`，选择 **Add bookmark**。
- 预期：右侧新增 `type: bookmark` 和 `position`；左侧只标记最小锚点。
- 证据：![A-04 创建 Bookmark](evidence/A-04-bookmark.png)

### A-05 类型筛选与导航

- 按 `m`，依次筛选 Notes、Highlights、Bookmarks 和 All。
- 每种筛选下使用 Ctrl+N / Ctrl+P；到首尾时不得循环。
- 预期：右侧只投影所选类型，磁盘侧车仍保留三条记录；All 恢复三条。
- 证据：![A-05 Note 筛选](evidence/A-05-filter-note.png)
  ![A-05 Highlight 筛选](evidence/A-05-filter-highlight.png)
  ![A-05 Bookmark 筛选](evidence/A-05-filter-bookmark.png)
  ![A-05 All 筛选](evidence/A-05-filter-all.png)

### A-06 结构化编辑 Note

- 聚焦 Note，按 `m`，选择 **Edit current annotation**。
- 保持类型为 Note，把正文改为：`人工验收批注：正文已通过结构化表单修改。`
- 预期：稳定 ID、原文范围和引文不变，正文更新。
- 证据：![A-06 结构化编辑 Note](evidence/A-06-edit-note.png)

### A-07 保存与重新打开

- 按 Ctrl+S，退出，再次打开同一文件。
- 预期：三种类型、修改后的正文、筛选和 Ctrl+N/P 均正常；原文未因标注而改变。
- 证据：![A-07 保存与重新打开](evidence/A-07-reopen.png)

### A-08 确认删除 Highlight

- 聚焦 Highlight，按 `m`，选择 **Delete current annotation**。
- 先取消一次，确认记录仍在；再次执行并确认删除。
- 保存并重新打开。
- 预期：只剩 Note 与 Bookmark；被删高亮不再出现，原文保持不变。
- 证据：![A-08 取消删除](evidence/A-08-delete-cancel.png)
  ![A-08 确认删除](evidence/A-08-delete-confirm.png)

## B. 三类型重匹配

打开预置场景：

```console
pta-view acceptance/manual/generated/02-rematch/rematch.md
```

### B-01 检查预置记录

- 使用 Ctrl+N / Ctrl+P 依次访问三条记录并检查 All/三种类型筛选。
- 预期：各有一条 Note、Highlight、Bookmark；三条范围均与左侧准确对齐。
- 证据：![B-01 预置三类型](evidence/B-01-preloaded.png)

### B-02 修改原文并保存

- 在左侧按 `gg`，再按 `i`；在紧凑警告窗口输入区分大小写的 `Yes`。
- 在第一行前插入：`人工验收插入行：三种锚点都应向后移动。`，并换行。
- 按 Ctrl+S 保存，再按 Escape 回到 Normal 模式。
- 预期：只修改原文；旧侧车范围暂时保持，下一步由用户主动按 `R` 审查。
- 证据：![B-02 修改原文](evidence/B-02-source-edited.png)

### B-03 审查候选

- 按 `R`，用 `J` / `K` 查看三条记录。
- 预期：三条均显示 Shifted 或等价的精确位移候选；左侧绿色候选分别落在
  Note、Highlight 和 Bookmark 的原引文；Note/Highlight 约为 100%，单字符
  Bookmark 约为 80% 并不应被默认选中。
- 证据：![B-03 Note 候选](evidence/B-03-note-candidate.png)
  ![B-03 Highlight 候选](evidence/B-03-highlight-candidate.png)
  ![B-03 Bookmark 候选](evidence/B-03-bookmark-candidate.png)

### B-04 确认应用

- 用 `J` / `K` 复查；Note/Highlight 可保留默认选择，在 Bookmark 上按
  Space 明确选中低于 90% 的候选，然后按 Ctrl+S 打开最终确认。
- 在最终确认中再次用 `J` / `K` 复查，然后选择 **Apply**。
- 预期：最终确认不遮住双栏；应用后范围整体后移，三种类型、稳定 ID 和
  Note 正文都不改变；生成 `before-rematch` 备份。
- 证据：![B-04 最终确认](evidence/B-04-final-confirm.png)
  ![B-04 已应用](evidence/B-04-applied.png)

### B-05 重启验证

- 退出并重新打开；再次筛选三种类型并逐条跳转。
- 预期：三条记录都定位到修改后的正确原文；再次按 `R` 不应重复提出同一位移。
- 证据：![B-05 重启验证](evidence/B-05-reopen.png)

## C. 完成判定

- `EVIDENCE_RECORD.md` 中 A-01 至 B-05 都必须填写 Pass/Fail 和实际结果。
- 必须保留失败截图，不得只记录成功结果。
- 任一 Fail 都应附复现步骤；修复后另开一轮，不覆盖旧证据。
- 本脚本不自动生成截图，因为截图必须证明真实人工界面操作。
"""


def _evidence_text() -> str:
    rows = (
        ("A-01", "初始界面"),
        ("A-02", "创建 Note"),
        ("A-03", "创建 Highlight"),
        ("A-04", "创建 Bookmark"),
        ("A-05", "筛选与首尾导航"),
        ("A-06", "结构化编辑"),
        ("A-07", "保存与重启"),
        ("A-08", "取消和确认删除"),
        ("B-01", "预置三类型"),
        ("B-02", "原文移动"),
        ("B-03", "重匹配候选"),
        ("B-04", "最终确认与应用"),
        ("B-05", "重启后定位"),
    )
    body = "\n".join(
        f"| {case_id} | {title} | Not run |  |  |"
        for case_id, title in rows
    )
    return f"""# 人工验收证据记录

- 执行人：
- 日期与时区：
- Git 提交：
- PTA 版本：
- 终端及尺寸：
- 操作系统：

状态只能填写 `Pass`、`Fail` 或 `Blocked`。证据栏填写 `evidence/` 下的文件名。

| ID | 检查项 | 状态 | 实际结果/偏差 | 截图证据 |
| --- | --- | --- | --- | --- |
{body}

## 失败与修复记录

| ID | 复现步骤 | 期望 | 实际 | 缺陷/提交 | 复测结果 |
| --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |

## 最终结论

- [ ] 13 个检查项全部 Pass。
- [ ] 所有指定截图已经保留。
- [ ] 失败项和复测没有被覆盖或删除。
- 结论：
- 执行人签名/日期：
"""


def _evidence_readme_text() -> str:
    return """# Screenshot evidence

Store the real screenshots requested by `../START_HERE.md` in this directory.
Do not add generated placeholders. Keep failure evidence as well as successful
retests, and reference every filename from `../EVIDENCE_RECORD.md`.
"""


def _validate_generated_suite(output: Path) -> Dict[str, object]:
    create_source = output / "01-create/three-types.md"
    create_sidecar = create_source.with_name(
        create_source.name + ".annotations.md"
    )
    rematch_source = output / "02-rematch/rematch.md"
    rematch_sidecar = rematch_source.with_name(
        rematch_source.name + ".annotations.md"
    )
    required = (
        output / MARKER_NAME,
        output / "START_HERE.md",
        output / "EVIDENCE_RECORD.md",
        output / "evidence/README.md",
        create_source,
        rematch_source,
        rematch_sidecar,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f"generated suite is incomplete: {missing}")
    if create_sidecar.exists():
        raise RuntimeError("creation case must start without an annotation sidecar")
    records = parse_annotation_records(rematch_sidecar.read_text(encoding="utf-8"))
    type_counts = Counter(record.annotation_type.value for record in records)
    if len(records) != 3 or type_counts != {
        "note": 1,
        "highlight": 1,
        "bookmark": 1,
    }:
        raise RuntimeError(
            "rematch fixture must contain exactly one record of each type"
        )
    source_text = rematch_source.read_text(encoding="utf-8")
    for record in records:
        if source_text[record.start : record.end] != record.quote:
            raise RuntimeError(
                f"fixture range does not match quotation: {record.annotation_id}"
            )
    return {
        "required_files": len(required),
        "rematch_records": len(records),
        "annotation_type_counts": dict(sorted(type_counts.items())),
    }


def generate_suite(output: Path, *, force: bool = False) -> Dict[str, object]:
    """Create a fresh human acceptance directory and validate its anchors."""

    output = output.resolve()
    _prepare_output(output, force=force)
    _write_text(
        output / MARKER_NAME,
        "Generated by acceptance/generate_manual_acceptance.py\n",
    )
    create_path = output / "01-create/three-types.md"
    _write_text(create_path, CREATE_SOURCE)
    rematch_manifest = _write_rematch_case(output / "02-rematch")
    _write_text(output / "START_HERE.md", _checklist_text())
    _write_text(output / "EVIDENCE_RECORD.md", _evidence_text())
    _write_text(output / "evidence/README.md", _evidence_readme_text())

    manifest: Dict[str, object] = {
        "schema_version": 1,
        "generator": "acceptance/generate_manual_acceptance.py",
        "purpose": "three-type human acceptance",
        "create_case": {
            "source": str(create_path.relative_to(output)),
            "source_sha256": _sha256(create_path.read_bytes()),
            "sidecar_must_be_absent_initially": True,
            "target_quotes": {
                "note": NOTE_QUOTE,
                "highlight": HIGHLIGHT_QUOTE,
                "bookmark_sentence": BOOKMARK_SENTENCE,
            },
        },
        "rematch_case": rematch_manifest,
    }
    _write_text(
        output / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    validation = _validate_generated_suite(output)
    manifest["validation"] = validation
    _write_text(
        output / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a disposable three-type human acceptance suite."
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "replace an existing generated suite after checking its safety "
            "marker; this deletes any evidence stored inside it"
        ),
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = generate_suite(args.output, force=args.force)
    except (FileExistsError, RuntimeError, OSError, ValueError) as error:
        print(f"manual acceptance generation failed: {error}", file=sys.stderr)
        return 1
    validation = manifest["validation"]
    print(f"human acceptance suite: {args.output.resolve()}")
    print(
        "validated: "
        f"{validation['rematch_records']} rematch records; "
        f"types={validation['annotation_type_counts']}"
    )
    print(f"start with: {(args.output.resolve() / 'START_HERE.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
