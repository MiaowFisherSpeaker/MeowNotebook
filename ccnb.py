#!/usr/bin/env python3
"""MeowNotebook: 把 cc 的对话整理成可浏览的问答网站。

子命令:
  serve                    启动本地网站(默认 127.0.0.1:8765)
  ensure-server            若未运行则后台启动,打印网址
  list [--project DIR]     列出会话
  extract (--current | --session ID) [--new-only]
                           以 Markdown 输出某会话的问答(供 /qa-notes skill 整理)
  current                  打印当前目录最新会话的 ID
  launch [--detach] [cc 参数...]
                           在当前目录以 bypass 模式启动 cc(tmux 会话),
                           并确保网站运行;本地终端与网页终端共享同一会话
"""
import argparse
import base64
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import term

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
NOTES = ROOT / "data" / "notes"
STARS = ROOT / "data" / "stars.json"
PROJECTS = term.CC_HOME / "projects"
HOST = os.environ.get("CCNB_HOST", "127.0.0.1")
PORT = int(os.environ.get("CCNB_PORT", "8765"))
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp", ".bmp": "image/bmp", ".avif": "image/avif"}
ACTIVE_SECONDS = 180  # 文件在这么多秒内被写过 → 视为进行中
# 只接受这些主机名的请求(防 DNS rebinding);端口不限,方便 VS Code / SSH 转发到别的本地端口
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]", *filter(None, os.environ.get("CCNB_ALLOWED_HOSTS", "").split(","))}

# ---------------------------------------------------------------- 解析

TAG_RE = {
    "reminder": re.compile(r"<system-reminder>.*?</system-reminder>", re.S),
    "cmd_name": re.compile(r"<command-name>(.*?)</command-name>", re.S),
    "cmd_args": re.compile(r"<command-args>(.*?)</command-args>", re.S),
    "cmd_msg": re.compile(r"<command-message>.*?</command-message>", re.S),
    "bash_in": re.compile(r"<bash-input>(.*?)</bash-input>", re.S),
    "out": re.compile(r"<(local-command-stdout|local-command-stderr|bash-stdout|bash-stderr)>(.*?)</\1>", re.S),
    "pasted": re.compile(r"<pasted_content[^>]*>(.*?)</pasted_content[^>]*>", re.S),
    "caveat": re.compile(r"<local-command-caveat>.*?</local-command-caveat>", re.S),
}
INTERRUPT_RE = re.compile(r"^\[Request interrupted by user.*?\]$", re.S)


def _blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content or []


def _clean_user_text(text):
    """返回 (kind, text)。kind: question / command / shell / output / interrupt / None"""
    text = TAG_RE["reminder"].sub("", text)
    text = TAG_RE["caveat"].sub("", text)
    if not text.strip():
        return None, ""
    if INTERRUPT_RE.match(text.strip()):
        return "interrupt", text.strip()
    m = TAG_RE["cmd_name"].search(text)
    if m:
        args = TAG_RE["cmd_args"].search(text)
        return "command", (m.group(1).strip() + " " + (args.group(1).strip() if args else "")).strip()
    m = TAG_RE["bash_in"].search(text)
    if m:
        return "shell", m.group(1).strip()
    m = TAG_RE["out"].search(text)
    if m and not TAG_RE["out"].sub("", text).strip():
        return "output", m.group(2).strip()
    text = TAG_RE["pasted"].sub(lambda mm: "\n> " + mm.group(1).strip().replace("\n", "\n> ") + "\n", text)
    text = TAG_RE["cmd_msg"].sub("", text)
    return "question", text.strip()


def parse_session(path):
    """把一个 .jsonl 会话解析成问答轮次。"""
    meta = {"id": path.stem, "file": str(path), "cwd": None, "title": None,
            "ai_title": None, "first_q": None, "started": None, "updated": None, "continued_in": None}
    turns = []
    # 图片只记位置 (类型, 所在行的字节偏移, 该行第几个 base64 块),turn 里存索引;
    # /api/image/<sid>/<k> 时再回文件读那一行。图片数据不进缓存(动辄几十 MB)
    images = []
    cur = None
    seen_text = set()  # 同一条 assistant 消息可能被重复写入
    line_b64 = None  # 当前行里所有 base64 块的 id(),按出现顺序

    def add_image(src):
        nonlocal line_b64
        if isinstance(src, dict) and src.get("type") == "base64" and src.get("data"):
            if line_b64 is None:
                line_b64 = [id(x) for x in _base64_blocks(d)]
            images.append((src.get("media_type") or "image/png", offset, line_b64.index(id(src))))
            return len(images) - 1
        return None

    def new_turn(kind, text, ts):
        nonlocal cur
        cur = {"kind": kind, "q": text, "ts": ts, "texts": [], "tools": 0, "notes": [], "q_images": [], "images": []}
        turns.append(cur)

    with open(path, "rb") as f:
        pos = 0
        for raw in f:
            offset, pos = pos, pos + len(raw)
            line_b64 = None
            try:
                d = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            t = d.get("type")
            if t == "ai-title":
                meta["ai_title"] = d.get("aiTitle")
                continue
            if t == "continued-in":  # 从 agent 视图重新进入会话时,cc 会把历史复制到新会话继续,并在旧会话末尾留下这条记录
                meta["continued_in"] = d.get("continuedInSessionId")
                continue
            if t in ("custom-title", "agent-name"):
                meta["title"] = d.get("customTitle") or d.get("agentName") or meta["title"]
                continue
            if t not in ("user", "assistant") or d.get("isSidechain"):
                continue
            ts = d.get("timestamp")
            meta["cwd"] = meta["cwd"] or d.get("cwd")
            meta["started"] = meta["started"] or ts
            meta["updated"] = ts or meta["updated"]
            msg = d.get("message") or {}

            if t == "user":
                if d.get("isCompactSummary"):
                    txt = "".join(b.get("text", "") for b in _blocks(msg.get("content")) if b.get("type") == "text")
                    new_turn("summary", txt, ts)
                    continue
                if d.get("isMeta"):
                    continue
                blocks = _blocks(msg.get("content"))
                if any(b.get("type") == "tool_result" for b in blocks):
                    # 工具结果里的图片(cc 用 Read 查看的截图、PDF 页等)挂到当前这一轮的回答下
                    for b in blocks:
                        if b.get("type") == "tool_result" and isinstance(b.get("content"), list) and cur:
                            for x in b["content"]:
                                if x.get("type") == "image":
                                    k = add_image(x.get("source"))
                                    if k is not None:
                                        cur["images"].append(k)
                    continue
                parts, kinds, n_img, q_imgs = [], [], 0, []
                for b in blocks:
                    if b.get("type") == "image":
                        n_img += 1
                        k = add_image(b.get("source"))
                        if k is not None:
                            q_imgs.append(k)
                    elif b.get("type") == "text":
                        k, txt = _clean_user_text(b.get("text", ""))
                        if k:
                            kinds.append(k)
                            parts.append(txt)
                if not kinds and not n_img:
                    continue
                kind = "question" if "question" in kinds or n_img else kinds[0]
                text = "\n\n".join(parts)
                if kind == "interrupt":
                    if cur:
                        cur["notes"].append("⏹ 用户中断")
                    continue
                if kind == "output":
                    if cur:
                        cur["notes"].append("```\n" + text[:2000] + "\n```")
                    continue
                new_turn(kind, text.strip() or "(图片)", ts)
                cur["q_images"] = q_imgs
                if kind == "question" and not meta["first_q"]:
                    meta["first_q"] = text.strip()[:200]
            else:
                if cur is None:
                    new_turn("question", "", ts)
                for b in _blocks(msg.get("content")):
                    bt = b.get("type")
                    if bt == "text":
                        txt = b.get("text", "").strip()
                        key = (msg.get("id"), txt)
                        if txt and key not in seen_text and txt != "No response requested.":
                            seen_text.add(key)
                            cur["texts"].append(txt)
                    elif bt == "tool_use":
                        cur["tools"] += 1

    turns = [t for t in turns if t["q"] or t["texts"] or t["images"]]
    for i, t in enumerate(turns, 1):
        t["n"] = i
    meta["title"] = meta["title"] or meta["ai_title"] or (meta["first_q"] or meta["id"])[:60]
    meta["questions"] = sum(1 for t in turns if t["kind"] == "question")
    meta["_images"] = images
    return meta, turns


def _base64_blocks(obj):
    """按出现顺序列出 obj 里所有 {"type": "base64", "data": ...} 块。"""
    if isinstance(obj, dict):
        if obj.get("type") == "base64" and obj.get("data"):
            yield obj
        for v in obj.values():
            yield from _base64_blocks(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _base64_blocks(v)


def read_image(path, ref):
    """按 parse_session 记下的位置取回一张图片:(类型, 二进制)。"""
    mt, offset, j = ref
    with open(path, "rb") as f:
        f.seek(offset)
        d = json.loads(f.readline().decode("utf-8", errors="replace"))
    src = next(x for k, x in enumerate(_base64_blocks(d)) if k == j)
    return mt, base64.b64decode(src["data"])


# 两级缓存:元数据(列表、索引用)很小,常驻;完整问答按文本量做 LRU。键都是 (mtime_ns, size),文件一变就失效
_meta_cache = {}
_full_cache = OrderedDict()
FULL_CACHE_BUDGET = 32 * 1024 * 1024  # 完整问答的文本总量上限(字符数,近似)
_full_cache_size = 0
_cache_lock = threading.Lock()


def _file_key(path):
    st = path.stat()
    return st.st_mtime_ns, st.st_size


def _turns_size(turns):
    return sum(len(t["q"] or "") + sum(map(len, t["texts"])) + sum(map(len, t["notes"])) + 200 for t in turns)


def cached_parse(path):
    """完整解析结果 (meta, turns),LRU 缓存。"""
    global _full_cache_size
    key = _file_key(path)
    with _cache_lock:
        hit = _full_cache.get(path)
        if hit and hit[0] == key:
            _full_cache.move_to_end(path)
            return hit[1]
    res = parse_session(path)
    size = _turns_size(res[1])
    with _cache_lock:
        old = _full_cache.pop(path, None)
        if old:
            _full_cache_size -= old[2]
        _full_cache[path] = (key, res, size)
        _full_cache_size += size
        while _full_cache_size > FULL_CACHE_BUDGET and len(_full_cache) > 1:
            _, (_, _, sz) = _full_cache.popitem(last=False)
            _full_cache_size -= sz
        _meta_cache[path] = (key, res[0])
    return res


def cached_meta(path):
    """只要元数据(标题、时间、问题数、接续关系…)时用这个:命中时不碰完整问答缓存。"""
    key = _file_key(path)
    hit = _meta_cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    return cached_parse(path)[0]


def all_session_files():
    return [p for p, _ in session_index().files]


def find_session(sid):
    p = session_index().by_id.get(sid)
    if p and p.exists():
        return p
    for p in PROJECTS.glob(f"*/{sid}.jsonl"):  # 索引建立之后才出现的会话
        return p
    return None


class SessionIndex:
    """所有会话的元数据 + 接续关系,一次建好,列表 / 搜索 / 终端关联都从这里查,避免每条摘要再扫一遍目录。
    只要有文件新增、删除或变化(mtime / 大小)就重建;重建只读元数据缓存,通常是毫秒级。"""

    def __init__(self, files):
        self.files = files  # [(path, mtime)],按 mtime 从新到旧
        self.meta = {p: cached_meta(p) for p, _ in files}
        self.by_id = {p.stem: p for p, _ in files}
        self.succ, self.prev = {}, {}
        for p, m in self.meta.items():
            nxt = self.by_id.get(m["continued_in"]) if m["continued_in"] else None
            # 新会话必须确实带着复制过去的历史(提问数不少于旧会话),否则旧会话仍是唯一完整的记录
            if nxt and nxt != p and self.meta[nxt]["questions"] >= m["questions"]:
                self.succ[p] = nxt
                self.prev[nxt] = p

    def latest(self, path):
        """沿接续链走到最新的会话。"""
        for _ in range(50):  # 防环
            nxt = self.succ.get(path)
            if not nxt:
                break
            path = nxt
        return path

    def predecessors(self, path):
        """被 path 接续的旧会话(由近到远)。"""
        out = []
        while (path := self.prev.get(path)) and path not in out and len(out) < 50:
            out.append(path)
        return out


_index = None  # (建立时间, 文件指纹, SessionIndex)
_index_lock = threading.Lock()
INDEX_MAX_AGE = 1.0  # 秒:同一个请求里(列表循环、终端关联)反复调用时复用,不必每次 stat 全部文件


def session_index():
    global _index
    with _index_lock:
        if _index and time.monotonic() - _index[0] < INDEX_MAX_AGE:
            return _index[2]
    files = []
    for p in PROJECTS.glob("*/*.jsonl"):
        try:
            st = p.stat()
        except OSError:
            continue
        files.append((p, st.st_mtime, st.st_mtime_ns, st.st_size))
    files.sort(key=lambda x: x[1], reverse=True)
    key = tuple((p, ns, sz) for p, _, ns, sz in files)
    with _index_lock:
        idx = _index[2] if _index and _index[1] == key else SessionIndex([(p, mt) for p, mt, _, _ in files])
        _index = (time.monotonic(), key, idx)
        return idx


def successor(path):
    """会话已在别的会话里继续且新会话带着完整历史 → 新会话的路径,否则 None。"""
    return session_index().succ.get(path)


def latest_continuation(path):
    return session_index().latest(path)


def predecessors(path):
    return session_index().predecessors(path)


def find_note(path):
    """整理版可能是在接续之前写的,挂在旧会话 id 上。"""
    for p in [path, *predecessors(path)]:
        note = read_note(p.stem)
        if note:
            return note
    return None


def project_dir_for(cwd):
    return PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd).resolve()))


def current_session(cwd=None):
    d = project_dir_for(cwd or os.getcwd())
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _epoch(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() if ts else 0
    except ValueError:
        return 0


def _started_epoch(p):
    try:
        return _epoch(cached_meta(p)["started"])
    except OSError:
        return 0


def sid_by_title(cwd, title):
    if not title:
        return None
    prefix = title[:-1].rstrip() if title.endswith("…") else None
    d = project_dir_for(cwd)
    for p, _ in session_index().files:  # 从新到旧,第一个匹配的就是最近更新的
        if p.parent != d:
            continue
        meta = cached_meta(p)
        names = {meta["title"], meta["ai_title"]} - {None}
        if title in names or (prefix and any(n.startswith(prefix) for n in names)):
            return p.stem
    return None


def pane_record(pane):
    try:
        return json.loads((ROOT / "data" / "panes" / f"{pane}.json").read_text()) if pane else None
    except (OSError, ValueError):
        return None


def link_terms(terms):
    """tmux 终端 → 它当前所在的 cc 会话。
    优先用启动时指定的会话(--session-id / --resume);若终端里用 /clear 开了新会话,则取同目录下满足以下条件的最新会话:
    在终端创建之后才开始、开始时原会话已不再写入问答、且没被其他终端占用。"""
    claimed = {t["launch_sid"] for t in terms if t["launch_sid"]}
    for t in terms:
        # 1. 屏幕上 cc 输入框边框显示的会话标题 → 同目录下标题相同、最近更新的会话
        sid = sid_by_title(t["cwd"], term.screen_title(t["name"]))
        if sid:
            t["sid"], t["source"] = sid, "title"
            continue
        # 2. hook 报告的当前会话(启动、/resume、/clear;后台 worker 接管时可能不触发)
        rec = pane_record(t.get("pane"))
        if rec and rec["ts"] >= t["created"] - 5:
            t["sid"] = rec["session_id"]
            t["source"] = rec.get("source") or rec.get("event")
            # 换到了新 id 但还没有问答(不是 /clear):多半是 cc 正把旧会话接续到新 id,先留在旧会话上
            cur, prev = find_session(t["sid"]), rec.get("prev") and find_session(rec["prev"])
            if rec.get("source") not in ("clear", "startup") and prev and not (cur and cached_meta(cur)["questions"]) \
                    and cached_meta(prev)["questions"]:
                t["sid"] = prev.stem
            continue
        # 2. 兜底(旧终端没有 hook 时):启动参数 + /clear 推测
        sid = t["launch_sid"]
        base = find_session(sid) if sid else None
        after = t["created"] - 5
        base_last = 0
        if base:
            bm = cached_meta(base)
            after = max(after, _epoch(bm["started"]))
            base_last = _epoch(bm["updated"]) if bm["questions"] else 0
        newer = []
        for p in project_dir_for(t["cwd"]).glob("*.jsonl"):
            if p.stem in claimed or p.stem == sid or not cached_meta(p)["questions"]:
                continue
            st = _started_epoch(p)
            if st > after and st >= base_last - 5:
                newer.append((st, p.stem))
        if newer:
            sid = max(newer)[1]
        t["sid"] = sid if sid and find_session(sid) else None
    for t in terms:  # 和网页的跳转保持一致:已被接续的会话 → 最新的那个,避免网页在新旧 id 之间来回跳
        p = t["sid"] and find_session(t["sid"])
        if p:
            t["sid"] = latest_continuation(p).stem
    return terms


def latest_sid(cwd):
    p = current_session(cwd)
    return p.stem if p else None


def note_path(sid):
    return NOTES / f"{sid}.md"


def read_note(sid):
    p = note_path(sid)
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    fm = {}
    m = re.match(r"^---\n(.*?)\n---\n?", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip()
        text = text[m.end():]
    return {"meta": fm, "body": text, "mtime": p.stat().st_mtime}


# ---------------------------------------------------------------- 搜索

def _snippet(text, words, width=90):
    """取第一个命中词附近的一段文字(折叠空白)。"""
    flat = re.sub(r"\s+", " ", text)
    low = flat.lower()
    i = min((low.find(w) for w in words if w in low), default=0)
    a = max(0, i - 30)
    return ("…" if a else "") + flat[a:a + width] + ("…" if a + width < len(flat) else "")


def search(q, scope="all", per_session=5, max_sessions=60):
    """在会话正文里搜索。scope: q = 只搜提问;all = 提问 + 回答(含过程文字)+ 整理版。
    多个词以空格分隔,同一轮内全部出现才算命中(不区分大小写)。"""
    words = [w for w in q.lower().split() if w]
    if not words:
        return {"results": [], "sessions": 0, "hits": 0}
    results, total = [], 0
    for p in all_session_files():
        try:
            if successor(p) or not cached_meta(p)["questions"]:
                continue
            meta, turns = cached_parse(p)
        except OSError:
            continue
        hits = []
        for t in turns:
            if t["kind"] not in ("question", "summary"):
                continue
            fields = [("问", t["q"])]
            if scope == "all":
                fields += [("答", x) for x in reversed(t["texts"])]  # 最终回答在最后,优先显示
            hay = "\n".join(x for _, x in fields).lower()
            if not all(w in hay for w in words):
                continue
            where, text = next(((k, x) for k, x in fields if any(w in x.lower() for w in words)), fields[0])
            hits.append({"n": t["n"], "where": where, "snip": _snippet(text, words)})
        if scope == "all":
            note = read_note(meta["id"])
            if note and all(w in note["body"].lower() for w in words):
                hits.insert(0, {"n": None, "where": "整理版", "snip": _snippet(note["body"], words)})
        if hits:
            total += len(hits)
            results.append({"id": meta["id"], "title": meta["title"], "cwd": meta["cwd"] or p.parent.name,
                            "mtime": p.stat().st_mtime, "count": len(hits), "hits": hits[:per_session]})
    return {"results": results[:max_sessions], "sessions": len(results), "hits": total}


# ---------------------------------------------------------------- 只读文件浏览

TEXT_LIMIT = 2 * 1024 * 1024
LANG_BY_EXT = {".py": "python", ".js": "javascript", ".mjs": "javascript", ".ts": "typescript", ".tsx": "typescript",
               ".jsx": "javascript", ".json": "json", ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".css": "css",
               ".html": "xml", ".xml": "xml", ".svg": "xml", ".yaml": "yaml", ".yml": "yaml", ".toml": "ini",
               ".ini": "ini", ".conf": "ini", ".md": "markdown", ".tex": "latex", ".c": "c", ".h": "c", ".cpp": "cpp",
               ".hpp": "cpp", ".go": "go", ".rs": "rust", ".java": "java", ".rb": "ruby", ".php": "php", ".sql": "sql",
               ".lua": "lua", ".r": "r", ".kt": "kotlin", ".swift": "swift", ".diff": "diff", ".patch": "diff",
               ".csv": "plaintext", ".txt": "plaintext", ".log": "plaintext", ".jsonl": "json", ".ipynb": "json"}


def fs_path(raw, cwd=None):
    f = Path(raw or "~").expanduser()
    if not f.is_absolute():
        f = Path(cwd or Path.home()).expanduser() / f
    return f.resolve()


def fs_list(path, hidden=False):
    d = fs_path(path)
    if not d.is_dir():
        raise ValueError(f"不是目录: {d}")
    entries = []
    for e in d.iterdir():
        if not hidden and e.name.startswith("."):
            continue
        try:
            st = e.stat()
            entries.append({"name": e.name, "dir": e.is_dir(), "size": st.st_size, "mtime": st.st_mtime})
        except OSError:
            entries.append({"name": e.name, "dir": False, "size": None, "mtime": None})
    entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return {"path": str(d), "parent": str(d.parent) if d != d.parent else None,
            "entries": entries[:3000], "truncated": len(entries) > 3000}


def read_regular(f, limit):
    """只读普通文件,最多 limit 字节;超过返回 None。
    O_NONBLOCK:打开 FIFO 不会卡住;fstat 检查打开后的那个文件,设备 / 管道 / socket 一律拒绝;
    读的时候限长,不依赖 stat 的大小(文件可能正在变大)。"""
    fd = os.open(f, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"不是普通文件: {f}")
        with os.fdopen(fd, "rb") as fh:
            fd = None
            data = fh.read(limit + 1)
    finally:
        if fd is not None:
            os.close(fd)
    return None if len(data) > limit else data


def fs_read(path, cwd=None):
    f = fs_path(path, cwd)
    if not f.exists():
        raise ValueError(f"文件不存在: {f}")
    if f.is_dir():
        return {"path": str(f), "kind": "dir"}
    if not f.is_file():
        raise ValueError(f"不是普通文件: {f}")
    st = f.stat()
    info = {"path": str(f), "name": f.name, "size": st.st_size, "mtime": st.st_mtime}
    ext = f.suffix.lower()
    if ext in IMAGE_TYPES:
        return {**info, "kind": "image"}
    if ext == ".pdf":
        return {**info, "kind": "pdf"}
    if st.st_size > TEXT_LIMIT:
        return {**info, "kind": "large"}
    data = read_regular(f, TEXT_LIMIT)
    if data is None:
        return {**info, "kind": "large"}
    if b"\0" in data[:8192]:
        return {**info, "kind": "binary"}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("gb18030")
        except UnicodeDecodeError:
            return {**info, "kind": "binary"}
    kind = "markdown" if ext in (".md", ".markdown") else "text"
    return {**info, "kind": kind, "text": text, "lang": LANG_BY_EXT.get(ext, "")}


# ---------------------------------------------------------------- 收藏

_stars_lock = threading.Lock()  # 收藏的"读-改-写"必须串行,否则并发请求会互相覆盖


def load_stars():
    try:
        return json.loads(STARS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save_stars(stars):
    STARS.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STARS.parent, prefix=".stars-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(stars, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, STARS)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def migrate_stars():
    """收藏所在的会话被接续后,把收藏改挂到最新的会话上(问答时间戳随历史一起复制,保持不变)。"""
    with _stars_lock:
        stars, changed = load_stars(), False
        for x in stars:
            p = find_session(x["sid"])
            latest = latest_continuation(p) if p else None
            if latest and latest != p:
                x["sid"], changed = latest.stem, True
        if changed:
            save_stars(stars)
        return stars


def toggle_star(sid, ts, on):
    """收藏以 (会话, 提问时间戳) 为键,并保存问答快照:即使原始记录被清理,收藏仍在。"""
    with _stars_lock:
        return _toggle_star(sid, ts, on)


def _toggle_star(sid, ts, on):
    stars = [x for x in load_stars() if not (x["sid"] == sid and x["ts"] == ts)]
    if on:
        p = find_session(sid)
        if not p:
            raise ValueError("会话不存在")
        meta, turns = cached_parse(p)
        t = next((t for t in turns if t["ts"] == ts), None)
        if not t:
            raise ValueError("找不到这一轮")
        stars.insert(0, {"sid": sid, "ts": ts, "n": t["n"], "title": meta["title"], "cwd": meta["cwd"],
                         "q": t["q"], "a": t["texts"][-1] if t["texts"] else "",
                         "starred_at": datetime.now().astimezone().isoformat(timespec="seconds")})
    save_stars(stars)
    return stars


# ---------------------------------------------------------------- 网页继续对话

def continue_session(sid):
    """返回运行这个会话的 tmux 终端;没有就以 --resume 在后台新开一个。"""
    terms = link_terms(term.list_sessions())
    name = next((t["name"] for t in terms if (t["sid"] or t["launch_sid"]) == sid), None)
    if name:
        return name
    p = find_session(sid)
    if not p:
        raise ValueError("会话不存在")
    if time.time() - p.stat().st_mtime < 60:
        raise ValueError("这个会话正在别的终端里运行(不是用 cc 启动的)。为避免两个进程同时写同一会话,"
                         "请在原终端里继续,或先退出它再从网页继续。")
    meta = cached_meta(p)
    return term.new_session(meta["cwd"] or str(Path.home()), ["--resume", sid])


# 每个会话最近几个版本里各轮的签名,用来算出"客户端手里的版本 → 当前版本"之间变了哪些轮
_turn_sigs = OrderedDict()  # path → OrderedDict(version → [sig, ...])
_turn_sigs_lock = threading.Lock()


def _turn_sig(t):
    return hashlib.blake2b(json.dumps(t, ensure_ascii=False, sort_keys=True).encode(), digest_size=8).hexdigest()


def changed_turns(path, have, version, turns):
    """客户端持有版本 have,返回从 have 到 version 之间变化 / 新增的轮次;
    认不出 have、整理版变了或轮次变少时返回 None,让调用方回完整数据。"""
    sigs = [_turn_sig(t) for t in turns]
    with _turn_sigs_lock:
        hist = _turn_sigs.setdefault(path, OrderedDict())
        _turn_sigs.move_to_end(path)
        old = hist.get(have)
        hist[version] = sigs
        while len(hist) > 8:
            hist.popitem(last=False)
        while len(_turn_sigs) > 32:
            _turn_sigs.popitem(last=False)
    # 版本号 = "<文件 mtime>-<整理版 mtime>";整理版变了就整页重画
    if old is None or have.rsplit("-", 1)[-1] != version.rsplit("-", 1)[-1] or len(sigs) < len(old):
        return None
    return [t for i, t in enumerate(turns) if i >= len(old) or old[i] != sigs[i]]


def session_summary(path):
    meta = cached_meta(path)
    mtime = path.stat().st_mtime
    return {
        "id": meta["id"], "title": meta["title"], "cwd": meta["cwd"] or path.parent.name,
        "first_q": meta["first_q"], "questions": meta["questions"],
        "started": meta["started"], "updated": meta["updated"], "mtime": mtime,
        "active": time.time() - mtime < ACTIVE_SECONDS,
        "noted": note_path(meta["id"]).exists() or any(note_path(x.stem).exists() for x in predecessors(path)),
    }


# ---------------------------------------------------------------- Markdown 导出(供 skill 使用)

def to_markdown(meta, turns, start=1):
    out = [f"# {meta['title']}", "",
           f"- session: `{meta['id']}`", f"- cwd: `{meta['cwd']}`",
           f"- 共 {len(turns)} 轮,本次导出第 {start} 轮起", ""]
    for t in turns:
        if t["n"] < start:
            continue
        label = {"question": "问", "command": "命令", "shell": "Shell", "summary": "上下文压缩摘要"}.get(t["kind"], t["kind"])
        out += [f"## [{t['n']}] {label}", "", t["q"] or "(无)", ""]
        if t["q_images"]:
            out += [f"(提问附带 {len(t['q_images'])} 张图片)", ""]
        if t["texts"]:
            out += [f"### [{t['n']}] 答" + (f"(用了 {t['tools']} 次工具)" if t["tools"] else ""), ""]
            out += ["\n\n".join(t["texts"]), ""]
        if t["images"]:
            out += [f"(回答过程中查看了 {len(t['images'])} 张图片)", ""]
        for n in t["notes"]:
            out += [n, ""]
    return "\n".join(out)


# ---------------------------------------------------------------- HTTP

class Handler(SimpleHTTPRequestHandler):
    # keep-alive:网页每 3 秒轮询,经过 VS Code / SSH 端口转发时复用连接,少建很多 TCP 连接
    protocol_version = "HTTP/1.1"
    timeout = 300  # 闲置的 keep-alive 连接 5 分钟后释放

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(STATIC), **kw)

    def log_message(self, *a):
        pass

    def handle_one_request(self):
        # 浏览器中途取消请求(PDF 查看器常这样做)属于正常现象,不要往日志里刷 traceback
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            self.close_connection = True

    def _send_file(self, f, ctype, extra=None):
        """流式发送文件:支持 Range(206)和 ETag 缓存校验(304)。
        PDF 查看器会按需分段读取,首屏快;重复打开未修改的文件不必重新传输。"""
        st = f.stat()
        size = st.st_size
        etag = f'"{st.st_mtime_ns:x}-{size:x}"'
        headers = {"Accept-Ranges": "bytes", "ETag": etag, "Cache-Control": "private, no-cache",
                   "Last-Modified": self.date_time_string(int(st.st_mtime)), "X-Content-Type-Options": "nosniff",
                   **(extra or {})}
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start, end, code = 0, size - 1, 200
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
        if m and size and (not self.headers.get("If-Range") or self.headers.get("If-Range") == etag):
            a, b = m.groups()
            if a:
                start, end = int(a), min(int(b), size - 1) if b else size - 1
            elif b:  # 最后 N 字节
                start = max(0, size - int(b))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            code = 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in headers.items():
            self.send_header(k, v)
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(f, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(256 * 1024, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0] if not (self.headers.get("Host") or "").startswith("[") \
            else (self.headers.get("Host") or "").split("]")[0] + "]"
        if host not in ALLOWED_HOSTS:
            self.send_error(403, "host not allowed")
            return False
        return True

    def _same_origin(self):
        """WebSocket 和 POST 必须同源:防止你浏览的其他网站偷偷连上本地终端。"""
        origin = self.headers.get("Origin")
        if origin is None or urlparse(origin).netloc != self.headers.get("Host"):
            self._json({"error": "cross-origin request rejected"}, 403)
            return False
        return True

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _bytes(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _local_image(self, path, cwd):
        """Markdown 里引用的本地图片(绝对路径、~ 或相对会话目录)。只放行位图格式,不放 SVG(可含脚本)。"""
        if path.startswith("file://"):
            path = path[7:]
        f = Path(path).expanduser()
        if not f.is_absolute():
            f = Path(cwd or Path.home()) / f
        f = f.resolve()
        mt = IMAGE_TYPES.get(f.suffix.lower())
        if not mt or not f.is_file():
            return self._json({"error": "not an image file"}, 404)
        return self._send_file(f, mt)

    def _raw_file(self, path, download):
        """原始文件。图片/PDF 按类型返回;其余一律 text/plain + CSP sandbox,
        避免 HTML/SVG 在本站同源下执行脚本(本站能连到 bypass 终端)。"""
        f = fs_path(path)
        if not f.is_file():
            return self._json({"error": "not found"}, 404)
        ext = f.suffix.lower()
        ctype = IMAGE_TYPES.get(ext) or ("application/pdf" if ext == ".pdf" else None)
        extra = {}
        if download:
            ctype = "application/octet-stream"
            from urllib.parse import quote
            extra["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(f.name)}"
        elif not ctype:
            ctype = "text/plain; charset=utf-8"
        if ext != ".pdf" or download:
            extra["Content-Security-Policy"] = "sandbox"
        return self._send_file(f, ctype, extra)

    def do_POST(self):
        if not self._host_ok() or not self._same_origin():
            return
        u = urlparse(self.path)
        try:
            m = re.fullmatch(r"/api/sessions/([\w-]+)/continue", u.path)
            if m:
                name = continue_session(m.group(1))
                return self._json({"term": name})
            if u.path == "/api/stars":
                b = self._body()
                return self._json({"stars": toggle_star(b["sid"], b["ts"], bool(b.get("on", True)))})
            m = re.fullmatch(r"/api/terms/([\w-]+)/kill", u.path)
            if m:
                term.kill_session(m.group(1))
                return self._json({"ok": True})
        except Exception as e:  # noqa: BLE001
            return self._json({"error": str(e)}, 400)
        return self._json({"error": "not found"}, 404)

    def do_GET(self):
        if not self._host_ok():
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        m = re.fullmatch(r"/ws/term/([\w-]+)", u.path)
        if m:
            if self._same_origin():
                term.bridge(self, m.group(1))
            return
        try:
            if u.path == "/api/stars":
                return self._json({"stars": migrate_stars()})
            m = re.fullmatch(r"/api/image/([\w-]+)/(\d+)", u.path)
            if m:
                p = find_session(m.group(1))
                imgs = cached_parse(p)[0]["_images"] if p else []
                k = int(m.group(2))
                if k >= len(imgs):
                    return self._json({"error": "not found"}, 404)
                mt, data = read_image(p, imgs[k])
                return self._bytes(data, mt)
            if u.path == "/api/fs/list":
                return self._json(fs_list(q.get("path", ["~"])[0], q.get("hidden", ["0"])[0] == "1"))
            if u.path == "/api/fs/read":
                return self._json(fs_read(q.get("path", [""])[0], q.get("cwd", [""])[0] or None))
            if u.path == "/api/fs/raw":
                return self._raw_file(q.get("path", [""])[0], q.get("dl", ["0"])[0] == "1")
            if u.path == "/api/file":
                return self._local_image(q.get("path", [""])[0], q.get("cwd", [""])[0])
            if u.path == "/api/search":
                return self._json(search(q.get("q", [""])[0], q.get("scope", ["all"])[0]))
            if u.path == "/api/terms":
                return self._json({"terms": link_terms(term.list_sessions())})
            if u.path == "/api/sessions":
                terms = link_terms(term.list_sessions())
                running = {t["sid"] or t["launch_sid"]: t["name"] for t in terms}
                items = []
                for t in terms:  # 刚启动、还没提问的会话也列出来
                    sid = t["sid"] or t["launch_sid"]
                    if sid and not (find_session(sid) and cached_meta(find_session(sid))["questions"]):
                        items.append({"id": sid, "title": "新会话(尚未提问)", "cwd": t["cwd"], "first_q": "",
                                      "questions": 0, "started": None, "updated": None, "mtime": t["created"],
                                      "active": True, "noted": False, "running": t["name"]})
                for p in all_session_files():
                    try:
                        if successor(p):  # 已在新会话里继续,只列最新的那个
                            continue
                        s = session_summary(p)
                    except OSError:
                        continue
                    if s["questions"]:
                        s["running"] = running.get(s["id"])
                        items.append(s)
                return self._json({"sessions": items, "terms": terms, "now": time.time()})
            m = re.fullmatch(r"/api/session/([\w-]+)", u.path)
            if m:
                p = find_session(m.group(1))
                if not p or not cached_meta(p)["questions"]:
                    t = next((t for t in link_terms(term.list_sessions())
                              if (t["sid"] or t["launch_sid"]) == m.group(1)), None)
                    if t:
                        return self._json({"pending": True, "active": True, "version": "pending", "note": None, "turns": [],
                                           "meta": {"id": m.group(1), "title": "新会话(尚未提问)", "cwd": t["cwd"],
                                                    "questions": 0, "started": None, "updated": None}})
                if not p:
                    return self._json({"error": "not found"}, 404)
                latest = latest_continuation(p)
                if latest != p:
                    return self._json({"moved": latest.stem})
                note = find_note(p)
                version = f"{p.stat().st_mtime_ns}-{note['mtime'] if note else 0}"
                have = q.get("v", [""])[0]
                if have == version:
                    return self._json({"unchanged": True, "version": version})
                meta, turns = cached_parse(p)
                meta = {k: v for k, v in meta.items() if k != "_images"}
                res = {"meta": meta, "version": version, "active": time.time() - p.stat().st_mtime < ACTIVE_SECONDS}
                changed = changed_turns(p, have, version, turns)
                if changed is not None:  # 进行中的会话:只回变化的轮次,前端只重画这些
                    return self._json({**res, "delta": True, "count": len(turns), "turns": changed})
                return self._json({**res, "turns": turns, "note": note})
        except Exception as e:  # noqa: BLE001  本地工具,错误直接回给页面
            return self._json({"error": repr(e)}, 500)
        if u.path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        if u.path != "/" and not (STATIC / u.path.lstrip("/")).exists():
            self.path = "/"
        return super().do_GET()


def port_open():
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((HOST if HOST != "0.0.0.0" else "127.0.0.1", PORT)) == 0


def url(sid=None):
    return f"http://localhost:{PORT}/" + (f"#/s/{sid}" if sid else "")


def cmd_serve(_):
    NOTES.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"MeowNotebook: {url()}  (读取 {PROJECTS})", flush=True)
    srv.serve_forever()


def cmd_ensure(args):
    if not port_open():
        log = open(ROOT / "data" / "server.log", "a")
        subprocess.Popen([sys.executable, str(ROOT / "ccnb.py"), "serve"], stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, start_new_session=True, cwd=str(ROOT), env=term.clean_env(os.environ))
        for _ in range(30):
            if port_open():
                break
            time.sleep(0.1)
    print(("运行中: " if port_open() else "启动失败,见 data/server.log: ") + url(args.session))


def cmd_launch(args):
    """cc 命令的实现:启动网站 + 在 tmux 里以 bypass 模式运行 cc,然后本地 attach。"""
    cmd_ensure(argparse.Namespace(session=None))
    try:
        cols, rows = os.get_terminal_size()
    except OSError:
        cols, rows = 200, 50
    name = term.new_session(os.getcwd(), args.cc_args, cols, rows, latest_sid=latest_sid(os.getcwd()))
    print(f"网页终端: http://localhost:{PORT}/#/t/{name}")
    first = ROOT / "data" / ".launched"
    if not first.exists():  # 第一次使用时提醒一次
        print(FIRST_RUN_NOTICE)
        first.parent.mkdir(parents=True, exist_ok=True)
        first.touch()
        if sys.stdin.isatty() and not args.detach:
            input("按回车继续…")
    if args.detach or not sys.stdin.isatty():
        return
    term.attach(name)
    if term.session_exists(name):  # 只是离开了终端(detach / 关窗口),cc 还在跑
        print(f"\n已离开终端,cc 仍在后台运行。\n  网页: http://localhost:{PORT}/#/t/{name}\n"
              f"  回到终端: cca {name}    结束: 网页上点「结束会话」,或在终端里 /exit")


FIRST_RUN_NOTICE = """
⚠ 第一次使用 cc,几点提醒(只显示这一次,完整说明见 README):
  1. cc 以 --dangerously-skip-permissions 运行:执行命令、改文件都不再询问,只在信任的目录里用。
  2. 网页终端等于一个免密的 bypass shell,只监听 127.0.0.1。远程服务器请用 VS Code 端口转发
     或 ssh -L 8765:localhost:8765 访问;不要把 8765 暴露到公网。
  3. 关掉网页或断开 SSH 不会结束 cc;用 ccls 查看、cca 回到终端。
  4. cc 遮住了 C 编译器 /usr/bin/cc,需要时用 command cc 或 gcc。
"""


def resolve(args):
    if getattr(args, "session", None):
        p = find_session(args.session)
    else:
        p = current_session(getattr(args, "cwd", None))
    if not p:
        sys.exit("找不到会话")
    return p


def cmd_extract(args):
    p = resolve(args)
    meta, turns = parse_session(p)
    start = 1
    if args.new_only:
        note = read_note(meta["id"])
        if note:
            start = int(note["meta"].get("covered_turns", "0") or 0) + 1
    print(f"<!-- note_file: {note_path(meta['id'])} -->")
    print(f"<!-- total_turns: {len(turns)} -->")
    print(to_markdown(meta, turns, start))


def cmd_current(args):
    print(resolve(args).stem)


def cmd_list(args):
    for p in all_session_files():
        if args.project and project_dir_for(args.project) != p.parent or successor(p):
            continue
        s = session_summary(p)
        if s["questions"]:
            ts = datetime.fromtimestamp(s["mtime"], timezone.utc).astimezone().strftime("%m-%d %H:%M")
            flag = ("● " if s["active"] else "  ") + ("✎" if s["noted"] else " ")
            print(f"{flag} {ts}  {s['id']}  {s['questions']:>3}问  {s['title']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve").set_defaults(fn=cmd_serve)
    e = sub.add_parser("ensure-server")
    e.add_argument("--session")
    e.set_defaults(fn=cmd_ensure)
    x = sub.add_parser("extract")
    g = x.add_mutually_exclusive_group(required=True)
    g.add_argument("--current", action="store_true")
    g.add_argument("--session")
    x.add_argument("--cwd")
    x.add_argument("--new-only", action="store_true", help="只导出整理稿尚未覆盖的轮次")
    x.set_defaults(fn=cmd_extract)
    c = sub.add_parser("current")
    c.add_argument("--cwd")
    c.set_defaults(fn=cmd_current)
    la = sub.add_parser("launch")
    la.add_argument("--detach", action="store_true", help="只在后台启动,不在本终端 attach")
    la.add_argument("cc_args", nargs=argparse.REMAINDER)
    la.set_defaults(fn=cmd_launch)
    at = sub.add_parser("attach")
    at.add_argument("name")
    at.set_defaults(fn=lambda a: sys.exit(term.attach(a.name)))
    ls = sub.add_parser("list")
    ls.add_argument("--project")
    ls.set_defaults(fn=cmd_list)
    argv = sys.argv[1:]
    if argv[:1] == ["launch"]:  # 其余参数原样交给 cc(argparse 会误吞 -c / --resume 这类参数)
        detach = argv[1:2] == ["--detach"]
        return cmd_launch(argparse.Namespace(detach=detach, cc_args=argv[2 if detach else 1:]))
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
