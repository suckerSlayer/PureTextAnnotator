# Pure Text Annotator 用户操作手册

适用版本：0.11.0
文档日期：2026-11-28

## 1. 软件用途

Pure Text Annotator（PTA）是一个键盘优先的终端阅读与标注工具，支持：

- UTF-8 TXT、Markdown 和带文字层 PDF；
- Vim 风格阅读、跳转、搜索、选择、复制和编辑；
- Note、Highlight、Bookmark 三种标注类型；
- 原文与外置 Markdown 侧车的双栏显示；
- 类型筛选、双向定位、重匹配、外部修改保护和原文重新绑定；
- PDF 标准文本标记的导入、嵌入和双向冲突审查。

标注默认保存在原文旁边，不写入 TXT 或 Markdown 原文。PDF 标注在侧车保存后可以同步到源 PDF 的标准 annotation layer。

正式侧车格式见 [ANNOTATION_FORMAT_SPEC.md](ANNOTATION_FORMAT_SPEC.md)。

## 2. 安装

在仓库根目录执行：

```console
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

确认安装：

```console
pta-view --version
pta-view --help
```

Windows PowerShell 激活虚拟环境时使用：

```powershell
.venv\Scripts\Activate.ps1
```

## 3. 打开文件

### 3.1 TXT 或 Markdown

```console
pta-view path/to/file.txt
pta-view path/to/file.md
```

长行默认自动换行。需要保留每个物理行的横向结构时使用：

```console
pta-view --no-wrap path/to/file.md
```

### 3.2 PDF

```console
pta-view path/to/paper.pdf
```

PDF 必须具有可提取文字层。扫描图片 PDF 会被拒绝；当前版本不提供 OCR 或页面图像视图。

### 3.3 指定非默认侧车

```console
pta-view --annotations path/to/old.annotations.md path/to/renamed.md
```

该选项只对本次启动有效。默认侧车名始终是：

```text
<完整原文件名>.annotations.md
```

例如 `chapter.md` 对应 `chapter.md.annotations.md`，`paper.pdf` 对应 `paper.pdf.annotations.md`。

### 3.4 旧设备低功耗模式

在旧款 Mac 或其他性能受限的设备上，可以使用：

```console
pta-view --low-power path/to/file.md
```

该模式把终端重绘上限设为 30 FPS，使用常亮光标，关闭括号匹配和非必要动画，并按 30 Hz 合并滚轮或触控板滚动事件。它可以与 `--no-wrap` 同时使用：

```console
pta-view --low-power --no-wrap path/to/file.md
```

不指定 `--low-power` 时，默认视觉行为保持不变。

## 4. 界面与模式

### 4.1 双栏布局

- 左栏：原文或 PDF 文字投影；
- 右栏：完整 Markdown 标注侧车；
- 底部：模式、同步状态、筛选、修改状态和临时命令提示；
- 紧凑窗口：编辑确认、搜索、菜单、重匹配、冲突和保存选择。

紧凑窗口尽量保留双栏可见，使用户可以在确认前检查真实上下文。

### 4.2 Normal、Visual 和 Edit

| 模式 | 用途 | 进入 | 退出 |
| --- | --- | --- | --- |
| Normal | 移动、搜索、菜单、撤销、重做 | 启动默认 | 不适用 |
| Visual | 扩展文本选择 | `v` 或 Shift+方向键 | `v`、Escape 或复制后退出 |
| Edit | 修改当前文本缓冲区 | `i` | Escape 或 Ctrl+`[` |

PDF 左栏始终只读。右侧 Markdown 侧车可以进入 Edit 模式。

### 4.3 切换栏位

- 鼠标点击目标栏；
- 或按 Ctrl+W，再按 `w`。

Tab 不切换栏位。由于终端把 Ctrl+I 与 Tab 编码为同一按键，Tab 执行当前栏位的“跳转历史向前”。

## 5. 阅读与移动

### 5.1 基本移动

| 按键 | 动作 |
| --- | --- |
| `h` / `j` / `k` / `l` | 左 / 下 / 上 / 右 |
| 方向键 | 同上 |
| `w` | 下一个 Vim 小写 word 的起点；单词和连续符号都可成为停点 |
| `b` | 上一个 word 的起点 |
| `e` | 当前或下一个 word 的末尾 |
| `0` / `$` | 行首 / 行尾 |
| Home / End | 行首 / 行尾 |
| `gg` / `G` | 文档开头 / 结尾 |
| `[数字]gg` | 跳到指定行，例如 `25gg` |

单独按 `g` 只等待第二个按键，不会立即跳到开头。

### 5.2 页面和视口

| 按键 | 动作 |
| --- | --- |
| Ctrl+F / Page Down | 下一页 |
| Ctrl+B / Page Up | 上一页 |
| Ctrl+D / Ctrl+U | 下一页 / 上一页 |
| Ctrl+E / Ctrl+Y | 视口向下 / 向上滚动一行 |
| `zt` / `zz` / `zb` | 把光标行放在窗口顶部 / 中间 / 底部 |

两个栏位允许向最后一行之后滚动约半屏，以便最后一行也能用 `zz` 居中。空白区域不会写入文件。

### 5.3 搜索

1. 聚焦要搜索的栏位；
2. 按 `/`；
3. 输入区分大小写的原样文本；
4. Enter 确认；
5. 使用 `n` / `N` 查找下一个 / 上一个结果。

每个栏位独立保存自己的最近搜索。Escape 关闭搜索输入而不移动光标。

### 5.4 选择和复制

- `v` 开始 Visual 选择；
- 使用移动键扩展选择；
- Shift+方向键也可直接扩展；
- 鼠标拖动可以选择；
- `y`、Ctrl+C 或 Cmd+C 复制；
- Escape 清除选择。

跨行选择和 Unicode 文本均使用实际代码点范围保存。

## 6. 创建三种标注

### 6.1 Note

快捷流程：

1. 在左栏选择非空文本；
2. 按 `a`；
3. 输入 Note 正文；
4. 提交。

也可以按 `m`，选择 **Add note**。Note 正文不能为空。

### 6.2 Highlight

1. 在左栏选择非空文本；
2. 按 `m`；
3. 选择 **Add highlight**。

Highlight 不要求正文。右栏仍保留空的 `### Note` 结构，方便普通 Markdown 工具查看和以后补充说明。

### 6.3 Bookmark

1. 在左栏取消文本选择；
2. 把光标放到需要返回的位置；
3. 按 `m`；
4. 选择 **Add bookmark**。

Bookmark 保存精确 `position`，同时保存一个最小非空文本锚点。它不是 PDF 目录中的 outline 项。

### 6.4 高亮颜色

- 普通已标注范围：黄色；
- 当前聚焦标注：蓝色；
- 重匹配候选预览：绿色；
- 右栏只有当前标注保持正常强调，其余记录弱化显示；
- 原文引用使用区别于 Note 正文的颜色，元数据使用更暗颜色。

## 7. 查看、筛选和管理标注

### 7.1 标注导航

Ctrl+N / Ctrl+P 跳到下一条 / 上一条当前可见类型的标注：

- 左栏按原文范围排序；
- 右栏按侧车顺序排序；
- 到最后或第一条时停止，不循环；
- 跳转会记录到当前栏位的跳转历史。

### 7.2 类型筛选

按 `m`，选择 **Filter annotations**，再选择：

- All；
- Notes；
- Highlights；
- Bookmarks。

筛选只改变右栏投影和相关导航/搜索范围，不改写磁盘侧车。

### 7.3 编辑当前标注

1. 把光标放到目标标注；
2. 按 `m`；
3. 选择 **Edit current annotation**；
4. 修改类型或正文并确认。

结构化修改保留稳定 ID、范围、引文和锚点。把类型改为 Note 时正文不能为空；改为 Bookmark 时程序维护 `position`。

需要编辑完整 Markdown 时，可以聚焦右栏并按 `i`。直接修改范围或锚点前应先阅读格式规范。

### 7.4 删除当前标注

1. 把光标放到目标记录；
2. 按 `m`；
3. 选择 **Delete current annotation**；
4. 检查确认窗口；
5. 确认删除。

取消不会修改文件。确认只删除目标记录，保留原文并重新编号右栏显示标题。

### 7.5 元数据折叠

右栏默认折叠元数据：

| 按键 | 动作 |
| --- | --- |
| `zi` | 全局切换元数据折叠开关 |
| `zo` | 展开当前记录元数据 |
| `zc` | 折叠当前记录元数据 |

折叠、展开和右栏编辑都尽量保持光标在窗口中的视觉位置，避免意外滚动。

## 8. 同步、对齐和跳转历史

### 8.1 三种同步状态

按 `S` 循环：Normal → Sync → Off。

| 状态 | 左栏移动影响右栏 | 右栏移动影响左栏 | `.` 手动对齐 |
| --- | --- | --- | --- |
| Normal（默认） | 是 | 否 | 可用 |
| Sync | 是 | 是 | 可用 |
| Off | 否 | 否 | 可用 |

### 8.2 手动对齐

按 `.` 对齐当前标注。若从右栏触发：

- 左栏滚动到对应原文并居中；
- 右栏当前记录也在自身栏位居中。

### 8.3 独立跳转历史

- Ctrl+O：返回较早跳转；
- Ctrl+I 或 Tab：前进到较新跳转。

两栏历史独立。`gg`、`G`、带数字的 `gg`、搜索、标注跳转和显式对齐都会记录跳转。

## 9. 编辑、撤销和保存

### 9.1 编辑原文

在 TXT/Markdown 左栏按 `i` 时，底部会出现紧凑警告窗口。必须输入区分大小写的：

```text
Yes
```

然后确认，才能进入 Edit 模式。任何其他输入都不会授权编辑原文。PDF 左栏不能编辑。

### 9.2 编辑侧车

聚焦右栏按 `i` 会直接进入 Edit 模式，不弹出原文警告。保存前 PTA 验证保留标记、ID、类型、范围、引文结构和重复 ID。

### 9.3 Edit 模式按键

| 按键 | 动作 |
| --- | --- |
| Ctrl+W | 向后删除一个 word |
| Ctrl+H | 向后删除一个字符 |
| Escape / Ctrl+`[` | 回到 Normal 模式 |
| Ctrl+S | 保存所有已修改缓冲区，但保持当前模式 |

### 9.4 撤销和重做

在 Normal 模式：

- `u`：撤销当前栏位最近一次编辑批次；
- Ctrl+R：重做当前栏位最近一次撤销。

原文和侧车拥有独立编辑历史。

### 9.5 保存

Ctrl+S 原子保存所有脏缓冲区。文本原文保存时保留：

- UTF-8 BOM 状态；
- LF、CRLF 或 CR 换行风格；
- 文件权限。

保存原文不会自动修改已有标注范围。原文变动后回到 Normal 模式并按 `R` 审查。

### 9.6 退出

按 `q` 或 Ctrl+Q：

- 没有修改：直接退出并记录光标位置；
- 有修改：选择 Save、Discard 或 Cancel；
- Save：安全保存后退出；
- Discard：丢弃内存修改后退出；
- Cancel：返回应用。

下次打开同一路径且内容指纹未变化时恢复原文光标；内容变化时不恢复，避免跳到错误位置。

## 10. 外部编辑与冲突保护

PTA 在加载时记录原文和侧车的完整磁盘字节指纹。保存前和原子替换前都会复查。

### 10.1 显式重载

按 `m`，选择 **Reload files from disk**：

- 缓冲区干净时直接重载；
- 有内存修改时提供 Cancel、Reload disk、Conflict copy + reload。

PTA 不在后台自动采用可能尚未写完的外部文件。

### 10.2 冲突选择

| 选择 | 结果 |
| --- | --- |
| Cancel | 不改变磁盘和 PTA 缓冲区 |
| Reload disk | 接受外部文件；若双方都改动，会丢弃该栏未保存缓冲区 |
| Conflict copy + reload | 先把 PTA 缓冲区保存为唯一时间戳冲突副本，再加载外部文件 |

格式错误的侧车不会覆盖原文件；修复外部文件或保存冲突副本后再重载。

## 11. 原文移动或改名

把原文和侧车一起移动且相对布局不变时通常无需操作。若原文单独改名或移动：

```console
pta-view --annotations old/path/file.annotations.md new/path/file.md
```

启动窗口显示记录路径、当前路径和基于文档哈希、范围、引文的近似兼容度。确认前：

- 可以阅读、搜索、复制和重载；
- 不允许编辑、保存、增删标注、重匹配或 PDF 同步。

确认只重写 front matter 的 `source`。若内容也发生变化，确认路径后再按 `R` 单独审查范围。

菜单中的 **Rebind source path** 只有在已经加载的托管侧车记录路径与当前原文不一致时才可用；它不是文件选择器。

## 12. 重匹配失效范围

### 12.1 开始扫描

确保缓冲区已保存并处于 Normal 模式，按 `R`。PTA 会分类：

- Healthy；
- Shifted；
- Modified；
- Ambiguous；
- Orphaned；
- Malformed。

系统依次使用原范围、唯一精确引文、规范化引文、前后文、文本相似度和相邻记录顺序。扫描不写磁盘。

### 12.2 审查候选

| 按键 | 动作 |
| --- | --- |
| `J` / `K` | 下一条 / 上一条标注 |
| Space | 接受或跳过当前候选 |
| `C` / Shift+`C` | 下一个 / 上一个候选 |
| Ctrl+S | 进入最终 Apply 确认 |

候选在左栏显示绿色预览；右栏显示保存的引文和 Note。估算值达到 90% 的候选可以预选，低于阈值或歧义候选必须由用户明确决定。

### 12.3 人工选择范围

1. 取消审查；
2. 在左栏 Visual 选择正确文本；
3. 按 `R`；
4. 导航到目标记录；
5. 按 `M` 使用当前选择。

人工选择仍会显示估算值并经过最终确认。

### 12.4 应用

最终确认继续保持双栏可见。选择 **Apply** 后 PTA：

- 复查原文和侧车磁盘指纹；
- 创建 `before-rematch` 时间戳备份；
- 更新范围、引文、文本锚点和历史；
- PDF 来源还会重建几何锚点；
- 原子写入、重新解析并在失败时回滚。

只有 Apply 写入；Cancel 和 Review 都是只读操作。

## 13. PDF 工作流

### 13.1 规范工作对

PDF 工作文件是：

```text
paper.pdf
paper.pdf.annotations.md
```

`.annotated.pdf` 只是显式输出副本，不是默认规范来源。

### 13.2 导入外部标记

```console
pta-import path/to/paper.pdf
pta-import --annotations path/to/notes.md path/to/paper.pdf
```

支持标准 `/Highlight`、`/Underline`、`/Squiggly`、`/StrikeOut`。独立便笺、绘图、印章、链接和已经扁平化的图形不导入。

无 `/Contents` 评论的外部文本标记导入为正文为空的 Highlight，不生成“Imported PDF highlight...”之类占位句。

### 13.3 将侧车嵌入 PDF

```console
pta-embed path/to/paper.pdf
pta-embed --annotations path/to/notes.md path/to/paper.pdf
pta-embed --output path/to/review-copy.pdf path/to/paper.pdf
```

默认原子更新源 PDF。使用 `--output` 时才创建独立副本；已有输出只有在同时给出 `--force` 时替换。

### 13.4 保存时同步

PDF 侧车成功保存后默认立即同步源 PDF，包括：

- 新增或删除标注；
- 编辑侧车后 Ctrl+S；
- 应用确认重匹配；
- 退出时选择 Save。

禁用本次会话自动同步：

```console
pta-view --no-embed-on-save path/to/paper.pdf
```

菜单中的 **Synchronize annotations into source PDF** 仍可以手动执行。

### 13.5 PDF/Markdown 冲突

打开 PDF 时会先协调两侧。典型冲突包括：

- PDF 删除、Markdown 保留：Restore PDF 或 Delete MD；
- Markdown 删除、PDF 保留：Delete PDF 或 Restore MD；
- 同一记录两侧不同：Use Markdown 或 Use PDF。

冲突没有默认决定，最终应用前再次显示影响，并创建备份。取消后按 `R` 可以重新打开。

### 13.6 PDF 提取缓存

缓存按完整 PDF SHA-256、缓存版本、提取 profile 和 `pdfplumber` 版本命中。任一 PDF 字节变化都会重新提取。

```console
pta-view --no-pdf-cache paper.pdf
pta-view --rebuild-pdf-cache paper.pdf
```

缓存包含提取文字，应作为私有数据处理；删除缓存不会损坏 PDF 或侧车。

## 14. `m` 操作菜单

Normal 模式按 `m` 打开菜单。默认选中 **Add note**。主要项目如下：

- Add note；
- Add highlight；
- Add bookmark；
- Filter annotations；
- Edit current annotation；
- Enter Edit mode；
- Switch Sync mode；
- Delete current annotation；
- Rematch annotations；
- Show keybindings；
- Synchronize annotations into source PDF；
- Reload files from disk；
- Rebind source path。

灰色项目表示当前来源、栏位或状态不满足条件，不代表功能缺失。

## 15. 示例与人工验收

示例总入口见 [examples/README.md](examples/README.md)。推荐顺序：

1. `examples/annotation_types/`：查看三种类型的规范侧车；
2. `examples/rematch_cases/`：体验四类重匹配及内置复位；
3. `acceptance/manual/generated/`：查看已经完成的 13 项人工验收和 20 张截图；
4. `acceptance/run_acceptance.py`：复现 120 例准确率和性能测试。

重新生成人工练习时不要覆盖已归档证据，使用新目录：

```console
.venv/bin/python acceptance/generate_manual_acceptance.py \
  --output acceptance/manual/run-YYYYMMDD
```

## 16. 常见问题

### 16.1 Rebind 为什么是灰色？

当前加载侧车的 `source` 已与原文一致，或没有显式加载真正需要重新绑定的旧侧车。先使用 `--annotations` 指定旧侧车。

### 16.2 外部编辑后为什么不能保存？

磁盘指纹已变化，PTA 正在阻止覆盖外部编辑。使用 Reload，或先保存 conflict copy 再重载。

### 16.3 为什么按 `R` 后没有自动写入？

扫描和候选审查有意保持只读。必须选择候选、按 Ctrl+S 进入最终确认，再选择 Apply。

### 16.4 为什么短 Bookmark 置信度较低？

单字符或短引文可能在文档中重复，文本证据较弱。即使位置看起来正确，也应检查上下文并明确按 Space 选择。

### 16.5 为什么 PDF 修改后锚点失效？

PDF 几何锚点绑定完整 PDF 字节哈希。先导入并协调外部 PDF 修改，再审查文字范围或重新生成几何锚点。

### 16.6 格式验证失败怎么办？

PTA 不会覆盖磁盘原文件。使用普通编辑器检查错误记录的：

- 起止 marker；
- marker ID 与 YAML ID；
- `type`；
- `range.start/end`；
- Bookmark `position`；
- `### Original text` blockquote；
- `### Note` 和结束 marker。

详细规则见格式规范。

## 17. 建议的安全工作流

1. 用默认侧车名或明确的 `--annotations` 打开文件；
2. 创建 Note、Highlight 或 Bookmark；
3. Ctrl+S 保存；
4. 外部编辑后主动 Reload，不直接覆盖冲突；
5. 原文变化后按 `R`，逐条检查候选；
6. 只有确认无误才 Apply；
7. PDF 修改先导入协调，再向 PDF 写回；
8. 把原文、侧车和自动生成备份一起纳入版本控制或定期备份。
