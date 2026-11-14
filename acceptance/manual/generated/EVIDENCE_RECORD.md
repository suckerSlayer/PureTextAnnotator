# 人工验收证据记录

- 执行人：
- 日期与时区：2026-11-14，Asia/Shanghai
- Git 提交：`ab28fc9`
- PTA 版本：0.11.0
- 终端及尺寸：MacOS Terminal 90x42
- 操作系统：macOS 15.5，arm64

状态只能填写 `Pass`、`Fail` 或 `Blocked`。证据栏填写 `evidence/` 下的文件名。

| ID | 检查项 | 状态 | 实际结果/偏差 | 截图证据 |
| --- | --- | --- | --- | --- |
| A-01 | 初始界面 | Pass | 双栏初始状态和原文显示符合预期。 | ![A-01 初始界面](evidence/A-01-initial.png) |
| A-02 | 创建 Note | Pass | Note 创建、原文引用、正文和当前高亮符合预期。 | ![A-02 创建 Note](evidence/A-02-note.png) |
| A-03 | 创建 Highlight | Pass | Highlight 无正文创建和显示符合预期。 | ![A-03 创建 Highlight](evidence/A-03-highlight.png) |
| A-04 | 创建 Bookmark | Pass | Bookmark 的类型、位置和最小锚点符合预期。 | ![A-04 创建 Bookmark](evidence/A-04-bookmark.png) |
| A-05 | 筛选与首尾导航 | Pass | Note、Highlight、Bookmark 和 All 筛选及首尾导航符合预期。 | ![A-05 Note 筛选](evidence/A-05-filter-note.png)<br>![A-05 Highlight 筛选](evidence/A-05-filter-highlight.png)<br>![A-05 Bookmark 筛选](evidence/A-05-filter-bookmark.png)<br>![A-05 All 筛选](evidence/A-05-filter-all.png) |
| A-06 | 结构化编辑 | Pass | Note 正文更新，类型和锚点保持符合预期。 | ![A-06 结构化编辑 Note](evidence/A-06-edit-note.png) |
| A-07 | 保存与重启 | Pass | 保存、退出和重新打开后的三类型持久化符合预期。 | ![A-07 保存与重新打开](evidence/A-07-reopen.png) |
| A-08 | 取消和确认删除 | Pass | 取消时记录保留，确认后 Highlight 删除且原文不变。 | ![A-08 取消删除](evidence/A-08-delete-cancel.png)<br>![A-08 确认删除](evidence/A-08-delete-confirm.png) |
| B-01 | 预置三类型 | Pass | 预置 Note、Highlight 和 Bookmark 各一条，定位与筛选符合预期。 | ![B-01 预置三类型](evidence/B-01-preloaded.png) |
| B-02 | 原文移动 | Pass | 原文插入与保存成功，旧侧车等待人工审查。 | ![B-02 修改原文](evidence/B-02-source-edited.png) |
| B-03 | 重匹配候选 | Pass | 三种类型的位移候选、上下文和估算正确率符合预期。 | ![B-03 Note 候选](evidence/B-03-note-candidate.png)<br>![B-03 Highlight 候选](evidence/B-03-highlight-candidate.png)<br>![B-03 Bookmark 候选](evidence/B-03-bookmark-candidate.png) |
| B-04 | 最终确认与应用 | Pass | 最终确认保留双栏可见，应用后类型、稳定 ID 和 Note 正文保留。 | ![B-04 最终确认](evidence/B-04-final-confirm.png)<br>![B-04 已应用](evidence/B-04-applied.png) |
| B-05 | 重启后定位 | Pass | 重启后三条记录均定位到修改后原文，未重复提出同一位移。 | ![B-05 重启验证](evidence/B-05-reopen.png) |

## 失败与修复记录

| ID | 复现步骤 | 期望 | 实际 | 缺陷/提交 | 复测结果 |
| --- | --- | --- | --- | --- | --- |
|  |  |  |  |  |  |

## 最终结论

- [x] 13 个检查项全部 Pass。
- [x] 所有指定截图已经保留。
- [x] 本轮无失败项；证据未被覆盖或删除。
- 结论：13 项人工验收全部通过。
- 执行人签名/日期：刘申/20261114
