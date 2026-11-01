# 脑图文件格式参考

排查转换问题时读本文件。三种格式解析后的中间表示一致：`{oid, title, children, notes, images, task_pct, callout, num}`，其中 notes 为文本行列表（图片以 `@@IMG:key@@` 占位）。

## MindManager（.html 导出 / .mmap）

### HTML 导出结构

```html
<script id="mmap" type="text/plain">UEsDBAA...  ← base64 编码的 ZIP（.mmap 本体）</script>
<script id="mmap-name" type="text/plain">xxx.mmap</script>
<script id="app" type="text/plain">UEsDB...  ← 导出器的查看器代码，不是地图数据，忽略</script>
```

### .mmap = ZIP

- `Document.xml` — 脑图数据（6MB 级，单行 XML），namespace：
  - `ap` = `http://schemas.mindjet.com/MindManager/Application/2003`
  - `cor` = `http://schemas.mindjet.com/MindManager/Core/2003`
- `bin/<GUID>.bin` — 图片等二进制（数量可达上千，只提取被引用的）
- `PDFPreview.pdf`、`Preview.png` — 预览图，无文本价值，忽略
- `xsd/` — schema 定义，忽略

### Document.xml 要点

- 主题树：`ap:OneTopic/ap:Topic` 为中心主题；每个 `ap:Topic` 含：
  - `ap:Text PlainText="标题"`（标题在 **PlainText 属性**上，元素体只有 Font 样式；`&#xA;` 是标题内换行，转成 " / "）
  - `ap:SubTopics/ap:Topic` — 子主题
  - `ap:NotesGroup` — 笔记：`ap:NotesXhtmlData` 里是 `<html xmlns=...>` XHTML（表格只是单格样式容器，按块级标签换行即可）；`ap:NotesData Index="N"` + `cor:Uri = mmarch://bin/GUID.bin` 把笔记里 `<img src="mmnotes://N.png">` 映射到 bin 文件（**N 是每个主题内局部编号，不是全局**）
  - `ap:Task TaskPercentage="100"` — 任务进度
  - `ap:CalloutTopic` — 标注气泡
  - `ap:OneImage/ap:Image/ap:ImageData/cor:Uri` + `ap:ImageSize` — 主题图片；宽高 < 100×50 的多为装饰图标，跳过
- 关联关系：`ap:Relationships/ap:Relationship`（注意：**没有 FromOId/ToOId 属性**），端点在其 `ap:ConnectionGroup/ap:Connection/ap:ObjectReference OIdRef=...` 里（两个 ConnectionGroup = 两端，顺序即方向）；连线标签在其 `ap:FloatingTopics/ap:Topic/ap:Text` 里
- 根级浮动主题：`ap:FloatingTopics`，父元素不是 `ap:Relationship` 的才是（Relationship 里的是连线标签）
- ET 序列化笔记 XHTML 时会加 `ns0:` 或 `html:` 前缀，正则剥掉：`(</?)(?:ns\d+|html):`

### 编码陷阱（重要）

老 MindManager 文件（尤其中文 Windows 上编辑过的）部分字符串是 **GBK 字节被按 UTF-8 误读**的乱码：

- 表现为亚美尼亚/西里尔/希腊字母（GBK 双字节凑巧构成合法 2 字节 UTF-8，如 "远"=GBK D4B6 → U+0536），无效序列变成 U+FFFD（字节已永久丢失）
- 修复：无 CJK 且含非 ASCII 的字符串按字符 `ch.encode('utf-8').decode('gbk')` 恢复（仅 0x80-0x7FF 的 2 字节字符），U+FFFD → □
- 含中文的字符串是干净的，跳过修复；纯 ASCII 跳过
- 注意：这类文件混存干净和损坏的字符串（新旧内容），必须逐字符串判断

## XMind（.xmind = ZIP）

### 新版（XMind Zen）：content.json

```json
[{ "id": "sheet1", "class": "sheet", "title": "Sheet 1",
   "rootTopic": { "id": "root", "class": "topic", "title": "中心主题",
     "notes": { "plain": { "content": "纯文本" } },        // 或 "html" / "realHTML"
     "image": { "src": "xap:resources/xxx.png" },           // zip 内路径 = 去掉 xap: 前缀
     "children": { "attached": [ { ...topic... } ], "detached": [...] } },
   "relationships": [ { "id": "r1", "end1Id": "t1", "end2Id": "t2", "title": "标注" } ] }]
```

- 一个 sheet = 一篇；多 sheet 输出到同一个 md，用 `---` 分隔
- notes 三种形态：`plain`（纯文本）、`html`、`realHTML`（都是 XHTML）
- 图片 src `xap:resources/...` 或 `xap:attachments/...`，zip 内同名条目
- children 里 class 为 "summary" 的是概要节点，跳过

### 旧版（XMind 8）：content.xml

```xml
<xmap-content xmlns="urn:xmind:xmap:xmlns:content:2.0">
  <sheet id="..."><topic id="root"><title>中心主题</title>
    <children><topics type="attached"><topic id="t1"><title>...</title></topic></topics></children>
    <notes><plain>纯文本</plain></notes>   <!-- 或 <html>XHTML</html> -->
  </topic>
  <relationships><relationship end1="t1" end2="t2"><title>标注</title></relationship></relationships>
  </sheet>
</xmap-content>
```

- 标题在 `<title>` 元素（itertext 拼接）
- 无任务进度概念（task 仅 MindManager 有）

## FreeMind / Freeplane（.mm）

纯 XML 文件，根元素必须是 `<map>`（嗅探依据，否则报"不是 FreeMind/Freeplane 脑图"）：

```xml
<map version="1.0.1">
  <node ID="n1" TEXT="标题">
    <node ID="n2" TEXT="子主题" POSITION="left|right"/>
    <richcontent TYPE="NODE"><html>…<img src="data:image/png;base64,…"/>…</html></richcontent>
    <richcontent TYPE="NOTE"><html>…笔记 XHTML…</html></richcontent>
    <arrowlink DESTINATION="目标节点ID"/>
  </node>
</map>
```

- 标题：`TEXT`（或 `text`）属性，实体换行保留多行；无 TEXT 时取 `richcontent TYPE="NODE"`（文本行作标题、其中 data URL 图片登记为主题图片）
- 笔记：`richcontent TYPE="NOTE"`（HTML → 块引用，图片内嵌通道）
- 图片：richcontent 内 `<img src="data:image/…;base64,…">`（必须整体替换 img 标签为占位符，否则 marker 留在属性里被剥标签时删掉）
- 关联：`arrowlink DESTINATION` → 关联关系章节（labels 空）；图标/样式/hook 不导入
- `POSITION` 左右布局忽略，按文档顺序输出

## OPML（.opml）

纯 XML 大纲，根元素 `<opml>`、`<body>` 下嵌套 `<outline>`：

- 标题：`text` 属性（兜底 `title`/内文本）；含 HTML 标签时剥掉，`<br>` 转行
- 笔记：`_note` 属性（HTML 或纯文本）→ 块引用
- 无图片/关联概念；多个平级根 outline 时第一个作中心主题、其余挂为其子分支

## 百度脑图 kityminder（.km）

纯 UTF-8 JSON（schema 以官方导出源码为准，高置信）：

```json
{"root": {"data": {"text": "…", "note": "<div>…</div>",
                    "image": {"url": "data:image/png;base64,…", "size": {…}},
                    "priority": 1, "progress": 0, "id": "guid"},
          "children": [{"data": {…}, "children": []}]},
 "template": "default", "theme": "classic", "version": "1.4.43"}
```

- 层级：完全嵌套（无 parentID）
- 标题 `data.text`（内嵌 HTML 剥标签）；笔记 `data.note`（HTML）
- 图片 `data.image.url`：`data:` URL 走内嵌通道；`http(s)://` 不抓取（离线、不可信输入），不产生引用
- `priority`/`progress` 不导入（九级优先级 ≠ MindManager 百分比，无忠实映射）

## Coggle（.json）

**置信度说明**：Coggle 官方导出菜单没有 .json（只有 PDF/PNG/FreeMind .mm/纯文本）；.json 只来自 API 或组织版 bulk export，按官方 API 文档确认的节点树结构做防御式解析：

```json
{"text": "节点文本", "id": "…", "offset": {"x": 100, "y": 0},
 "children": [ /* 递归同形 */ ]}
```

- 标题 `text`（markdown 风格字符串，`<br>` 转行）；层级 = `children` 递归
- 无笔记概念；`offset`/颜色等布局信息忽略
- JSON 嗅探：根对象有 `root.data` → km；有 `text`/`children` → Coggle；都不满足报"不是受支持的脑图 JSON"。`.km`/`.json` 后缀互认（按嗅探结果解析）

## MindNode（.mindnode）

macOS 目录包（输入目录路径即可；也接受把包内容打成 zip）。**仅支持 v3**（包内 `contents.json`）；检测到 `*.plist`（v2 NodeStorev2）时报错提示先用 MindNode 另存升级为 v3：

```json
{"metadata": {"formatVersion": 3},
 "rootNodeID": "UUID",
 "nodes": {"UUID": {"title": "…", "subtopics": ["子UUID", …],
                    "notes": {"text": "…"},
                    "attachments": [{"type": "image",
                                     "value": {"fileName": "a.png", "url": "file://attachments/a.png"}}]}},
 "connections": {"UUID": {"endA": "…", "endB": "…", "label": "…"}}}
```

- 层级：UUID 字典 + `subtopics` 子 ID 列表（visited 集合防引用环、缺引用跳过）；无 rootNodeID 时取第一个未被引用为 subtopic 的节点
- 笔记 `notes.text`（`<br>`/换行 → 块引用）
- 图片：`attachments[type=image]` 的 `value.url`/`fileName` 相对包内路径（`file://` 前缀剥掉、unquote；resolve 后必须在包内，防路径穿越）→ 读字节走内嵌通道
- 关联：`connections {endA, endB, label}` → 关联关系章节

## iThoughts（.itmx / .itmz）

zip 包，内含 `map.xml`（XML plist）；`.itmz` 是外层 zip 套一个 `.itmx`（代码递归找 map.xml）。schema 来自 toketaWare 官方存档文档（高置信）：

- plist 结构：`mindmap` → `topics`（Topic dict 嵌套）
- Topic 字段：`uuid`、`text`（标题，`<br>` 转行）、`note`（笔记）、`children`（嵌套子 Topic）、`parents`（忽略，树由 children 决定）、`attachments`
- 图片：`attachments [{"type": "image", "data": <base64 plist data>, "extension": "png"}]` → 内嵌通道（以魔数为准）
- 关联：`attachments [{"type": "relationship", "start-point"/"end-point"}]` → 尽力而为产出（字段名版本差异，可能为空）
- 根 = topics 中第一个未出现在任何 `parents` 里的 Topic；其余顶层 Topic 挂为其子分支

## 章节编号算法（number_tree）

基本规则 + 默认/定制参数的并集：

1. 按同父兄弟分组统计兄弟主题数；内部深度从根的直接子主题（第 2 层）起算为 1
2. 可编号条件：`用户层级 >= start_level 且 同父兄弟主题数 > gt_count`（按同父兄弟
   分组统计，同组要么全编号、要么全不编号）
3. 正文（多行标题的续行、笔记）不编号
4. 不满足条件的同父兄弟组整体不编号，不部分编号
5. 编号赋值：起始层级（第一个出现可编号主题的层级，DFS 先序）用所选序列，按无编
   号父主题分组各自从 1 连续编号；父主题有编号 → 父号.兄弟序号（只数被编号的兄弟，
   连续无跳号，序号沿用父编号末段的序列：父 1 → 1.1，父 Ⅰ → Ⅰ.Ⅰ）；父主题没有
   编号且非起始层级 → 该兄弟组用直接上级主题（最近的已编号祖先）没用过的其他序列
   （优先罗马数字，其次阿拉伯、字母、汉字）从头连续编号，各自从 1 重起
6. 序列渲染（`--sequence`）：
   - `arabic`：1, 2, 3…（如 `1.` `1.1`）
   - `roman`：I, II, III, IV…（如 `I.` `I.II`）
   - `alpha`：A, B, … Z, AA…（如 `A.` `A.B`）
   - `chinese`：一, 二, … 十, 十一…（如 `一.` `一.一`）
7. 编号开启时先移除标题开头的手工编号（`1. `、`（1）`、`1)` 等），避免
   "1. 1." 叠号；编号关闭时保留主题原文

## 图片提取

统一走两条通道，落盘都由 `resolve_images()` 收口、`sniff_ext()` 魔数检测（PNG(89 50 4E 47)/JPG(FF D8)/GIF(47 49 46 38)；`01 00 00 00` 开头的是 MindManager 私有 GDI+ 格式，跳过）：

- **zip 条目通道**（`IMG_SRC[key] = (zip内路径, …)`）：MindManager bin/GUID.bin、XMind resources/attachments 条目；zip 需保持打开到图片落盘
- **内嵌字节通道**（`IMG_DATA[key] = bytes`）：FreeMind richcontent 与 km 笔记里的 `data:image/…;base64,` img、km `data.image.url` 的 data URL、iThoughts 附件 base64、MindNode 包内附件文件——按内容 sha1 去重，key 形如 `fm:/km:/mn:/itx:<hash>`，与 zip 通道共用同一套魔数检测与重名加 `_2` 规则
- 输出文件名：MindManager 用 GUID；XMind 用资源文件名；内嵌通道用 `前缀_hash`（重名自动加 _2 后缀）
