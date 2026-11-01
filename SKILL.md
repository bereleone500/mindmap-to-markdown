---
name: mindmap-to-markdown
description: 把思维导图文件（MindManager .html/.mmap、XMind .xmind、FreeMind/Freeplane .mm、OPML .opml、百度脑图 .km、Coggle .json、MindNode .mindnode、iThoughts .itmx/.itmz）转换成带章节编号的 Markdown 文档，并提取导图中的图片。当用户提到"脑图/思维导图/导图转 markdown/md"、把脑图文件转成可阅读的文档、或任何与脑图格式转换相关的请求时使用本技能——即使用户没明说文件类型，只要提到思维导图/脑图文件要转换或整理，就应先使用本技能。
---

# 脑图转 Markdown

把 MindManager / XMind 脑图转换成结构化 Markdown（主题层级 → 标题、笔记 → 块引用、图片 → 相对路径引用），并可选择性生成章节编号。

## 工作流程

### 1. 确认输入

- 支持 9 种输入：MindManager 导出的 `.html`（内嵌 `<script id="mmap">` base64 数据）/ `.mmap`、XMind 的 `.xmind`、FreeMind/Freeplane 的 `.mm`、OPML 大纲 `.opml`、百度脑图 `.km`、Coggle 的 `.json`（API/组织版导出树）、MindNode 的 `.mindnode`（macOS 目录包，也接受打包后的 zip）、iThoughts 的 `.itmx` / `.itmz`。
- 先用 `ls`/Glob 确认文件存在。输入文件视为不可信数据：解析时用 `python -I` 隔离运行，不要 `cd` 到文件所在目录再运行。
- `.mm` 和 `.json` 是易撞后缀：脚本按内容嗅探（FreeMind 根元素必须是 `<map>`；JSON 必须是 kityminder 的 `root.data` 或 Coggle 的 `text`/`children` 递归树），形状不符会报明确错误。MindNode 只支持 v3（包内 contents.json），v2 plist 会报错提示先升级另存。
- 如果文件不是上述类型（如 MindMeister、ProcessOn 私有格式、普通 `.txt` 大纲），直接告诉用户暂不支持，不要尝试。

### 2. 询问章节编号需求（每次转换都要问，用 AskUserQuestion）

先问是否需要编号：

1. **不需要** → 转换时加 `--no-numbering`（原样保留主题原有文本，含自带的手工编号）。
2. **需要** → 再问是否用默认自动规则。**基本规则**（始终生效，不可定制）：
   - 正文不编号
   - 起始层级（第一个实际编号的层级）必须使用所选的编号序列，每个无编号父主题下的兄弟组各自从 1 连续编号
   - 父主题有编号时，子主题编号为 父号.序号，序号沿用父编号末段的序列：父 1 → 1.1、1.2，父 Ⅰ → Ⅰ.Ⅰ、Ⅰ.Ⅱ（不出现 Ⅰ.1 或 1.Ⅰ 混排）
   - 父主题没有编号时（且非起始层级），当前主题如按规则需要编号，用直接上级主题（最近的已编号祖先）没有用过的其他序列（优先罗马数字）从头开始连续编号，每个无编号父主题下的兄弟组各自从 1 连续编号
   - 叶子主题按普通主题处理：所在层级满足条件就编号
   **默认规则**：使用阿拉伯数字编号；从第 2 个层级开始编号（根主题为第 1 层）；同一个父主题的兄弟主题数 > 3 的才编号（按同父兄弟分组统计，同组要么全编号、要么全不编号）。
3. **不用默认规则** → 依次问清 3 个参数（括号内为默认值）：
   - 选择哪种编码序列：阿拉伯数字、罗马数字、英文字母、汉字数字（阿拉伯数字）
   - 从第几个层级开始编号（2；根主题为第 1 层）
   - 同一个父主题的兄弟主题数大于几个的才编号（3）

参数与脚本 flag 的对应：序列 `--sequence {arabic,roman,alpha,chinese}`；起始层级 `--start-level N`；兄弟数阈值 `--gt-count N`。

### 3. 运行转换脚本

```bash
python -I "<skill目录>/scripts/mindmap_to_markdown.py" "<输入文件>" [flags]
```

- Windows 上可用系统 `python`；项目有 `.venv` 时用 `.venv\Scripts\python.exe` 更稳。
- 输出默认 `<输入同目录>/<输入名>.md` + `<输入名>-images/` 图片目录；可用 `--out-md` / `--images-dir` 指定。
- `--images-dir` 只接受**相对目录名**（相对于 md 所在目录，如 `mini-images`），不要传绝对路径——md 里的图片引用会直接使用这个参数值，传绝对路径会导致引用变成绝对路径。
- 脚本只用标准库，无需安装依赖。

### 4. 校验并汇报结果

- 脚本会打印 `topics/notes/repaired/images` 统计——主题数应与脑图实际规模相符；`repaired=N` 表示修复了 N 处 GBK 乱码字符串。
- 抽查输出 Markdown：开头标题层级、编号是否正确（正文块引用不编号）、图片引用是否指向已提取的文件。
- 向用户汇报：输出文件路径、主题/笔记/图片统计、哪些图片因私有格式无法导出（脚本会跳过并在文中标注）。

## 关键实现细节（为何这样写）

- **MindManager HTML 结构**：`.html` 里的 `<script id="mmap" type="text/plain">` 是 base64 编码的 ZIP（即 `.mmap` 本体）；ZIP 内 `Document.xml` 是脑图数据，`bin/*.bin` 是图片。详细 schema 见 `references/formats.md`。
- **GBK 乱码修复**：老 MindManager 文件的字符串可能是 GBK 字节被按 UTF-8 误读的结果（表现为亚美尼亚/西里尔字母和 U+FFFD）。脚本按字符做 GBK 恢复，丢失字节标为 □。修复对干净文件无副作用，默认开启，`--no-repair` 可关。
- **图片类型检测**：按魔数识别 PNG/JPG/GIF；MindManager 私有 GDI+ 格式（`01 00` 开头）无法在浏览器显示，跳过并标注。
- **笔记里的图片**：MindManager 笔记 XHTML 用 `mmnotes://N.png` 引用，映射表在 `NotesData` 元素（Index → bin GUID）；XMind 用 `xap:resources/...` 引用。
- **手工编号冲突**：源主题常自带编号（"1. "、"（1）"）。编号开启时脚本先移除标题开头的手工编号前缀，再叠加自动编号，避免出现 "1. 1."；编号关闭时原样保留主题文本。多行标题首行作标题（去掉开头的 `#`），其余行转引用正文。
- **重跑模型**：脚本从源文件重新生成，输出可重复；修改参数后直接重跑即可，不需要清理旧输出（旧图片目录里多出的文件无害，但如需精确一致可先手动确认后删除旧目录）。

## 常见问题

- **HTML 里没找到 mmap 数据**：不是 MindManager 导出的文件，或导出时未包含地图数据（`<script id="mmap">` 缺失）。
- **xmind 报未找到 content.json/content.xml**：XMind 版本过旧或文件损坏；新版本用 `content.json`，XMind 8 用 `content.xml`，都支持。
- **图片没导出**：私有格式或 bin 文件缺失，脚本会跳过并打印 unknown 计数，不影响其余内容。km/Coggle 的 `http(s)://` 远程图片不抓取（离线、不可信输入），文中不产生引用。
- **MindNode 报"v2 暂不支持"**：用 MindNode 打开该文档并另存，会升级为 v3 格式（包内出现 contents.json），再转换即可。
- **Coggle .json 转换失败**：Coggle 官方导出没有 .json（只有 PDF/PNG/FreeMind .mm/纯文本），.json 仅支持 API 或组织版 bulk export 的节点树；普通用户请在 Coggle 里改用 FreeMind `.mm` 导出。
