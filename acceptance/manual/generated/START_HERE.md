# 三类型人工验收执行脚本

本文件是人工操作脚本，不是自动化测试输出。执行者必须亲自完成每个步骤，
在 `EVIDENCE_RECORD.md` 填写结果，并按指定文件名保存真实截图。

## 0. 准备

1. 使用至少 90 列 × 42 行的终端。
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

- 在左侧完整选择：`组合锚点让批注与原文保持可审查的联系。`
- 按 `a`，输入：`人工验收批注：组合锚点需要保留原文和说明。`
- 提交批注。
- 预期：右侧新增 `type: note`；原文引用和说明均可见；左侧目标变为当前蓝色。
- 证据：![A-02 创建 Note](evidence/A-02-note.png)

### A-03 创建 Highlight

- 在左侧完整选择：`黄色高亮用于标记重要内容，不要求填写说明。`
- 按 `m`，选择 **Add highlight**。
- 预期：无需填写说明；右侧新增 `type: highlight`，`### Note` 内容为空。
- 证据：![A-03 创建 Highlight](evidence/A-03-highlight.png)

### A-04 创建 Bookmark

- 取消选择，把光标放到句子 `书签目标位于这一行的“返回”二字，便于检查精确光标位置。` 中“返”字的位置。
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
