#!/usr/bin/env python3
"""脑图转 Markdown：支持 MindManager（.html / .mmap）、XMind（.xmind）、
FreeMind/Freeplane（.mm）、OPML（.opml）、百度脑图 kityminder（.km）、
Coggle（.json，API/组织版导出树）、MindNode（.mindnode 包或其 zip）、
iThoughts（.itmx / .itmz）。

仅用标准库（xml.etree / zipfile / json / plistlib / re）。
输入文件视为不可信数据，建议用 `python -I` 运行本脚本。

用法：
    python -I mindmap_to_markdown.py <输入文件> [选项]

选项（编号参数，全部可选，默认值 = 默认规则）：
    --no-numbering            不编号（纯转换，保留主题原有文本与编号）
    --sequence S              编号序列：arabic / roman / alpha / chinese
                              （默认 arabic 阿拉伯数字；罗马数字、英文字母、
                              汉字数字）
    --start-level N           从第 N 个层级开始编号，根主题为第 1 层（默认 2）
    --gt-count N              同一个父主题的兄弟主题数 > N 的才编号（默认 3）
    --no-repair               关闭 GBK 乱码修复
    --out-md PATH             输出 Markdown 路径（默认与输入同名 .md）
    --images-dir NAME         图片输出目录名（默认 <输出名>-images）

输出：
    <输入同目录>/<输入名>.md + <输入名>-images/（提取的图片）

编号开启时，主题标题开头的手工编号（如 "1. "、"（1）"）会被移除，避免与
自动编号重复出现 "1. 1."；编号关闭时保留主题原有文本；含换行的标题首行作
标题、其余行作为引用正文输出。
"""
import argparse
import base64
import hashlib
import html
import io
import json
import plistlib
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from urllib.parse import unquote

AP = "{http://schemas.mindjet.com/MindManager/Application/2003}"
COR = "{http://schemas.mindjet.com/MindManager/Core/2003}"
XMAP = "{urn:xmind:xmap:xmlns:content:2.0}"

MM_IMG_RE = re.compile(r'<img[^>]*src="mmnotes://(\d+)\.png"[^>]*/?>')
XAP_IMG_RE = re.compile(r'src="(xap:[^"]+)"')
DATA_URL_RE = re.compile(r"data:image/[\w.+-]+;base64,([A-Za-z0-9+/=]+)")
# 整个 <img src="data:…"> 标签（必须整体替换，否则 marker 留在属性里被剥标签时删掉）
IMG_DATA_URL_RE = re.compile(
    r'<img[^>]*\ssrc=["\']data:image/[\w.+-]+;base64,([A-Za-z0-9+/=]+)["\'][^>]*/?>')
MARK_RE = re.compile(r"@@IMG:([^@]+)@@")
TAG_RE = re.compile(r"<[^>]+>")
# 标题开头的手工编号（"1. "、"（1）"、"1)"）与 markdown 标题符（"## "）
MANUAL_NUM_RE = re.compile(r"^\s*(?:\d{1,3}[.、．]\s*|[（(]\d{1,3}[)）]\s*|\d{1,3}\)\s*)")
HEAD_HASH_RE = re.compile(r"^\s*#+\s+")

# key -> (zip 内路径, 真实扩展名或 None, 输出文件名基础)
IMG_SRC = {}
# key -> 内嵌图片字节（FreeMind/km/iThoughts/MindNode 的 base64 图片走此通道）
IMG_DATA = {}
IMG_PREFIX = ""   # 输出 md 中图片引用的目录前缀（convert() 时设置）
stats = {"topics": 0, "notes": 0, "repaired": 0}


# ---------------- 乱码修复 ----------------
def repair(s: str) -> str:
    """部分老 MindManager 文件的字符串是 GBK 字节被按 UTF-8 误读产生的乱码。

    含中文的字符串视为正常原样返回；无中文且含非 ASCII 的字符串按字符做
    GBK 恢复（仅恢复 2 字节 UTF-8 字符 = 原 GBK 双字节），
    U+FFFD（已丢失字节）用 □ 标记。
    """
    if not s:
        return s
    if any("一" <= c <= "鿿" for c in s):
        return s
    if s.isascii():
        return s
    stats["repaired"] += 1
    out = []
    for ch in s:
        if ch == "�":
            out.append("□")
            continue
        o = ord(ch)
        if 0x80 <= o <= 0x7FF:
            try:
                out.append(ch.encode("utf-8").decode("gbk"))
                continue
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
        out.append(ch)
    return "".join(out)


def html_to_lines(x: str) -> list:
    """XHTML 笔记内容 -> 文本行（<br>/</p>/</tr>/</div>/</li> 处换行）。"""
    x = re.sub(r"<br\s*/?>", "\n", x)
    x = re.sub(r"</(p|tr|table|div|li|h\d)>", "\n", x)
    x = TAG_RE.sub("", x)
    x = html.unescape(x)
    lines = []
    for raw in x.split("\n"):
        line = re.sub(r"[ \t ]+", " ", raw).strip()
        if line:
            lines.append(line)
    return lines


def clean_title(s: str, do_repair: bool) -> str:
    return repair(s) if do_repair else s


def new_node(oid=None):
    return {"oid": oid, "title": "", "children": [], "notes": None,
            "images": [], "task_pct": None, "callout": None, "num": None}


def reg_inline_image(prefix: str, data: bytes) -> str:
    """登记内嵌图片字节（按内容去重），返回 key；IMG_SRC[0] 用占位符。"""
    key = f"{prefix}:{hashlib.sha1(data).hexdigest()[:16]}"
    IMG_SRC[key] = ("<inline>", None, key)
    IMG_DATA[key] = data
    return key


def inline_data_urls(x: str, prefix: str) -> str:
    """把 HTML 里的 <img src="data:…"> 整个标签替换为 @@IMG:key@@ 占位。"""
    return IMG_DATA_URL_RE.sub(
        lambda m: f"\n@@IMG:{reg_inline_image(prefix, base64.b64decode(m.group(1)))}@@\n", x)


def local_tag(el) -> str:
    """剥掉命名空间的标签名（新格式 XML 的防御式解析用）。"""
    return el.tag.rpartition("}")[2] if isinstance(el.tag, str) else ""


def parse_xml_root(data: bytes, what: str) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError as e:
        raise ValueError(f"文件不是合法 XML（{what}）：{e}")


def rich_html_lines(el, prefix: str) -> list:
    """XHTML 片段元素 -> 文本行（剥 ns 前缀，data URL 图片转占位符）。"""
    x = ET.tostring(el, encoding="unicode")
    x = re.sub(r"(</?)(?:ns\d+|html):", r"\1", x)
    return html_to_lines(inline_data_urls(x, prefix))


def html_note_lines(note: str, prefix: str) -> list:
    """HTML/纯文本笔记字符串 -> 文本行（data URL 图片转占位符）。"""
    note = inline_data_urls(html.unescape(note), prefix)
    if "<" in note:
        return html_to_lines(note)
    return [ln.strip() for ln in note.split("\n") if ln.strip()]


def strip_inline_html(raw: str) -> str:
    """标题里内嵌的 HTML：剥标签，<br> 转换行。"""
    raw = re.sub(r"<br\s*/?>", "\n", raw)
    if "<" in raw:
        raw = TAG_RE.sub("", html.unescape(raw))
    return raw


# ---------------- MindManager 解析 ----------------
def mm_parse(z: zipfile.ZipFile, do_repair: bool):
    """返回 (central, rels)；central 带 'floats' 字段。"""
    tree = ET.fromstring(z.read("Document.xml"))
    parent_map = {c: p for p in tree.iter() for c in p}

    def reg_image(guid):
        IMG_SRC[guid] = (f"bin/{guid}.bin", None, guid)
        return guid

    def parse_topic(el):
        stats["topics"] += 1
        node = new_node(el.get("OId"))
        for child in el:
            tag = child.tag
            if tag == AP + "Text":
                node["title"] = child.get("PlainText") or "".join(child.itertext()).strip()
                node["title"] = html.unescape(node["title"]).strip()
            elif tag == AP + "SubTopics":
                for sub in child:
                    if sub.tag == AP + "Topic":
                        node["children"].append(parse_topic(sub))
            elif tag == AP + "NotesGroup":
                img_map = {}
                xhtml = ""
                for ng in child:
                    if ng.tag == AP + "NotesData":
                        uri = ng.find(f"{COR}Uri")
                        if uri is not None and uri.text:
                            m = re.search(r"bin/([0-9A-F-]+)\.bin", uri.text)
                            if m:
                                img_map[ng.get("Index", "")] = m.group(1)
                for ng in child:
                    if ng.tag == AP + "NotesXhtmlData" and len(ng) and not xhtml:
                        inner = ET.tostring(list(ng)[0], encoding="unicode")
                        inner = re.sub(r"(</?)(?:ns\d+|html):", r"\1", inner)
                        xhtml = inner
                if xhtml:
                    def img_repl(mo):
                        g = img_map.get(mo.group(1))
                        return f"\n@@IMG:{reg_image(g)}@@\n" if g else "\n"
                    node["notes"] = html_to_lines(MM_IMG_RE.sub(img_repl, xhtml))
                    stats["notes"] += 1
            elif tag == AP + "Task":
                if child.get("TaskPercentage"):
                    node["task_pct"] = child.get("TaskPercentage")
            elif tag == AP + "CalloutTopic":
                t = child.find(f"{AP}Text")
                if t is not None:
                    node["callout"] = t.get("PlainText") or ""
            elif tag == AP + "OneImage":
                img = child.find(f"{AP}Image")
                if img is not None:
                    uri = img.find(f"{AP}ImageData/{COR}Uri")
                    size = img.find(f"{AP}ImageSize")
                    w = h = 0.0
                    if size is not None:
                        w = float(size.get("Width", 0) or 0)
                        h = float(size.get("Height", 0) or 0)
                    if uri is not None and uri.text and (w >= 100 or h >= 50):
                        m = re.search(r"bin/([0-9A-F-]+)\.bin", uri.text)
                        if m:
                            node["images"].append(reg_image(m.group(1)))
        if do_repair:
            node["title"] = repair(node["title"])
            if node["callout"]:
                node["callout"] = repair(node["callout"])
        # 标题保留换行，生成时首行作标题、其余行作引用正文
        return node

    one = tree.find(f"{AP}OneTopic")
    central = parse_topic(one.find(f"{AP}Topic"))

    # 根级浮动主题（排除 Relationship 内部的连线标签）
    floats = []
    for ft in tree.iter(f"{AP}FloatingTopics"):
        if parent_map.get(ft) is not None and parent_map[ft].tag == AP + "Relationship":
            continue
        for t in ft:
            if t.tag == AP + "Topic":
                floats.append(parse_topic(t))
    central["floats"] = floats

    rels = []
    rel_el = tree.find(f"{AP}Relationships")
    if rel_el is not None:
        for r in rel_el.findall(f"{AP}Relationship"):
            ends = []
            for cg in r.findall(f"{AP}ConnectionGroup"):
                for c in cg.findall(f"{AP}Connection"):
                    ref = c.find(f"{AP}ObjectReference")
                    if ref is not None:
                        ends.append(ref.get("OIdRef"))
            labels = []
            ft = r.find(f"{AP}FloatingTopics")
            if ft is not None:
                for tp in ft.findall(f"{AP}Topic"):
                    t = tp.find(f"{AP}Text")
                    if t is not None:
                        lab = t.get("PlainText") or ""
                        if do_repair:
                            lab = repair(lab)
                        if lab:
                            labels.append(lab)
            rels.append({"from": ends[0] if ends else None,
                         "to": ends[1] if len(ends) > 1 else None,
                         "labels": labels})
    return central, rels


# ---------------- XMind 解析 ----------------
def xmind_parse(z: zipfile.ZipFile, do_repair: bool) -> list:
    """返回 [(sheet_title, central, rels), ...]；每个 sheet 一篇。"""
    names = z.namelist()

    def reg_xap(src):
        zipname = src[len("xap:"):]
        IMG_SRC[src] = (zipname, None, Path(zipname).stem)
        return src

    if "content.json" in names:
        data = json.loads(z.read("content.json").decode("utf-8"))
        sheets = data if isinstance(data, list) else [data]

        def j_node(t):
            stats["topics"] += 1
            node = new_node(t.get("id"))
            node["title"] = clean_title(t.get("title") or "", do_repair)
            ch = t.get("children") or {}
            for k in ("attached", "detached"):
                for c in ch.get(k) or []:
                    if isinstance(c, dict) and c.get("class") in (None, "topic"):
                        node["children"].append(j_node(c))
            notes = t.get("notes") or {}
            text = None
            if isinstance(notes, dict):
                for kind in ("plain", "html", "realHTML"):
                    v = notes.get(kind)
                    if isinstance(v, dict) and v.get("content"):
                        text = v["content"]
                        break
            if text:
                def img_repl(mo):
                    return f"\n@@IMG:{reg_xap(mo.group(1))}@@\n"
                if "<" in text:
                    node["notes"] = html_to_lines(XAP_IMG_RE.sub(img_repl, text))
                else:
                    node["notes"] = [ln.strip() for ln in text.split("\n") if ln.strip()]
                stats["notes"] += 1
            img = t.get("image")
            if isinstance(img, dict) and img.get("src"):
                node["images"].append(reg_xap(img["src"]))
            return node

        result = []
        for sh in sheets:
            rt = sh.get("rootTopic")
            if not isinstance(rt, dict):
                continue
            central = j_node(rt)
            rels = []
            for r in sh.get("relationships") or []:
                rels.append({"from": r.get("end1Id"), "to": r.get("end2Id"),
                             "labels": [r["title"]] if r.get("title") else []})
            result.append((sh.get("title") or "", central, rels))
        return result

    if "content.xml" in names:
        tree = ET.fromstring(z.read("content.xml"))

        def x_node(el):
            stats["topics"] += 1
            node = new_node(el.get("id"))
            t = el.find(f"{XMAP}title")
            node["title"] = clean_title(
                "".join(t.itertext()).strip() if t is not None else "", do_repair)
            ch = el.find(f"{XMAP}children")
            if ch is not None:
                for topics in ch.findall(f"{XMAP}topics"):
                    for tp in topics.findall(f"{XMAP}topic"):
                        node["children"].append(x_node(tp))
            notes = el.find(f"{XMAP}notes")
            if notes is not None:
                plain = notes.find(f"{XMAP}plain")
                xhtml = notes.find(f"{XMAP}html")
                text = None
                if plain is not None:
                    text = "".join(plain.itertext())
                elif xhtml is not None:
                    text = ET.tostring(xhtml, encoding="unicode")
                    text = re.sub(r"</?ns\d+:", lambda m: m.group(0)[:2], text)
                if text:
                    if "<" in text:
                        node["notes"] = html_to_lines(text)
                    else:
                        node["notes"] = [ln.strip() for ln in text.split("\n") if ln.strip()]
                    stats["notes"] += 1
            return node

        result = []
        for sheet in tree.findall(f"{XMAP}sheet"):
            rt = sheet.find(f"{XMAP}topic")
            if rt is None:
                continue
            central = x_node(rt)
            rels = []
            rels_el = sheet.find(f"{XMAP}relationships")
            if rels_el is not None:
                for r in rels_el.findall(f"{XMAP}relationship"):
                    lab = r.find(f"{XMAP}title")
                    rels.append({"from": r.get("end1"), "to": r.get("end2"),
                                 "labels": ["".join(lab.itertext())] if lab is not None else []})
            result.append((sheet.get("title") or "", central, rels))
        return result

    raise ValueError("xmind 压缩包内未找到 content.json 或 content.xml")


# ---------------- 其他脑图格式解析 ----------------
def freemind_parse(tree, do_repair: bool) -> tuple:
    """FreeMind/Freeplane .mm：<map><node TEXT=…> 嵌套。返回 (central, rels)。"""
    root = tree.find("node")
    if root is None:
        raise ValueError("FreeMind 文件缺少根 <node>")

    def f_node(el):
        stats["topics"] += 1
        node = new_node(el.get("ID"))
        title = html.unescape(el.get("TEXT") or el.get("text") or "").strip()
        note_html = node_html = None
        arrows = []
        for child in el:
            tag = local_tag(child)
            if tag == "node":
                node["children"].append(f_node(child))
            elif tag == "richcontent":
                if child.get("TYPE") == "NOTE":
                    note_html = child
                elif child.get("TYPE") == "NODE" and not title:
                    node_html = child
            elif tag == "arrowlink" and child.get("DESTINATION"):
                arrows.append(child.get("DESTINATION"))
        if not title and node_html is not None and len(node_html):
            lines = rich_html_lines(list(node_html)[0], "fm")
            title = "\n".join(ln for ln in lines
                              if not ln.strip().startswith("@@IMG:"))
            for k in MARK_RE.findall(" ".join(lines)):
                node["images"].append(k)
        node["title"] = clean_title(title, do_repair)
        if note_html is not None and len(note_html):
            node["notes"] = rich_html_lines(list(note_html)[0], "fm")
            if node["notes"]:
                stats["notes"] += 1
        node["_arrows"] = arrows
        return node

    central = f_node(root)

    rels = []

    def collect_arrows(n):
        for dest in n.pop("_arrows", []):
            rels.append({"from": n["oid"], "to": dest, "labels": []})
        for c in n["children"]:
            collect_arrows(c)

    collect_arrows(central)
    return central, rels


def opml_parse(tree, do_repair: bool) -> tuple:
    """OPML 大纲：<body><outline text=… _note=…> 嵌套。无图片/关联概念。"""
    body = tree.find("body")
    if body is None:
        raise ValueError("OPML 文件缺少 <body>")
    roots = [c for c in body if local_tag(c) == "outline"]
    if not roots:
        raise ValueError("OPML <body> 下没有 <outline>（不是大纲文件？）")

    def o_node(el):
        stats["topics"] += 1
        node = new_node(el.get("id"))
        raw = el.get("text") or el.get("title") or "".join(el.itertext()).strip()
        node["title"] = clean_title(strip_inline_html(html.unescape(raw)).strip(),
                                    do_repair)
        note = el.get("_note")
        if note:
            node["notes"] = html_note_lines(note, "op")
            if node["notes"]:
                stats["notes"] += 1
        for child in el:
            if local_tag(child) == "outline":
                node["children"].append(o_node(child))
        return node

    central = o_node(roots[0])
    # 多个平级根 outline：第一个作中心主题，其余挂为子分支
    for extra in roots[1:]:
        central["children"].append(o_node(extra))
    return central, []


def sniff_json_kind(obj) -> str:
    """区分脑图 JSON：kityminder（root.data）/ Coggle（text/children 递归）。"""
    if isinstance(obj, dict):
        root = obj.get("root")
        if isinstance(root, dict) and isinstance(root.get("data"), dict):
            return "km"
        if "text" in obj or "children" in obj:
            return "coggle"
    return ""


def kityminder_parse(obj, do_repair: bool) -> tuple:
    """百度脑图 .km：{"root": {"data": {…}, "children": […]}} 嵌套。"""
    root = obj.get("root")
    if not isinstance(root, dict):
        raise ValueError("km 文件缺少 root 节点")

    def k_node(d):
        stats["topics"] += 1
        data = d.get("data") or {}
        node = new_node(data.get("id"))
        raw = strip_inline_html(str(data.get("text") or "")).strip()
        node["title"] = clean_title(raw, do_repair)
        note = data.get("note")
        if isinstance(note, str) and note.strip():
            node["notes"] = html_note_lines(note, "km")
            if node["notes"]:
                stats["notes"] += 1
        image = data.get("image")
        if isinstance(image, dict) and isinstance(image.get("url"), str):
            m = re.match(r"data:image/[\w.+-]+;base64,([A-Za-z0-9+/=]+)",
                         image["url"])
            if m:  # http(s):// 远程图片不抓取
                try:
                    node["images"].append(
                        reg_inline_image("km", base64.b64decode(m.group(1))))
                except Exception:
                    pass
        for c in d.get("children") or []:
            if isinstance(c, dict):
                node["children"].append(k_node(c))
        return node

    return k_node(root), []


def coggle_parse(obj, do_repair: bool) -> tuple:
    """Coggle API/组织版导出树：{"text", "children"} 递归（无笔记概念）。"""
    def c_node(d):
        stats["topics"] += 1
        node = new_node(d.get("id"))
        raw = strip_inline_html(html.unescape(str(d.get("text") or ""))).strip()
        node["title"] = clean_title(raw, do_repair)
        ch = d.get("children")
        if isinstance(ch, list):
            for c in ch:
                if isinstance(c, dict):
                    node["children"].append(c_node(c))
        return node

    return c_node(obj), []


def mindnode_parse(pkg: Path, do_repair: bool) -> tuple:
    """MindNode .mindnode 包（目录或整包 zip）：仅支持 v3 contents.json。"""
    z = zipfile.ZipFile(pkg) if pkg.is_file() else None

    def read_bytes(rel):
        if z is not None:
            try:
                return z.read(rel)
            except KeyError:
                return None
        p = pkg / rel
        return p.read_bytes() if p.is_file() else None

    try:
        data = read_bytes("contents.json")
        if data is None:
            has_plist = (any(n.endswith(".plist") for n in z.namelist())
                         if z is not None else any(pkg.glob("*.plist")))
            if has_plist:
                raise ValueError("MindNode v2（NodeStorev2 plist）暂不支持："
                                 "请先用 MindNode 打开并另存为 v3 格式后再转换")
            raise ValueError("MindNode 包内未找到 contents.json")
        obj = json.loads(data.decode("utf-8"))
        nodes = obj.get("nodes")
        if not isinstance(nodes, dict):
            raise ValueError("contents.json 缺少 nodes 字典（不是 MindNode v3 格式？）")

        def load_attachment(url):
            rel = unquote(url)
            for prefix in ("file://", "file:"):
                if rel.startswith(prefix):
                    rel = rel[len(prefix):]
            rel = rel.lstrip("/")
            if not rel or ".." in Path(rel).parts:
                return None
            if z is not None:
                try:
                    return z.read(rel)
                except KeyError:
                    tail = "/" + rel  # zip 内可能带包名前缀，后缀匹配兜底
                    for n in z.namelist():
                        if n.endswith(tail):
                            return z.read(n)
                    return None
            target = (pkg / rel).resolve()
            root_dir = pkg.resolve()
            if root_dir not in target.parents:
                return None  # 防路径穿越：附件必须在包内
            return target.read_bytes() if target.is_file() else None

        def m_node(uid, visited):
            info = nodes.get(uid)
            if not isinstance(info, dict):
                return None
            stats["topics"] += 1
            node = new_node(uid)
            node["title"] = clean_title(
                strip_inline_html(str(info.get("title") or "")).strip(), do_repair)
            notes = info.get("notes")
            if isinstance(notes, dict) and isinstance(notes.get("text"), str):
                text = re.sub(r"<br\s*/?>", "\n", html.unescape(notes["text"]))
                node["notes"] = [ln.strip() for ln in text.split("\n") if ln.strip()]
                if node["notes"]:
                    stats["notes"] += 1
            for att in info.get("attachments") or []:
                if not isinstance(att, dict) or att.get("type") != "image":
                    continue
                value = att.get("value") or {}
                url = value.get("url") or value.get("fileName")
                if isinstance(url, str):
                    blob = load_attachment(url)
                    if blob:
                        node["images"].append(reg_inline_image("mn", blob))
            for sub in info.get("subtopics") or []:
                if sub in visited:
                    continue  # 防御：subtopics 引用环
                child = m_node(sub, visited | {sub})
                if child is not None:
                    node["children"].append(child)
            return node

        root_uid = obj.get("rootNodeID")
        central = m_node(root_uid, {root_uid}) if root_uid in nodes else None
        if central is None:  # 无 rootNodeID：取第一个未被引用为 subtopic 的节点
            referenced = set()
            for info in nodes.values():
                if isinstance(info, dict):
                    referenced.update(info.get("subtopics") or [])
            for uid in nodes:
                if uid not in referenced:
                    central = m_node(uid, {uid})
                    break
        if central is None:
            raise ValueError("MindNode contents.json 无法定位根节点")
        rels = []
        for c in (obj.get("connections") or {}).values():
            if isinstance(c, dict):
                label = c.get("label") if isinstance(c.get("label"), str) else ""
                rels.append({"from": c.get("endA"), "to": c.get("endB"),
                             "labels": [label] if label else []})
        return central, rels
    finally:
        if z is not None:
            z.close()


def ithoughts_parse(z: zipfile.ZipFile, do_repair: bool) -> tuple:
    """iThoughts .itmx/.itmz：zip 内 map.xml 为 plist（mindmap->topics 嵌套）。"""
    names = z.namelist()
    src = z
    if "map.xml" not in names:  # .itmz：内层还套一个 .itmx
        cand = [n for n in names if n.lower().endswith(".itmx")]
        if not cand:
            raise ValueError("压缩包内未找到 map.xml 或 .itmx（不是 iThoughts 文件？）")
        src = zipfile.ZipFile(io.BytesIO(z.read(cand[0])))
        if "map.xml" not in src.namelist():
            raise ValueError(".itmx 内未找到 map.xml")
    try:
        pl = plistlib.loads(src.read("map.xml"))
        mm = pl.get("mindmap") if isinstance(pl, dict) else None
        topics = mm.get("topics") if isinstance(mm, dict) else None
        if not isinstance(topics, list):
            raise ValueError("map.xml 缺少 mindmap/topics（不是 iThoughts 脑图？）")

        rels = []

        def i_node(t):
            stats["topics"] += 1
            node = new_node(t.get("uuid") or t.get("UUID"))
            node["title"] = clean_title(
                strip_inline_html(str(t.get("text") or "")).strip(), do_repair)
            note = t.get("note")
            if isinstance(note, str) and note.strip():
                node["notes"] = html_note_lines(note, "itx")
                if node["notes"]:
                    stats["notes"] += 1
            for att in t.get("attachments") or []:
                if not isinstance(att, dict):
                    continue
                if att.get("type") == "image":
                    b64 = att.get("data")
                    if isinstance(b64, (bytes, bytearray)):
                        try:
                            node["images"].append(reg_inline_image("itx", bytes(b64)))
                        except Exception:
                            pass
                elif att.get("type") == "relationship":
                    a = att.get("start-point") or att.get("endA")
                    b = att.get("end-point") or att.get("endB")
                    if a and b:
                        rels.append({"from": a, "to": b, "labels": []})
            for c in t.get("children") or []:
                if isinstance(c, dict):
                    node["children"].append(i_node(c))
            return node

        # 根 = topics 里第一个没有被 parents 引用的主题
        parented = set()
        for t in topics:
            if isinstance(t, dict):
                parented.update(t.get("parents") or [])

        def t_uid(t):
            return (t.get("uuid") or t.get("UUID")) if isinstance(t, dict) else None

        central = None
        for t in topics:
            uid = t_uid(t)
            if uid and uid not in parented:
                central = i_node(t)
                break
        if central is None and isinstance(topics[0], dict):
            central = i_node(topics[0])
        if central is None:
            raise ValueError("iThoughts topics 为空")
        # 其余顶层主题作为分支挂到根下
        for t in topics[1:]:
            uid = t_uid(t)
            if uid and uid not in parented and uid != central["oid"]:
                central["children"].append(i_node(t))
        return central, rels
    finally:
        if src is not z:
            src.close()


# ---------------- 章节编号 ----------------
def int_to_roman(n: int) -> str:
    vals = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"),
            (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"),
            (5, "V"), (4, "IV"), (1, "I")]
    out = []
    for v, s in vals:
        while n >= v:
            out.append(s)
            n -= v
    return "".join(out)


def int_to_alpha(n: int) -> str:
    out = []
    while n > 0:
        n, r = divmod(n - 1, 26)
        out.append(chr(65 + r))
    return "".join(reversed(out))


CN_DIGITS = "零一二三四五六七八九"

def int_to_chinese(n: int) -> str:
    if n <= 9:
        return CN_DIGITS[n]
    if n < 20:
        return "十" + (CN_DIGITS[n % 10] if n % 10 else "")
    tens, ones = divmod(n, 10)
    return CN_DIGITS[tens] + "十" + (CN_DIGITS[ones] if ones else "")


def seq_render(i: int, seq: str) -> str:
    if seq == "roman":
        return int_to_roman(i)
    if seq == "alpha":
        return int_to_alpha(i)
    if seq == "chinese":
        return int_to_chinese(i)
    return str(i)


def number_tree(roots, p) -> None:
    """按"基本规则 + 默认/定制参数"给主题编号（结果写入 node["num"]）。

    基本规则（始终生效）：
    - 正文不编号。
    - 起始层级（文档中第一个出现可编号主题的层级）必须使用所选序列，
      按无编号父主题分组、各自从 1 连续编号。
    - 父主题有编号 → 子主题编号为 父号.序号（序号沿用父编号末段的
      序列：父 1 → 1.1，父 Ⅰ → Ⅰ.Ⅰ）。
    - 父主题没有编号且非起始层级 → 该兄弟组用直接上级主题（最近的已
      编号祖先）没有用过的其他序列（优先罗马数字，其次阿拉伯、字母、
      汉字）从头连续编号，各自从 1 重起。
    层级规则（默认/定制）：
    - 从第 start_level 个层级开始编号（1 起算，根主题为第 1 层，根主题
      的直接子主题为第 2 层）；
    - 同一个父主题的兄弟主题数 > gt_count 的才编号（按同父兄弟分组统计，
      同组主题要么全部编号、要么全部不编号）。
    编号存为 (序号, 序列) 元组列表。
    """
    gsize = {}  # id(node) -> 同父兄弟主题数

    def scan(n, d, size):
        gsize[id(n)] = size
        for c in n["children"]:
            scan(c, d + 1, len(n["children"]))

    for r in roots:
        scan(r, 1, len(roots))

    def eligible(n, d):
        return d + 1 >= p["start_level"] and gsize[id(n)] > p["gt_count"]

    # 起始层级：文档中第一个出现可编号主题的内部深度（DFS 先序）
    start = None

    def find_start(n, d):
        nonlocal start
        if start is None and eligible(n, d):
            start = d
            return
        for c in n["children"]:
            find_start(c, d + 1)
            if start is not None:
                return

    for r in roots:
        find_start(r, 1)
        if start is not None:
            break

    restarts = {}  # id(parent) -> 该父主题下已重起的编号计数

    def assign(n, d, pnum, anc, parent):
        # pnum: (父主题编号, 兄弟序号)；anc: 最近的已编号祖先的编号
        num = None
        if eligible(n, d):
            if pnum is not None and pnum[0] is not None:
                # 子序号沿用父编号末段的序列：父 1 → 1.1，父 Ⅰ → Ⅰ.Ⅰ
                num = pnum[0] + [(pnum[1], pnum[0][-1][1])]
            elif d == start:
                # 起始层级：必须用所选序列
                c = restarts.setdefault(id(parent), [0])
                c[0] += 1
                num = [(c[0], p["sequence"])]
            else:
                # 父无编号且非起始层：用直接上级没用过的其他序列，优先罗马
                used = {s for _, s in anc} if anc else set()
                seq = next(s for s in ("roman", "arabic", "alpha", "chinese")
                           if s not in used)
                c = restarts.setdefault(id(parent), [0])
                c[0] += 1
                num = [(c[0], seq)]
        n["num"] = num
        sib = 0
        new_anc = num if num is not None else anc
        for c in n["children"]:
            if num is not None and eligible(c, d + 1):
                sib += 1
            assign(c, d + 1, (num, sib), new_anc, n)

    for r in roots:
        assign(r, 1, None, None, None)


def num_prefix(num) -> str:
    # num 是 (序号, 序列) 元组列表：起始层用所选序列；父无编号的重起段
    # 用直接上级没用过的其他序列（优先罗马数字）；父子接续沿用父编号末
    # 段的序列（Ⅰ.Ⅰ / 1.1）
    s = ".".join(seq_render(i, seq) for i, seq in num)
    if len(num) == 1:
        s += "."
    return s + " "


# ---------------- Markdown 生成 ----------------
def emit_notes(node, out, prefix="> ", do_repair=True):
    if not node["notes"]:
        return
    for line in node["notes"]:
        parts = MARK_RE.split(line)
        for i, piece in enumerate(parts):
            if not piece:
                continue
            if i % 2 == 1:
                src = IMG_SRC.get(piece)
                if src and src[1] is not None:
                    out.append(f"{prefix}![图片]({IMG_PREFIX}{src[2]}.{src[1]})")
            else:
                out.append(f"{prefix}{repair(piece) if do_repair else piece}")


def emit_topic(node, depth, out, do_repair=True, numbering=False, seq="arabic"):
    raw = node["title"] or "（无标题）"
    if numbering:
        # 源主题常自带手工编号（"1. "、"（1）"），与自动编号重复，先移除；
        # 不编号模式则原样保留主题文本
        raw = MANUAL_NUM_RE.sub("", raw, count=1)
    if numbering and node.get("num"):
        raw = num_prefix(node["num"]) + raw
    if node["task_pct"]:
        pct = node["task_pct"]
        raw += " ✅" if pct == "100" else f"（进度 {pct}%）"
    lines = raw.split("\n")
    head = HEAD_HASH_RE.sub("", lines[0]).strip() or "（无标题）"
    if depth <= 6:
        out.append(f"{'#' * depth} {head}")
    else:
        out.append(f"{'  ' * (depth - 7)}- **{head}**")
    out.append("")
    for ln in lines[1:]:
        if ln.strip():
            out.append(f"> {ln.strip()}")
    if len(lines) > 1:
        out.append("")
    emitted = 0
    for key in node["images"]:
        src = IMG_SRC.get(key)
        if src and src[1] is not None:
            out.append(f"![主题图片]({IMG_PREFIX}{src[2]}.{src[1]})")
            out.append("")
            emitted += 1
    if node["images"] and not emitted:
        out.append("> （原主题图片为私有格式，无法转换导出）")
        out.append("")
    emit_notes(node, out, do_repair=do_repair)
    if node["notes"]:
        out.append("")
    if node["callout"]:
        out.append(f"> 💬 标注：{node['callout']}")
        out.append("")
    for ch in node["children"]:
        emit_topic(ch, depth + 1, out, do_repair, numbering=numbering, seq=seq)


def collect_img_keys(node, acc):
    acc.update(node["images"])
    if node["notes"]:
        for line in node["notes"]:
            acc.update(MARK_RE.findall(line))
    for c in node["children"]:
        collect_img_keys(c, acc)


def oid_titles(node, acc):
    acc[node["oid"]] = node["title"].replace("\n", " ")
    for c in node["children"]:
        oid_titles(c, acc)


def sniff_ext(head: bytes):
    """按魔数识别图片扩展名；None = 不支持（含 MindManager 私有 GDI+）。"""
    if head.startswith(b"\x89PNG"):
        return "png"
    if head.startswith(b"\xff\xd8"):
        return "jpg"
    if head.startswith(b"GIF8"):
        return "gif"
    return None


def write_image_file(key, data: bytes, out_dir: Path, used: dict) -> bool:
    """魔数校验后写盘，回填 IMG_SRC[key] 的扩展名与文件名。"""
    ext = sniff_ext(data[:8])
    src = IMG_SRC.get(key)
    if ext is None or not src:
        return False
    base = src[2] or f"img{abs(hash(key)) % 100000:05d}"
    base = re.sub(r"[^\w\-. ]", "_", base)
    if base.lower() in used and used[base.lower()] != key:
        n = 2
        while f"{base}_{n}".lower() in used:
            n += 1
        base = f"{base}_{n}"
    used[base.lower()] = key
    (out_dir / f"{base}.{ext}").write_bytes(data)
    IMG_SRC[key] = (src[0], ext, base)
    return True


def resolve_images(z, keys, out_dir: Path) -> None:
    """图片落盘统一收口：内嵌字节（IMG_DATA）优先，否则从 zip 条目读取。"""
    used = {}
    for key in sorted(keys):
        src = IMG_SRC.get(key)
        if not src:
            continue
        data = IMG_DATA.get(key)
        if data is None and z is not None and src[0] != "<inline>":
            try:
                data = z.read(src[0])
            except KeyError:
                continue
        if data is not None:
            write_image_file(key, data, out_dir, used)


# ---------------- 主流程 ----------------
def load_and_parse(input_path: Path, do_repair: bool):
    """按输入类型打开并解析。

    返回 (central_list, 需保持打开到图片落盘的 zip 或 None, 待删临时文件)。
    新格式的图片都登记进 IMG_DATA 内嵌通道，无需保持 zip 打开。
    """
    suffix = input_path.suffix.lower()
    z = None
    tmp = None
    if suffix == ".html":
        text = input_path.read_text(encoding="utf-8")
        m = re.search(r'<script\s+id=["\']mmap["\'][^>]*>\s*([A-Za-z0-9+/=]+)\s*</script>', text)
        if not m:
            raise ValueError(
                "HTML 中未找到 <script id='mmap'> 内嵌数据（不是 MindManager 导出的脑图？）")
        tmp = input_path.with_suffix(".mmap.tmp")
        tmp.write_bytes(base64.b64decode(m.group(1)))
        z = zipfile.ZipFile(tmp)
        central, rels = mm_parse(z, do_repair)
        return [("", central, rels)], z, tmp
    if suffix in (".mmap", ".xmind"):
        z = zipfile.ZipFile(input_path)
        if suffix == ".xmind":
            return xmind_parse(z, do_repair), z, tmp
        central, rels = mm_parse(z, do_repair)
        return [("", central, rels)], z, tmp
    if suffix == ".mm":
        tree = parse_xml_root(input_path.read_bytes(), "FreeMind .mm")
        if local_tag(tree) != "map":
            raise ValueError("根元素不是 <map>：不是 FreeMind/Freeplane 脑图")
        central, rels = freemind_parse(tree, do_repair)
        return [("", central, rels)], z, tmp
    if suffix == ".opml":
        tree = parse_xml_root(input_path.read_bytes(), "OPML .opml")
        if local_tag(tree) != "opml":
            raise ValueError("根元素不是 <opml>：不是 OPML 大纲")
        central, rels = opml_parse(tree, do_repair)
        return [("", central, rels)], z, tmp
    if suffix in (".km", ".json"):
        try:
            obj = json.loads(input_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"文件不是合法 JSON：{e}")
        kind = sniff_json_kind(obj)
        if kind == "km":
            central, rels = kityminder_parse(obj, do_repair)
        elif kind == "coggle":
            central, rels = coggle_parse(obj, do_repair)
        else:
            raise ValueError("JSON 结构既不是百度脑图（root.data.text）也不是 "
                             "Coggle（text/children 递归），不是受支持的脑图 JSON")
        return [("", central, rels)], z, tmp
    if suffix == ".mindnode":
        central, rels = mindnode_parse(input_path, do_repair)
        return [("", central, rels)], z, tmp
    if suffix in (".itmx", ".itmz"):
        z = zipfile.ZipFile(input_path)
        central, rels = ithoughts_parse(z, do_repair)
        z.close()
        z = None
        return [("", central, rels)], z, tmp
    raise ValueError(f"不支持的文件类型 {suffix}（支持 .html / .mmap / .xmind / "
                     ".mm / .opml / .km / .json / .mindnode / .itmx / .itmz）")


def convert(input_path: Path, params) -> dict:
    tmp = None
    z = None
    try:
        central_list, z, tmp = load_and_parse(input_path, params["repair"])
        do_repair = params["repair"]

        global IMG_PREFIX
        out_md = params["out_md"]
        out_md.parent.mkdir(parents=True, exist_ok=True)
        img_dir = out_md.parent / params["images_dir"]
        IMG_PREFIX = params["images_dir"] + "/"

        keys = set()
        for _, central, _ in central_list:
            collect_img_keys(central, keys)
            for f in central.get("floats", []):
                collect_img_keys(f, keys)

        if keys:
            img_dir.mkdir(exist_ok=True)
            resolve_images(z, keys, img_dir)

        out = []
        first = True
        for sheet_title, central, rels in central_list:
            if not first:
                out.append("---")
                out.append("")
            first = False
            if params["numbering"]:
                number_tree(central["children"], params)
            out.append("# " + (central["title"] or sheet_title or "脑图").replace("\n", " "))
            out.append("")
            out.append(f"> 本文档由脑图自动转换，图片保存在 `{params['images_dir']}/` 目录。")
            out.append("")
            emit_notes(central, out, do_repair=do_repair)
            emitted = 0
            for key in central["images"]:
                src = IMG_SRC.get(key)
                if src and src[1] is not None:
                    out.append(f"![主题图片]({IMG_PREFIX}{src[2]}.{src[1]})")
                    emitted += 1
            if central["notes"] or emitted:
                out.append("")
            for ch in central["children"]:
                emit_topic(ch, 2, out, do_repair, numbering=params["numbering"],
                           seq=params["sequence"])

            if central.get("floats"):
                out.append("---")
                out.append("")
                out.append("## 浮动主题")
                out.append("")
                for f in central["floats"]:
                    emit_topic(f, 3, out, do_repair, numbering=params["numbering"],
                               seq=params["sequence"])

            if rels:
                titles = {}
                oid_titles(central, titles)
                out.append("---")
                out.append("")
                out.append("## 主题关联关系")
                out.append("")
                for r in rels:
                    frm = titles.get(r["from"], r["from"] or "?")
                    to = titles.get(r["to"], r["to"] or "?")
                    line = f"- {frm} → {to}"
                    for lab in r["labels"]:
                        line += f"（标注：{lab}）"
                    out.append(line)
                out.append("")

        out_md.write_text("\n".join(out), encoding="utf-8")
        n_img = sum(1 for k in keys if IMG_SRC.get(k) and IMG_SRC[k][1] is not None)
        return {"md": out_md, "images_dir": img_dir, "images": n_img}
    finally:
        if z is not None:
            z.close()
        if tmp is not None and tmp.exists():
            tmp.unlink()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="脑图转 Markdown（MindManager .html/.mmap、XMind .xmind、FreeMind .mm、"
                    "OPML、百度脑图 .km、Coggle .json、MindNode .mindnode、iThoughts .itmx/.itmz）")
    ap.add_argument("input", help="输入文件路径")
    ap.add_argument("--no-numbering", action="store_true", help="不编号")
    ap.add_argument("--sequence", choices=["arabic", "roman", "alpha", "chinese"],
                    default="arabic", help="编号序列（默认 arabic）")
    ap.add_argument("--start-level", type=int, default=2,
                    help="从第 N 个层级开始编号，根主题为第 1 层（默认 2）")
    ap.add_argument("--gt-count", type=int, default=3,
                    help="同一个父主题的兄弟主题数 > N 的才编号（默认 3）")
    ap.add_argument("--no-repair", action="store_true", help="关闭 GBK 乱码修复")
    ap.add_argument("--out-md", default=None, help="输出 Markdown 路径")
    ap.add_argument("--images-dir", default=None, help="图片输出目录名")
    args = ap.parse_args(argv)

    inp = Path(args.input).resolve()
    if not inp.exists():
        print(f"ERROR: 文件不存在: {inp}", file=sys.stderr)
        return 2
    out_md = Path(args.out_md).resolve() if args.out_md else inp.with_suffix(".md")
    params = {
        "numbering": not args.no_numbering,
        "sequence": args.sequence,
        "start_level": args.start_level,
        "gt_count": args.gt_count,
        "repair": not args.no_repair,
        "out_md": out_md,
        "images_dir": args.images_dir or (out_md.stem + "-images"),
    }
    try:
        res = convert(inp, params)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"topics={stats['topics']} notes={stats['notes']} repaired={stats['repaired']} "
          f"images={res['images']}")
    print(f"written {res['md']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
