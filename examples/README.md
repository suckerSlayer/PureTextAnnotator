# Pure Text Annotator 示例索引

这些示例用于学习操作和检查格式。正式侧车语法见
[`ANNOTATION_FORMAT_SPEC.md`](../ANNOTATION_FORMAT_SPEC.md)，完整操作见
[`USER_MANUAL.md`](../USER_MANUAL.md)。

## 1. 三种标注类型

目录：[`annotation_types/`](annotation_types/)

包含一份稳定原文和完整版本 1 侧车：

- Note：带非空 Markdown 正文；
- Highlight：正文为空；
- Bookmark：保存精确位置和一个字符的最小锚点。

直接查看：

```console
pta-view examples/annotation_types/types.md
```

如需练习增删改，建议复制到临时目录，避免修改作为格式参考的文件：

```console
mkdir -p /tmp/pta-annotation-types
cp examples/annotation_types/types.md* /tmp/pta-annotation-types/
pta-view /tmp/pta-annotation-types/types.md
```

## 2. 可复位重匹配案例

目录：[`rematch_cases/`](rematch_cases/)

包含四类失效范围：

1. 引文未改但整体位移；
2. 引文轻微修改；
3. 重复引文依赖上下文区分；
4. 引文已删除，必须保持未解决。

运行：

```console
pta-view examples/rematch_cases/cases.md
```

该案例带 PTA demo 标记。第一次按 `R` 可以把原文和侧车复位到仓库中的失效基线，然后再次按 `R` 进入审查。详细步骤见
[`rematch_cases/README.md`](rematch_cases/README.md)。

## 3. 已完成人工验收的材料

目录：[`../acceptance/manual/generated/`](../acceptance/manual/generated/)

该目录保存已经执行完成的证据，不是空白练习：

- 13/13 检查项 Pass；
- 20 张真实 PNG 截图；
- 创建三种类型后的侧车；
- 原文位移、候选审查、最终应用后的侧车；
- `before-rematch` 备份。

查看逐项记录：

[`EVIDENCE_RECORD.md`](../acceptance/manual/generated/EVIDENCE_RECORD.md)

不要用 `--force` 覆盖这批证据。新练习应输出到另一目录：

```console
.venv/bin/python acceptance/generate_manual_acceptance.py \
  --output acceptance/manual/run-YYYYMMDD
```

## 4. 机器验收数据

目录：[`../acceptance/`](../acceptance/)

- `data/rematch_cases.json`：120 个带标准答案的重匹配案例；
- `results/acceptance_results.json`：环境、逐项结果和性能原始计时；
- `run_acceptance.py`：完整复现脚本；
- `FORMAL_ACCEPTANCE_TEST_REPORT.md`：正式结论和计时边界。

运行：

```console
.venv/bin/python acceptance/run_acceptance.py
```

## 5. 示例使用原则

- 先复制再编辑不可复位的格式参考文件；
- 不把演示侧车误当成自己的默认侧车；
- 不手工修正失效范围来跳过重匹配审查；
- 保存截图前确认不包含不应公开的路径或私人内容；
- 任何范围都按去 BOM、LF 换行的 Unicode 码点文本计算。
