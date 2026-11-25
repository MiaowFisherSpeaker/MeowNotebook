#!/usr/bin/env python3
"""MeowNotebook: 把 cc 的对话整理成可浏览的问答网站。

子命令:
  serve                    启动本地网站(默认 127.0.0.1:8765)
  ensure-server            若未运行则后台启动,打印网址
  list [--project DIR]     列出会话
  extract (--current | --session ID) [--new-only]
                           以 Markdown 输出某会话的问答(供 /qa-notes skill 整理)
  current                  打印当前目录最新会话的 ID
  link [--session ID] [--turn N]
                           打印会话(某一轮)的网页链接;在 cc 里调用时默认是调用它的那个会话
  pending [--days N]       列出最近 N 天(默认 1 = 今天)更新过、整理版还没覆盖全部轮次的会话
  qq setup                 QQ 机器人:输入 AppID / AppSecret(不回显),写 data/qqbot.json 并验证
  qq probe                 QQ 机器人:只连接并打印收到的所有事件(排查"收不到消息")
  enter [名字]             cca:列出运行中的会话选择进入(↑↓ 回车,x 结束);给名字则直接进那个终端
  launch [--detach] [cc 参数...]
                           在当前目录以 bypass 模式启动 cc(tmux 会话),
                           并确保网站运行;本地终端与网页终端共享同一会话
"""
import argparse
import base64
import gzip
import hashlib
import html
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
import unicodedata
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import ProxyHandler, build_opener

import qqbot
import term
import updater

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
NOTES = ROOT / "data" / "notes"
STARS = ROOT / "data" / "stars.json"
SUMMARIES = ROOT / "data" / "summaries"
PROJECTS = term.CC_HOME / "projects"
PROCS = term.CC_HOME / "sessions"  # cc 给每个运行中的进程登记的 <pid>.json(当前会话 ID、目录…)
HOST = os.environ.get("CCNB_HOST", "127.0.0.1")
PORT = int(os.environ.get("CCNB_PORT", "8765"))
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp", ".bmp": "image/bmp", ".avif": "image/avif"}
ACTIVE_SECONDS = 180  # 文件在这么多秒内被写过 → 视为进行中
# 只接受这些主机名的请求(防 DNS rebinding);端口不限,方便 VS Code / SSH 转发到别的本地端口
# 网站进程启动时的版本(serve 时填):更新后网页据此提示刷新
VERSION_INFO = {"version": None}
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
    "task": re.compile(r"<task-notification>(.*?)</task-notification>", re.S),
}


def _task_notification(body):
    """后台任务(子 agent 等)完成的通知 → 一行标题 + 结果正文。
    通知把结果按 XML 转义过(< → &lt;),原样显示时公式里的 $s&gt;0$ 会被 KaTeX 当成 TeX 原样画出来甚至整条失败,
    所以结果要先还原;task-id、输出文件路径、给 cc 的说明和用量这些对读者没用的部分不显示。"""
    def tag(name):
        m = re.search(rf"<{name}>(.*?)</{name}>", body, re.S)
        return html.unescape(m.group(1).strip()) if m else ""
    head = tag("summary") or f"后台任务 {tag('status') or ''}".strip()
    result = tag("result")
    return f"**⚙ {head}**" + (f"\n\n{result}" if result else "")
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
    m = TAG_RE["task"].search(text)
    if m:
        rest = TAG_RE["task"].sub("", text).strip()
        return "question", (_task_notification(m.group(1)) + (f"\n\n{rest}" if rest else "")).strip()
    text = TAG_RE["pasted"].sub(lambda mm: "\n> " + mm.group(1).strip().replace("\n", "\n> ") + "\n", text)
    text = TAG_RE["cmd_msg"].sub("", text)
    return "question", text.strip()


class SessionParser:
    """把一个 .jsonl 会话解析成问答轮次。会话记录只会在末尾追加,所以解析状态留着:
    文件变长后 feed() 只读新增的行(进行中的长会话每秒都在写,不必每次从头解析几十 MB)。"""

    TAIL = 64  # 记下已读部分最后这么多字节,续读前核对,文件被改写过就从头解析

    def __init__(self, path):
        self.path = path
        self.meta = {"id": path.stem, "file": str(path), "cwd": None, "title": None, "cli": "claude",
                     "ai_title": None, "first_q": None, "started": None, "updated": None, "continued_in": None}
        self.turns = []
        # 图片只记位置 (类型, 所在行的字节偏移, 该行第几个 base64 块),turn 里存索引;
        # /api/image/<sid>/<k> 时再回文件读那一行。图片数据不进缓存(动辄几十 MB)
        self.images = []
        self.cur = None
        self.seen_text = set()  # 同一条 assistant 消息可能被重复写入
        self.pos = 0  # 已解析到的字节位置(总在一行的末尾)
        self.tail = b""
        self.ino = None
        self.lock = threading.Lock()

    def can_resume(self, st):
        if st.st_ino != self.ino or st.st_size < self.pos:
            return False
        if not self.pos:
            return True
        with open(self.path, "rb") as f:
            f.seek(self.pos - len(self.tail))
            return f.read(len(self.tail)) == self.tail

    def feed(self):
        """从上次停下的地方读到文件末尾的最后一个完整行(写到一半的行留到下次)。"""
        with open(self.path, "rb") as f:
            self.ino = os.fstat(f.fileno()).st_ino
            f.seek(self.pos)
            data = f.read()
        if not data:
            return
        # 之前交出去的结果和 cur 共用列表;cur 还会继续追加,先换成副本,已交出的结果不受影响
        if self.cur is not None:
            c = self.cur
            self.cur = {**c, "texts": list(c["texts"]), "notes": list(c["notes"]),
                        "q_images": list(c["q_images"]), "images": list(c["images"]), "follows": list(c["follows"])}
            self.turns[-1] = self.cur
        lines = data.split(b"\n")
        rest = lines.pop()  # 最后一个换行之后的部分:多半是还没写完的一行
        pos = self.pos
        for raw in lines:
            self._line(raw, pos)
            pos += len(raw) + 1
        if rest and self._complete(rest):  # 结尾没有换行但已是完整的一条
            self._line(rest, pos)
            pos += len(rest)
        self.pos = pos
        if pos:
            with open(self.path, "rb") as f:
                f.seek(max(0, pos - self.TAIL))
                self.tail = f.read(pos - max(0, pos - self.TAIL))

    @staticmethod
    def _complete(raw):
        try:
            json.loads(raw.decode("utf-8", errors="replace"))
            return True
        except ValueError:
            return False

    def _follow(self, d):
        """回答进行中用户插进来的消息:cc 记成 queued_command 附件,不是 user 消息。挂到当前这一轮,编号 #n-1、#n-2…"""
        a = d.get("attachment") or {}
        if a.get("type") != "queued_command" or a.get("commandMode") not in (None, "prompt") \
                or (a.get("origin") or {}).get("kind") not in (None, "human"):
            return
        p = a.get("prompt")
        text = p if isinstance(p, str) else "\n\n".join(b.get("text", "") for b in _blocks(p) if b.get("type") == "text")
        kind, text = _clean_user_text(text or "")
        if kind != "question" or not text.strip():
            return
        ts = a.get("timestamp") or d.get("timestamp")
        if self.cur is None:
            self._new_turn("question", text.strip(), ts)
            self.meta["first_q"] = self.meta["first_q"] or text.strip()[:200]
            return
        self.cur["follows"].append({"q": text.strip(), "ts": ts, "at": len(self.cur["texts"])})
        self.meta["updated"] = ts or self.meta["updated"]

    def _new_turn(self, kind, text, ts):
        # follows:回答进行中插进来的追问(cc 里 Ctrl+X Ctrl+S "send now"),{q, ts, at = 插入时已有几段回答文字}
        self.cur = {"kind": kind, "q": text, "ts": ts, "texts": [], "tools": 0, "notes": [], "q_images": [], "images": [],
                    "follows": []}
        self.turns.append(self.cur)

    def _line(self, raw, offset):
        try:
            d = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            return
        if not isinstance(d, dict):
            return
        meta = self.meta
        line_b64 = None  # 这一行里所有 base64 块的 id(),按出现顺序

        def add_image(src):
            nonlocal line_b64
            if isinstance(src, dict) and src.get("type") == "base64" and src.get("data"):
                if line_b64 is None:
                    line_b64 = [id(x) for x in _base64_blocks(d)]
                self.images.append((src.get("media_type") or "image/png", offset, line_b64.index(id(src))))
                return len(self.images) - 1
            return None

        t = d.get("type")
        if t == "ai-title":
            meta["ai_title"] = d.get("aiTitle")
            return
        if t == "continued-in":  # 从 agent 视图重新进入会话时,cc 会把历史复制到新会话继续,并在旧会话末尾留下这条记录
            meta["continued_in"] = d.get("continuedInSessionId")
            return
        if t in ("custom-title", "agent-name"):
            meta["title"] = d.get("customTitle") or d.get("agentName") or meta["title"]
            return
        if t == "attachment" and not d.get("isSidechain"):
            self._follow(d)
            return
        if t not in ("user", "assistant") or d.get("isSidechain"):
            return
        ts = d.get("timestamp")
        meta["cwd"] = meta["cwd"] or d.get("cwd")
        meta["started"] = meta["started"] or ts
        meta["updated"] = ts or meta["updated"]
        msg = d.get("message") or {}
        cur = self.cur

        if t == "user":
            if d.get("isCompactSummary"):
                txt = "".join(b.get("text", "") for b in _blocks(msg.get("content")) if b.get("type") == "text")
                self._new_turn("summary", txt, ts)
                return
            if d.get("isMeta"):
                return
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
                return
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
                return
            kind = "question" if "question" in kinds or n_img else kinds[0]
            text = "\n\n".join(parts)
            if kind == "interrupt":
                if cur:
                    cur["notes"].append("⏹ 用户中断")
                return
            if kind == "output":
                if cur:
                    cur["notes"].append("```\n" + text[:2000] + "\n```")
                return
            self._new_turn(kind, text.strip() or "(图片)", ts)
            self.cur["q_images"] = q_imgs
            if kind == "question" and not meta["first_q"]:
                meta["first_q"] = text.strip()[:200]
        else:
            if cur is None:
                self._new_turn("question", "", ts)
                cur = self.cur
            for b in _blocks(msg.get("content")):
                bt = b.get("type")
                if bt == "text":
                    txt = b.get("text", "").strip()
                    key = (msg.get("id"), txt)
                    if txt and key not in self.seen_text and txt != "No response requested.":
                        self.seen_text.add(key)
                        cur["texts"].append(txt)
                elif bt == "tool_use":
                    cur["tools"] += 1

    def result(self):
        """当前的 (meta, turns)。每次都是新的 dict,之后继续 feed() 不会改动已交出的结果。"""
        turns = [{**t, "n": i} for i, t in enumerate((t for t in self.turns if t["q"] or t["texts"] or t["images"]), 1)]
        meta = dict(self.meta)
        meta["title"] = meta["title"] or meta["ai_title"] or (meta["first_q"] or meta["id"])[:60]
        meta["questions"] = sum(1 for t in turns if t["kind"] == "question")
        meta["_images"] = list(self.images)
        return meta, turns


# ---------------------------------------------------------------- codex 的会话记录
# codex 把每个会话写成 ~/.codex/sessions/YYYY/MM/DD/rollout-<时间>-<会话 ID>.jsonl,标题在 session_index.jsonl。
# 网站各处都以"记录文件名 = 会话 ID"认会话,所以在 data/codex/ 下给每个记录建一个 <会话 ID>.jsonl 的软链接,
# 其余代码(列表、搜索、收藏、整理版、总结…)不用分两套;解析时按所在目录换成 CodexParser。
CODEX_SESSIONS = term.CODEX_HOME / "sessions"
CODEX_INDEX = term.CODEX_HOME / "session_index.jsonl"
CODEX_LINKS = ROOT / "data" / "codex"
_ROLLOUT_RE = re.compile(r"rollout-.+-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")
_codex_sync = [0.0]
_codex_sync_lock = threading.Lock()


def is_codex(path):
    return path.parent == CODEX_LINKS


def sync_codex_links(force=False):
    """让 data/codex/ 里的软链接和 codex 的记录一一对应:新会话加上,删掉 / 归档的去掉。最多 5 秒一次。"""
    with _codex_sync_lock:
        if not force and time.monotonic() - _codex_sync[0] < 5:
            return
        _codex_sync[0] = time.monotonic()
        want = {}
        if CODEX_SESSIONS.is_dir():
            for f in CODEX_SESSIONS.glob("*/*/*/rollout-*.jsonl"):
                m = _ROLLOUT_RE.search(f.name)
                if m:
                    want[m.group(1)] = str(f)
        if not want and not CODEX_LINKS.is_dir():
            return
        CODEX_LINKS.mkdir(parents=True, exist_ok=True)
        for link in CODEX_LINKS.glob("*.jsonl"):
            try:
                target = os.readlink(link)
            except OSError:
                target = None
            if want.get(link.stem) == target:
                del want[link.stem]
            else:
                link.unlink(missing_ok=True)
        for sid, f in want.items():
            try:
                os.symlink(f, CODEX_LINKS / f"{sid}.jsonl")
            except OSError:
                pass


_codex_names = [None, {}]


def codex_names():
    """codex 给会话起的标题(session_index.jsonl,同一个会话以最后一条为准)。"""
    try:
        st = CODEX_INDEX.stat()
    except OSError:
        return {}
    key = (st.st_mtime_ns, st.st_size)
    if _codex_names[0] != key:
        names = {}
        try:
            for line in CODEX_INDEX.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    d = json.loads(line)
                    if d.get("id") and d.get("thread_name"):
                        names[d["id"]] = d["thread_name"]
                except ValueError:
                    pass
        except OSError:
            pass
        _codex_names[:] = [key, names]
    return _codex_names[1]


class CodexParser(SessionParser):
    """codex 的记录 → 和 cc 一样的问答轮次。只看整理好的条目(event_msg item_completed):
    UserMessage 是一问,AgentMessage 是回答(commentary 是过程文字,final_answer 是最后的回答);
    同一个 turn_id 里后来的 UserMessage 是回答进行中插进来的(引导),记成 #n-1 这样的追问。
    response_item 里的工具调用只计数;那里的 user 消息带着 codex 自己塞的环境说明,不用。"""

    def __init__(self, path):
        super().__init__(path)
        self.meta["cli"] = "codex"
        self.turn_id = None
        self.seen_ids = set()

    def _line(self, raw, offset):
        try:
            d = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            return
        if not isinstance(d, dict):
            return
        t, p, ts = d.get("type"), d.get("payload"), d.get("timestamp")
        if not isinstance(p, dict):
            return
        meta, cur = self.meta, self.cur
        if t == "session_meta":
            meta["cwd"] = meta["cwd"] or p.get("cwd")
            meta["started"] = meta["started"] or p.get("timestamp") or ts
            return
        if t == "compacted":  # 上下文压缩:和 cc 的压缩摘要一样单独一轮
            if p.get("message"):
                self._new_turn("summary", p["message"], ts)
                self.turn_id = None
            return
        if t == "response_item":
            if str(p.get("type", "")).endswith("_call") and cur is not None:
                cur["tools"] += 1
            return
        if t != "event_msg":
            return
        et = p.get("type")
        if et == "turn_aborted":
            if cur is not None:
                cur["notes"].append("⏹ 用户中断")
            return
        if et != "item_completed":
            return
        it = p.get("item") or {}
        kind, iid = it.get("type"), it.get("id")
        if kind not in ("UserMessage", "AgentMessage") or (iid and iid in self.seen_ids):
            return
        if iid:
            self.seen_ids.add(iid)
        content = it.get("content") if isinstance(it.get("content"), list) else []
        meta["started"] = meta["started"] or ts
        meta["updated"] = ts or meta["updated"]
        if kind == "UserMessage":
            text = "\n\n".join(c.get("text", "") for c in content if c.get("type") == "text" and c.get("text")).strip()
            if not text and any("image" in str(c.get("type", "")) for c in content):
                text = "(图片)"
            if not text:
                return
            tid = p.get("turn_id")
            if cur is not None and tid and tid == self.turn_id:
                cur["follows"].append({"q": text, "ts": ts, "at": len(cur["texts"])})
                return
            self.turn_id = tid
            self._new_turn("question", text, ts)
            meta["first_q"] = meta["first_q"] or text[:200]
        else:
            txt = "\n\n".join(c.get("text", "") for c in content if c.get("text")).strip()
            if not txt:
                return
            if cur is None:
                self._new_turn("question", "", ts)
            self.cur["texts"].append(txt)

    def result(self):
        self.meta["ai_title"] = codex_names().get(self.meta["id"]) or self.meta["ai_title"]
        return super().result()


def parser_for(path):
    return CodexParser(path) if is_codex(path) else SessionParser(path)


def parse_session(path):
    """把一个 .jsonl 会话解析成问答轮次。"""
    p = parser_for(path)
    p.feed()
    return p.result()


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


# 两级缓存:元数据(列表、索引用)很小,常驻;完整问答按文本量做 LRU。键都是 (mtime_ns, size),文件一变就失效。
# 完整问答的缓存里还留着解析器:文件只是变长了(进行中的会话)就接着读新增的行,不从头解析
_meta_cache = {}
_full_cache = OrderedDict()  # path → (key, (meta, turns), size, parser)
FULL_CACHE_BUDGET = 32 * 1024 * 1024  # 完整问答的文本总量上限(字符数,近似)
_full_cache_size = 0
_cache_lock = threading.Lock()


def _file_key(path):
    st = path.stat()
    return st.st_mtime_ns, st.st_size


def _turns_size(turns):
    return sum(len(t["q"] or "") + sum(map(len, t["texts"])) + sum(map(len, t["notes"])) + 200
               + sum(len(x["q"]) for x in t.get("follows", ())) for t in turns)


def cached_parse(path):
    """完整解析结果 (meta, turns),LRU 缓存。"""
    global _full_cache_size
    st = path.stat()
    key = st.st_mtime_ns, st.st_size
    with _cache_lock:
        hit = _full_cache.get(path)
        if hit and hit[0] == key:
            _full_cache.move_to_end(path)
            return hit[1]
    parser = hit[3] if hit else None
    with (parser.lock if parser else threading.Lock()):  # 同一个解析器不能两个线程同时续读
        if parser is None or not parser.can_resume(st):
            parser = parser_for(path)
        parser.feed()
        res = parser.result()
    size = _turns_size(res[1])
    with _cache_lock:
        old = _full_cache.pop(path, None)
        if old:
            _full_cache_size -= old[2]
        _full_cache[path] = (key, res, size, parser)
        _full_cache_size += size
        while _full_cache_size > FULL_CACHE_BUDGET and len(_full_cache) > 1:
            _, (_, _, sz, _) = _full_cache.popitem(last=False)
            _full_cache_size -= sz
        _meta_cache[path] = (key, res[0])
    _meta_dirty.set()
    return res


# 元数据缓存另存一份到磁盘:网站重启后第一次列会话不必把几百 MB 的记录全解析一遍。
# 解析代码(本文件)一变,旧的缓存就不用了
META_CACHE = ROOT / "data" / "meta-cache.json"
_meta_dirty = threading.Event()
_meta_loaded = False
_meta_saved = 0.0


def _code_stamp():
    st = Path(__file__).stat()
    return f"{st.st_mtime_ns}-{st.st_size}"


def _read_meta_cache():
    try:
        d = json.loads(META_CACHE.read_text(encoding="utf-8"))
        if d.get("code") == _code_stamp() and isinstance(d.get("files"), dict):
            return d["files"]
    except (OSError, ValueError):
        pass
    return {}


def _load_meta_cache():
    global _meta_loaded
    if _meta_loaded:
        return
    _meta_loaded = True
    with _cache_lock:
        for p, v in _read_meta_cache().items():
            try:
                ns, size, meta = v
                _meta_cache.setdefault(Path(p), ((ns, size), meta))
            except (TypeError, ValueError):
                pass


def _save_meta_cache(force=False):
    """有变化时写盘,最多 10 秒一次(进行中的会话每秒都在变)。"""
    global _meta_saved
    if not _meta_dirty.is_set() or (not force and time.monotonic() - _meta_saved < 10):
        return
    _meta_dirty.clear()
    _meta_saved = time.monotonic()
    files = _read_meta_cache()  # 别的进程(命令行、另一个网站进程)写进去的也留着
    with _cache_lock:
        files.update({str(p): [k[0], k[1], {x: v for x, v in m.items() if x != "_images"}]
                      for p, (k, m) in _meta_cache.items()})
    files = {p: v for p, v in files.items() if os.path.exists(p)}
    try:
        META_CACHE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=META_CACHE.parent, prefix=".meta-cache.")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"code": _code_stamp(), "files": files}, f, ensure_ascii=False)
        os.replace(tmp, META_CACHE)
    except OSError:
        pass


def cached_meta(path):
    """只要元数据(标题、时间、问题数、接续关系…)时用这个:命中时不碰完整问答缓存。"""
    _load_meta_cache()
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
    if re.fullmatch(r"[0-9a-f-]{36}", sid or ""):  # 刚开始写的 codex 会话:软链接可能还没建
        sync_codex_links(force=not (CODEX_LINKS / f"{sid}.jsonl").exists())
        p = CODEX_LINKS / f"{sid}.jsonl"
        if p.exists():
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
    sync_codex_links()
    for p in [*PROJECTS.glob("*/*.jsonl"), *CODEX_LINKS.glob("*.jsonl")]:
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
    _save_meta_cache()
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
    try:
        cwd_r = str(Path(cwd).resolve())
    except OSError:
        cwd_r = cwd
    idx = session_index()
    loose = None
    for p, _ in idx.files:  # 从新到旧,第一个匹配的就是最近更新的
        meta = idx.meta[p]
        # 从 agent 视图 fork 出的会话可能记在别的项目目录里(如 worktree 的目录),但记录里的 cwd 还是原目录
        if p.parent != d and meta["cwd"] != cwd_r:
            continue
        names = {meta["title"], meta["ai_title"]} - {None}
        if title in names:  # 完全相同优先:fork 的标题是"原标题 ⑂ …",截断后的前缀也会匹配原会话
            return p.stem
        if loose is None and prefix and any(n.startswith(prefix) for n in names):
            loose = p.stem
    return loose


def pane_record(pane):
    try:
        return json.loads((ROOT / "data" / "panes" / f"{pane}.json").read_text()) if pane else None
    except (OSError, ValueError):
        return None


def _proc_start(pid):
    try:  # /proc/<pid>/stat 第 22 项:进程启动时间(时钟滴答),和登记里的 procStart 对上才是同一个进程
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


_procs = (0.0, None)


def live_processes():
    """正在运行的 cc 进程:[{pid, sid, cwd, platform}]。只认 pid 还活着、启动时间和登记一致的(防 pid 复用)。
    platform:正在处理 QQ 消息的进程是 "qq",否则 None。cc 版本太老、没有这份登记时返回 None。"""
    global _procs
    if time.monotonic() - _procs[0] < INDEX_MAX_AGE:
        return _procs[1]
    out = None
    if PROCS.is_dir():
        out = []
        for f in PROCS.glob("*.json"):
            try:
                rec = json.loads(f.read_text())
                pid, sid = int(rec["pid"]), str(rec["sessionId"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            start = _proc_start(pid)
            if start is None or ("procStart" in rec and str(rec["procStart"]) != start):
                continue
            # kind=bg:cc 的后台会话(agent 视图里 fork 出的会话也是),job 是 `claude attach` 用的 id
            out.append({"pid": pid, "sid": sid, "cwd": rec.get("cwd"), "platform": "qq" if pid in qqbot.RUNNING else None,
                        "job": rec.get("jobId") if rec.get("kind") == "bg" else None})
    _procs = (time.monotonic(), out)
    return out


def _proc_tree(pid):
    """pid 和它的所有子孙进程(读 /proc/<pid>/task/*/children)。"""
    out, todo = [], [pid]
    while todo and len(out) < 200:
        x = todo.pop()
        out.append(x)
        for task in Path(f"/proc/{x}/task").glob("*/children"):
            try:
                todo += [int(c) for c in task.read_text().split()]
            except (OSError, ValueError):
                pass
    return out


def _open_rollouts(pid):
    """这个进程正开着的 codex 记录 → [会话 ID]。codex 运行时一直开着当前会话的 rollout 文件(/resume、/new 后换成新的)。"""
    ids = []
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return ids
    for fd in fds:
        try:
            m = _ROLLOUT_RE.search(os.readlink(f"/proc/{pid}/fd/{fd}"))
        except OSError:
            continue
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    return ids


def codex_term_sid(t):
    """codex 终端此刻所在的会话:窗格里的 codex 进程开着哪个 rollout 文件。还没写出记录(没提问)时 None。"""
    if not t.get("pid"):
        return None
    ids = [x for pid in _proc_tree(t["pid"]) for x in _open_rollouts(pid)]
    if len(ids) > 1:  # 一般只有一个;万一有几个,取最近写过的
        ids.sort(key=lambda x: (find_session(x).stat().st_mtime if find_session(x) else 0), reverse=True)
    return ids[0] if ids else None


_codex_procs = (0.0, None)


def codex_holders():
    """所有开着 codex 记录的 codex 进程:{会话 ID: (pid, 是不是 codex 的后台服务)}。
    别处开的 codex 默认把会话交给共享的后台服务(app-server)跑,记录由它开着;那个 codex 窗口关掉后它就放开了。"""
    global _codex_procs
    if time.monotonic() - _codex_procs[0] < INDEX_MAX_AGE:
        return _codex_procs[1]
    out = {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            if (d / "comm").read_text().strip() != "codex":
                continue
            daemon = b"app-server" in (d / "cmdline").read_bytes()
        except OSError:
            continue
        for sid in _open_rollouts(int(d.name)):
            out.setdefault(sid, (int(d.name), daemon))
    _codex_procs = (time.monotonic(), out)
    return out


def session_owner(path, terms=None):
    """不是网页能接管的(cc 启动的 tmux 终端之外的)、正在使用这个会话的 cc 进程,没有则 None。
    同一条接续链上的会话算同一个(cc 接续时会换新 id)。"""
    if is_codex(path):  # codex:看哪个 codex 进程开着这个会话的记录,不是网页终端里的就是"别的终端"
        pid, daemon = codex_holders().get(path.stem) or (None, False)
        if pid:
            ours = {x for t in (terms if terms is not None else term.list_sessions()) if t.get("cli") == "codex" and t.get("pid")
                    for x in _proc_tree(t["pid"])}
            if pid not in ours:
                owner = {"pid": pid, "label": "别的 codex 窗口" if daemon else "别的终端", "platform": None, "job": None}
                msg = (f"这个会话正在别的 codex 窗口里打开着(由 codex 后台服务 pid {pid} 运行)。为避免两个进程同时写同一会话,"
                       "请在那边继续,或先关掉那个 codex 再从网页继续。") if daemon else owner_message(owner)
                return {**owner, "message": msg}
        return None
    procs = live_processes()
    if procs is None:  # 老版本 cc:只能看记录文件最近有没有被写过
        if time.time() - path.stat().st_mtime < 60:
            owner = {"pid": None, "label": "别的终端"}
            return {**owner, "message": owner_message(owner)}
        return None
    ours = {t["pid"] for t in (terms if terms is not None else term.list_sessions())}
    head = latest_continuation(path)
    for pr in procs:
        q = find_session(pr["sid"])
        if pr["pid"] not in ours and (pr["sid"] == path.stem or (q and latest_continuation(q) == head)):
            owner = {"pid": pr["pid"], "label": "QQ" if pr["platform"] else "cc 后台" if pr["job"] else "别的终端",
                     "platform": pr["platform"], "job": None if pr["platform"] else pr["job"]}
            return {**owner, "message": owner_message(owner)}
    return None


def owner_message(owner):
    who = f"pid {owner['pid']}" if owner.get("pid") else "不是用 cc 启动的"
    if owner.get("platform") == "qq":
        return f"cc 正在回答 QQ 发来的消息({who})。为避免两个进程同时写同一会话,等它回复完再从网页继续。"
    if owner.get("job"):
        return (f"这个会话在 cc 的后台进程里运行({who},agent 视图里 fork 出的会话都是这样)。"
                f"点上面的按钮会在下方以 claude attach {owner['job']} 接入它,关掉终端后台会话照常运行。")
    return (f"这个会话正在别的终端里运行({who})。为避免两个进程同时写同一会话,"
            "请在原终端里继续,或先退出它再从网页继续。")


def session_via(path):
    """会话是在 QQ 里开的 → "qq";本机终端里开的返回 None。"""
    origins = qqbot.origins()
    return "qq" if any(q.stem in origins for q in [path, *predecessors(path)]) else None


def qq_owner(sid):
    """QQ 机器人要继续这个会话前问一下:它是不是正开在网页终端 / 别的终端里。返回占用者名称或 None。"""
    p = find_session(sid)
    if not p:
        return None
    terms = link_terms(term.list_sessions())
    head = latest_continuation(p).stem
    if any((t["sid"] or t["launch_sid"]) in (sid, head) for t in terms):
        return "网页终端"
    o = session_owner(p, terms)
    return o["label"] if o and o.get("platform") != "qq" else None


_title_memo = {}  # 终端名 → (最近一次从屏幕标题认出的会话, 当时的登记 / hook 状态)


def link_terms(terms):
    """tmux 终端 → 它当前所在的 cc 会话。
    优先用启动时指定的会话(--session-id / --resume);若终端里用 /clear 开了新会话,则取同目录下满足以下条件的最新会话:
    在终端创建之后才开始、开始时原会话已不再写入问答、且没被其他终端占用。"""
    claimed = {t["launch_sid"] for t in terms if t["launch_sid"]}
    for t in terms:
        if t.get("cli", "claude") != "claude":  # codex:进程开着哪个记录就是哪个会话;agy 还不读记录,用启动时给的临时 ID
            scr = term.screen_state(t["name"], t["cli"])
            t["screen"] = {k: scr[k] for k in ("ready", "busy", "menu")}
            sid = codex_term_sid(t) if t["cli"] == "codex" else None
            t["sid"] = sid if sid and find_session(sid) else None
            continue
        reg = next((pr for pr in live_processes() or () if pr["pid"] == t.get("pid")), None)
        hook = pane_record(t.get("pane"))
        # 登记和 hook 都没变 → 终端没换过会话(没 /clear、/resume)
        mark = (t.get("pid"), reg and reg["sid"], hook and hook.get("session_id"), hook and hook.get("ts"))
        # 1. 屏幕上 cc 输入框边框显示的会话标题 → 同目录下标题相同、最近更新的会话
        scr = term.screen_state(t["name"])
        t["screen"] = {k: scr[k] for k in ("ready", "busy", "menu")}  # 网页的输入框据此显示"回答中 / 菜单选项"
        sid = sid_by_title(t["cwd"], scr["title"])
        if not sid:
            # cc 正在输出、弹着菜单时读不到标题。agent 视图里 fork 后,登记和 hook 仍停在原会话上,
            # 这时退回去会让网页在 fork 和原会话之间来回跳 → 沿用上次读到的标题对应的会话
            memo = _title_memo.get(t["name"])
            if memo and memo[1] == mark and find_session(memo[0]):
                sid = memo[0]
        if sid:
            _title_memo[t["name"]] = (sid, mark)
            t["sid"], t["source"] = sid, "title"
            continue
        _title_memo.pop(t["name"], None)
        if t.get("attach"):  # 接入后台会话的终端:没有自己的登记和 hook,读不到标题时就是接入时的那个会话
            t["sid"] = t["launch_sid"] if t["launch_sid"] and find_session(t["launch_sid"]) else None
            continue
        # 2. cc 自己登记的这个进程的当前会话(窗格里的进程就是 cc);还没写出记录的新 id 先不用,走下面的推测
        rec = reg
        if rec and find_session(rec["sid"]) and cached_meta(find_session(rec["sid"]))["questions"]:
            t["sid"], t["source"] = rec["sid"], "registry"
            continue
        # 3. hook 报告的当前会话(启动、/resume、/clear;后台 worker 接管时可能不触发)
        rec = hook
        if rec and rec["ts"] >= t["created"] - 5:
            t["sid"] = rec["session_id"]
            t["source"] = rec.get("source") or rec.get("event")
            # 换到了新 id 但还没有问答(不是 /clear):多半是 cc 正把旧会话接续到新 id,先留在旧会话上
            cur, prev = find_session(t["sid"]), rec.get("prev") and find_session(rec["prev"])
            if rec.get("source") not in ("clear", "startup") and prev and not (cur and cached_meta(cur)["questions"]) \
                    and cached_meta(prev)["questions"]:
                t["sid"] = prev.stem
            continue
        # 4. 兜底(旧终端没有 hook 时):启动参数 + /clear 推测
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


def pending_title(t):
    if t.get("cli", "claude") == "claude":
        return "新会话(尚未提问)"
    return f"{term.CLIS[t['cli']]['label']} 会话" + (f" · {t['effort']}" if t.get("effort") else "")


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
            fields = [("问", t["q"])] + [("问", x["q"]) for x in t.get("follows", ())]
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

# ---------------------------------------------------------------- 用什么命令行打开项目 / 思考强度
# 全局一份(网页、桌面客户端共用),记在 data/cli.json:{"cli": "claude", "effort": {"claude": "high", ...}}
CLI_PREFS = ROOT / "data" / "cli.json"


def cli_prefs():
    d = _read_json(CLI_PREFS, {})
    cli = d.get("cli") if d.get("cli") in term.CLIS else term.DEFAULT_CLI
    eff = {k: v for k, v in (d.get("effort") or {}).items() if k in term.CLIS and isinstance(v, str) and v}
    return {"cli": cli, "effort": eff}


def cli_state():
    return {**cli_prefs(), "clis": term.cli_info()}


def set_cli_prefs(b):
    """网页上改了"打开方式"或思考强度。effort 是给 b["cli"](没给就是当前的)设的,空 = 用它自己的默认值。"""
    prefs = cli_prefs()
    cli = b.get("cli") or prefs["cli"]
    info = next((c for c in term.cli_info() if c["id"] == cli), None)
    if not info:
        raise ValueError(f"不支持的命令行: {cli}")
    prefs["cli"] = cli
    if "effort" in b:
        e = b.get("effort") or None
        if e and e not in info["efforts"]:
            raise ValueError(f"{info['label']} 没有 {e} 这一档")
        if e:
            prefs["effort"][cli] = e
        else:
            prefs["effort"].pop(cli, None)
    _write_json(CLI_PREFS, prefs)
    return cli_state()


def running_term(sid):
    return next((t["name"] for t in link_terms(term.list_sessions()) if (t["sid"] or t["launch_sid"]) == sid), None)


_sent = OrderedDict()  # 网页发来的消息 id → 结果:网络差时网页会重发同一条,只输入一次
_sent_lock = threading.Lock()


# ---------------------------------------------------------------- 排队 / 引导 / 草稿箱
# cc 正在回答时发的消息有两种去处:
#   排队:存在这里(data/queue.json),等 cc 答完、空闲下来再一条条发;网页上能看到、撤回、改
#   引导:马上插进正在进行的回答(cc 的 Ctrl+X Ctrl+S "send now"),记录里是 #n-1 这样的追问
QUEUE = ROOT / "data" / "queue.json"
DRAFTS = ROOT / "data" / "drafts.json"
_qlock = threading.Lock()


def _read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
    with os.fdopen(fd, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def queue_items(sid):
    with _qlock:
        return _read_json(QUEUE, {}).get(sid, [])


def _queue_edit(sid, fn):
    """在锁里改某个会话的队列;fn(items) 原地修改并返回结果。改完推送给网页。"""
    with _qlock:
        q = _read_json(QUEUE, {})
        items = q.get(sid, [])
        res = fn(items)
        if items:
            q[sid] = items
        else:
            q.pop(sid, None)
        _write_json(QUEUE, q)
    _publish({"queue": {"sid": sid, "items": items}})
    return res


def queue_remove(sid, qid):
    """撤回 / 编辑排队的一条:从队列里拿出来,文字交还给网页。已经发出去了返回 None。"""
    def fn(items):
        for i, it in enumerate(items):
            if it["id"] == qid:
                return items.pop(i)
        return None
    return {"item": _queue_edit(sid, fn), "queue": queue_items(sid)}


def queue_steer(sid, qid):
    """排队的一条不等了:cc 正在回答就马上插进当前回答(引导),已经空闲就直接发。
    和 queue_worker 一样在锁里从队列拿出来,谁先拿到谁发,不会发两遍;没送进去就放回原位。"""
    name = running_term(sid)
    if not name:
        raise ValueError("cc 没在运行")
    pos = {}

    def take(items):
        for i, it in enumerate(items):
            if it["id"] == qid:
                pos["i"] = i
                return items.pop(i)
        return None
    item = _queue_edit(sid, take)
    if not item:  # 刚刚已经按顺序发出去了
        return {"item": None, "queue": queue_items(sid)}
    try:
        st = term.screen_state(name)
        if st["menu"]:
            raise ValueError("cc 正弹着一个选择菜单,先在输入框上方选一项")
        if st["busy"]:
            term.steer(name, item["text"])
            how = "steered"
        elif st["ready"]:
            term.send_text(name, item["text"])
            how = "sent"
        else:
            raise ValueError("cc 还没准备好,稍后再试")
    except Exception:
        _queue_edit(sid, lambda its: its.insert(min(pos["i"], len(its)), item))
        raise
    _q_cool[sid] = time.monotonic() + 5  # 刚插进去一条:别马上把下一条也发了
    return {"item": item, how: True, "queue": queue_items(sid)}


_q_idle = {}  # sid → 连续空闲的次数;发完一条后的冷却截止时间
_q_cool = {}


def queue_worker():
    """排队的消息:cc 空闲(有输入框、没在回答、没弹菜单)连续两次检查都是空闲才发下一条;
    发完冷却几秒,等 cc 开始回答,免得刚发完还没显示"回答中"就把下一条也发了。cc 没在运行就等着,不替你启动。"""
    while True:
        time.sleep(1)
        try:
            with _qlock:
                q = _read_json(QUEUE, {})
            for sid, items in q.items():
                if not items or time.monotonic() < _q_cool.get(sid, 0):
                    continue
                name = running_term(sid)
                st = term.screen_state(name) if name else None
                if not st or not st["ready"] or st["busy"] or st["menu"]:
                    _q_idle[sid] = 0
                    continue
                _q_idle[sid] = _q_idle.get(sid, 0) + 1
                if _q_idle[sid] < 2:
                    continue
                item = _queue_edit(sid, lambda its: its.pop(0) if its else None)
                if item:
                    try:
                        term.send_text(name, item["text"])
                    except Exception:  # noqa: BLE001  没送进去:放回队首
                        _queue_edit(sid, lambda its: its.insert(0, item))
                        raise
                _q_idle[sid] = 0
                _q_cool[sid] = time.monotonic() + 5
        except Exception as e:  # noqa: BLE001
            print("queue:", repr(e), file=sys.stderr, flush=True)


def drafts_list():
    with _qlock:
        return _read_json(DRAFTS, [])


def drafts_op(b):
    """草稿箱:add / update / delete。草稿不分会话(记着从哪个会话放进来的,方便认)。"""
    op = b.get("op")
    with _qlock:
        ds = _read_json(DRAFTS, [])
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        if op == "add":
            text = str(b.get("text") or "")
            if not text.strip():
                raise ValueError("草稿是空的")
            sid = str(b.get("sid") or "") or None
            d = {"id": uuid.uuid4().hex[:12], "text": text, "sid": sid, "title": str(b.get("title") or "")[:120],
                 "created": now, "updated": now}
            ds.insert(0, d)
            res = d
        elif op in ("update", "delete"):
            d = next((x for x in ds if x["id"] == b.get("id")), None)
            if not d:
                raise ValueError("这条草稿已经不在了")
            if op == "delete":
                ds.remove(d)
            else:
                d["text"], d["updated"] = str(b.get("text") or ""), now
            res = d
        else:
            raise ValueError("未知操作")
        _write_json(DRAFTS, ds)
    _publish({"drafts": True})
    return {"draft": res, "drafts": ds}


def send_message(sid, text, mid, mode="send"):
    """网页输入框发来的消息:输进这个会话的 cc(没在运行就先 --resume 启动)。
    mode: send = 直接发;queue = cc 在回答(或已有排队)时排队,否则直接发;steer = cc 在回答时插进当前回答,否则直接发。"""
    if mid:
        with _sent_lock:
            if mid in _sent:
                return _sent[mid]
            _sent[mid] = {"ok": True, "dup": True}
            while len(_sent) > 500:
                _sent.popitem(last=False)
    try:
        name = running_term(sid)
        if not name:
            name = continue_session(sid)
            st = term.wait_ready(name)
        else:
            st = term.screen_state(name)
        if mode == "queue" and not st.get("menu") and (st.get("busy") or queue_items(sid)):
            item = {"id": mid or uuid.uuid4().hex[:12], "text": text,
                    "ts": datetime.now().astimezone().isoformat(timespec="seconds")}
            _queue_edit(sid, lambda its: its.append(item))
            res = {"ok": True, "queued": True, "item": item}
        elif mode == "steer" and st.get("busy") and not st.get("menu"):
            term.steer(name, text)
            res = {"ok": True, "steered": True}
        elif st.get("menu"):
            if term.free_option(st["menu"]) is None:
                raise ValueError("cc 正弹着一个选择菜单,先在输入框上方选一项")
            res = {"ok": True, "term": name, "answered": True, **term.answer_free(name, text)}  # cc 在提问:作为自己填的答案
        elif not st.get("ready"):
            raise ValueError("cc 还没准备好(启动中,或在等别的操作),稍后再发;也可以打开终端看看")
        else:
            term.send_text(name, text)
            res = {"ok": True, "term": name}
    except Exception:
        if mid:
            with _sent_lock:
                _sent.pop(mid, None)  # 没送进去:允许重发
        raise
    if mid:
        with _sent_lock:
            _sent[mid] = res
    return res


def session_keys(sid, body):
    """输入框上的按钮:停止(Esc)、选菜单项。只对已在运行的 cc。"""
    name = running_term(sid)
    if not name:
        raise ValueError("cc 没在运行")
    if "choose" in body:
        term.choose(name, int(body["choose"]))
    elif "tab" in body:
        term.goto_tab(name, int(body["tab"]))
    else:
        term.send_keys(name, [str(k) for k in body.get("keys") or []])
    return {"ok": True}


def continue_session(sid):
    """返回运行这个会话的 tmux 终端;没有就以 --resume 在后台新开一个。"""
    terms = link_terms(term.list_sessions())
    name = next((t["name"] for t in terms if (t["sid"] or t["launch_sid"]) == sid), None)
    if name:
        return name
    p = find_session(sid)
    if p and is_codex(p):  # codex 的历史会话:codex resume <ID>
        owner = session_owner(p, terms)
        if owner:
            raise ValueError(owner_message(owner))
        meta = cached_meta(p)
        cwd = meta["cwd"] if meta["cwd"] and os.path.isdir(meta["cwd"]) else str(Path.home())
        return term.new_session(cwd, ["resume", sid], cli="codex", effort=cli_prefs()["effort"].get("codex"), sid=sid)
    if not p:
        cli = sid.split("-", 1)[0]
        if cli in term.CLIS and cli != "claude":
            raise ValueError(f"这个 {term.CLIS[cli]['label']} 终端已经结束了。网页暂时还不能接着它的历史会话,"
                             f"请在终端里用 {term.CLIS[cli]['short']} 继续,或在侧栏 new 开一个新的")
        raise ValueError("会话不存在")
    owner = session_owner(p, terms)
    meta = cached_meta(p)
    cwd = meta["cwd"] if meta["cwd"] and os.path.isdir(meta["cwd"]) else str(Path.home())
    if owner and owner.get("job"):  # cc 后台会话:接入它,而不是另起一个进程写同一会话
        return term.new_session(cwd, attach=owner["job"], sid=sid)
    if owner:
        raise ValueError(owner_message(owner))
    return term.new_session(cwd, ["--resume", sid], effort=cli_prefs()["effort"].get("claude"))


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
        "id": meta["id"], "title": meta["title"], "cwd": meta["cwd"] or path.parent.name, "cli": meta.get("cli", "claude"),
        "first_q": meta["first_q"], "questions": meta["questions"],
        "started": meta["started"], "updated": meta["updated"], "mtime": mtime,
        "active": time.time() - mtime < ACTIVE_SECONDS,
        "noted": note_path(meta["id"]).exists() or any(note_path(x.stem).exists() for x in predecessors(path)),
        "via": {"platform": "qq", "label": "QQ"} if session_via(path) else None,
    }


# ---------------------------------------------------------------- 整理版发回 QQ

def send_note(sid):
    """把整理版正文作为文字发回 QQ(QQ 私聊发不了 .md 文件)。只能在你最近一次给机器人发消息后 60 分钟内。"""
    p = find_session(sid)
    note = p and find_note(p)
    if not note:
        raise ValueError("这个会话还没有整理版,先执行 /qa-notes")
    qqbot.send_text(f"📒 {note['meta'].get('title') or cached_meta(p)['title']}\n\n{note['body'].strip()}")
    return "QQ"


# ---------------------------------------------------------------- 总结:按主题 + 日期范围让 cc 总结问答

SUMMARY_SOURCE_LIMIT = 400_000  # 交给 cc 的问答原文上限(字符);超出时逐步缩短每轮回答
SUMMARY_PROMPT = """下面是我在 {span} 和 cc(命令行 AI 编程助手)的问答记录,按会话分组,每轮是"问"和"答"。
这些记录已经按我的要求挑过一遍,但仍可能夹带无关内容。请按我的要求写一份总结。我的要求:{topic}

- 只总结和要求相关的会话与问题,无关的忽略;一条相关的都没有就直接说明。
- 用中文 Markdown:开头 3-5 条要点,然后按主题分节(## 标题),每节末尾用"来源:"列出会话标题。
- 公式用 LaTeX(行内 $...$,独立 $$...$$)。
- 忠实于记录,不编造记录里没有的内容;记录里后来被纠正的结论以纠正后的为准。
- 直接输出总结正文,不要寒暄,不要说"以下是总结"。"""
SUMMARY_PROMPT_END = "(问答记录到此结束。)\n\n再说一遍我的要求:{topic}\n请严格按这个要求和上面的格式写总结,与要求无关的会话一律不写。"
SUMMARY_FILTER_PROMPT = """下面是我在 {span} 和 cc(命令行 AI 编程助手)的问答目录:每个会话一行标题,下面每轮是"问"的开头,"→"后是"答"的开头。
我要按这个要求写总结:{topic}

请挑出和要求相关的轮次。追问、"继续"之类承接上文的轮次,跟着它所接的话题走;拿不准的宁可选上。
只输出一个 JSON 对象,键是会话编号,值是相关轮次编号的数组,例如 {{"3": [1, 2, 5], "7": [4]}};没有相关的输出 {{}}。不要输出其他文字。"""
SUMMARY_FILTER_END = "(目录到此结束。)\n\n要求:{topic}\n只输出 JSON。"
SUMMARY_ALL_TOPIC = "全部内容"


def _local_day(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date().isoformat()
    except (ValueError, AttributeError):
        return None


def summary_candidates(day_from, day_to):
    """[day_from, day_to](本地日期 YYYY-MM-DD,含两端)里提过问题的会话,[(meta, 这期间的提问轮次)],按时间排。"""
    picked = []
    since = datetime.fromisoformat(day_from).astimezone().timestamp()  # day_from 当天 0 点(本地时间)
    for p, mtime in session_index().files:
        if mtime < since:  # 文件按 mtime 从新到旧;之后再没写过的会话不可能有这期间的提问
            break
        if successor(p):  # 接续后的新会话带着完整历史,只取最新的那个
            continue
        meta, turns = cached_parse(p)
        sel = [t for t in turns if t["kind"] == "question" and day_from <= (_local_day(t["ts"]) or "") <= day_to]
        if sel:
            picked.append((meta, sel))
    picked.sort(key=lambda x: x[1][0]["ts"])
    return picked


def _clip(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def summary_catalog(cands):
    """筛选用的目录:每轮只有问和答的开头,编号是 会话序号 / 轮次序号(都从 1 起)。"""
    out = []
    for i, (meta, sel) in enumerate(cands, 1):
        out.append(f"[{i}] {meta['title']}(目录:{meta['cwd']})")
        for j, t in enumerate(sel, 1):
            out.append(f"  {j}. {_clip(t['q'], 160)} → {_clip(' '.join(t['texts']), 80)}")
    return "\n".join(out)


def summary_filter(cands, topic, span):
    """让 cc 按要求从目录里挑出相关轮次,返回和 cands 对齐的 [(meta, 选中的轮次)](没选中的会话去掉)。"""
    text = "\n\n".join([SUMMARY_FILTER_PROMPT.format(span=span, topic=topic), summary_catalog(cands),
                        SUMMARY_FILTER_END.format(topic=topic)])
    out = _cc_print(text, timeout=600)
    m = re.search(r"\{.*\}", out, re.S)
    try:
        pick = json.loads(m.group(0)) if m else None
    except ValueError:
        pick = None
    if not isinstance(pick, dict):
        raise RuntimeError("筛选相关会话时 cc 没有返回可用的结果:" + _clip(out, 200))
    res = []
    for i, (meta, sel) in enumerate(cands, 1):
        want = pick.get(str(i))
        if not isinstance(want, list):
            continue
        idx = sorted({n for n in want if isinstance(n, int) and 1 <= n <= len(sel)})
        if idx:
            res.append((meta, [sel[n - 1] for n in idx]))
    return res


def summary_source(picked):
    """把选中的问答拼成按会话分组的 Markdown;超过上限时逐步缩短每轮回答。"""
    for cap in (None, 4000, 1500, 600, 200, 0):
        out = []
        for meta, sel in picked:
            out += [f"# 会话:{meta['title']}", f"目录:{meta['cwd']}", ""]
            for t in sel:
                ans = "\n\n".join(t["texts"])
                if cap is not None and len(ans) > cap:
                    ans = ans[:cap] + "…(后略)"
                out += [f"## 问({_local_day(t['ts'])})", t["q"] or "", "", "### 答", ans, ""]
        text = "\n".join(out)
        if len(text) <= SUMMARY_SOURCE_LIMIT:
            break
    return text[:SUMMARY_SOURCE_LIMIT]


def _cc_print(text, timeout):
    """`cc -p`(不带工具、不保存会话),提示词整段从标准输入给。"""
    r = subprocess.run([term.CC_BIN, "-p", "--no-session-persistence", "--tools", ""], input=text,
                       capture_output=True, text=True, timeout=timeout, cwd=str(SUMMARIES),
                       env=term.clean_env(os.environ))
    if r.returncode or not r.stdout.strip():
        raise RuntimeError((r.stderr or r.stdout).strip()[-500:] or f"退出码 {r.returncode}")
    return r.stdout.strip()


_summary_jobs = set()  # 这个服务器进程里正在生成的总结


def _summary_meta(sid):
    try:
        meta = json.loads((SUMMARIES / f"{sid}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if meta.get("status") == "running" and sid not in _summary_jobs:  # 生成到一半服务器重启了
        meta.update(status="error", error="生成被中断(服务器重启了?),请重新生成")
    return meta


def _summary_save(meta):
    SUMMARIES.mkdir(parents=True, exist_ok=True)
    tmp = SUMMARIES / f"{meta['id']}.json.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SUMMARIES / f"{meta['id']}.json")


def list_summaries():
    out = [m for f in SUMMARIES.glob("*.json") if (m := _summary_meta(f.stem))] if SUMMARIES.is_dir() else []
    return sorted(out, key=lambda m: m["created"], reverse=True)


def read_summary(sid):
    meta = _summary_meta(sid)
    if not meta:
        return None
    f = SUMMARIES / f"{sid}.md"
    return {**meta, "body": f.read_text(encoding="utf-8") if f.exists() else ""}


def start_summary(topic, day_from, day_to, sessions=None):
    """在后台生成总结,结果存到 data/summaries/<id>.md。先让 cc 按要求从问答目录里挑出相关轮次,再只拿这些写总结;
    给了 sessions(会话 id 列表)就跳过挑选,直接用这些会话在这期间的全部提问。"""
    topic = (topic or "").strip()[:500] or SUMMARY_ALL_TOPIC
    for d in (day_from, day_to):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d or ""):
            raise ValueError("日期格式应为 YYYY-MM-DD")
    if day_from > day_to:
        day_from, day_to = day_to, day_from
    cands = summary_candidates(day_from, day_to)
    if not cands:
        raise ValueError("这段时间里没有提问记录")
    manual = sessions is not None
    if manual:
        want = set(map(str, sessions))
        if not want:
            raise ValueError("至少勾选一个会话")
    sid = time.strftime("%Y%m%d-%H%M%S-") + hashlib.blake2b(os.urandom(8), digest_size=3).hexdigest()
    meta = {"id": sid, "topic": topic, "from": day_from, "to": day_to, "status": "running", "error": None,
            "stage": None, "manual": manual, "sessions": 0, "turns": 0, "candidates": [],
            "created": time.time(), "finished": None}
    span = day_from if day_from == day_to else f"{day_from} 至 {day_to}"

    def save(**kw):
        meta.update(kw)
        if sid not in _summary_deleted:
            _summary_save(meta)

    def run():
        body = None
        try:
            if manual:
                picked = [x for x in cands if x[0]["id"] in want]
            elif topic == SUMMARY_ALL_TOPIC:
                picked = cands
            else:
                save(stage="filter")
                picked = summary_filter(cands, topic, span)
            chosen = {m["id"]: len(sel) for m, sel in picked}
            save(stage="write", sessions=len(picked), turns=sum(chosen.values()), candidates=[
                {"id": m["id"], "title": m["title"], "cwd": m["cwd"], "turns": len(sel), "picked": chosen.get(m["id"], 0)}
                for m, sel in cands])
            if picked:
                text = "\n\n".join([SUMMARY_PROMPT.format(span=span, topic=topic), summary_source(picked),
                                    SUMMARY_PROMPT_END.format(topic=topic)])
                body = _cc_print(text, timeout=900) + "\n"
            else:
                body = f"这段时间里没有和「{topic}」相关的问答。\n"
            meta["status"] = "done"
        except Exception as e:  # noqa: BLE001
            meta.update(status="error", error=str(e))
        finally:
            _summary_jobs.discard(sid)
        if sid not in _summary_deleted:  # 生成期间被删掉了就不再写回
            if body:
                (SUMMARIES / f"{sid}.md").write_text(body, encoding="utf-8")
            save(stage=None, finished=time.time())

    _summary_jobs.add(sid)
    _summary_save(meta)
    threading.Thread(target=run, daemon=True).start()
    return meta


_summary_deleted = set()


def delete_summary(sid):
    _summary_deleted.add(sid)
    for ext in (".json", ".md"):
        (SUMMARIES / f"{sid}{ext}").unlink(missing_ok=True)


# ---------------------------------------------------------------- 变化推送(SSE)
# 一个后台线程每秒看一次记录文件、整理稿和终端有没有变化,变了就推给所有打开的网页;
# 网页收到再取数据,不必每 3 秒轮询。推送断了(老浏览器、代理不支持)网页会退回轮询。

class _Sub:
    """一个打开着的网页:待发的事件 + 唤醒发送线程的信号。"""

    def __init__(self):
        self.q, self.ev = [], threading.Event()

    def push(self, data):
        self.q.append(data)
        self.ev.set()


_subs = set()
_subs_lock = threading.Lock()

# 每个打开着的网页当前选中的会话:页面 ID → {"sid", "ts", "subs": 推送连接数, "gone": 断开时刻}。
# cca 不带参数时进入"最近操作过的、仍开着的网页"选中的会话;推送连接断开 60 秒内(网络抖动、重连)仍算开着
_pages = {}
PAGE_GRACE = 60


def set_focus(page, sid):
    with _subs_lock:
        p = _pages.setdefault(page, {"subs": 0, "gone": time.time()})
        p["sid"], p["ts"] = sid, time.time()
        if len(_pages) > 50:  # 早已关掉的页面
            for k in [k for k, v in _pages.items() if not v["subs"] and time.time() - v["gone"] > PAGE_GRACE]:
                _pages.pop(k, None)


def _page_conn(page, delta):
    if not page:
        return
    with _subs_lock:
        p = _pages.setdefault(page, {"sid": None, "ts": 0, "subs": 0})
        p["subs"] = max(0, p["subs"] + delta)
        if not p["subs"]:
            p["gone"] = time.time()


def focused_session():
    """最近操作过的、仍开着的网页上选中的会话 ID;没有网页开着或它没选会话时返回 None。"""
    now = time.time()
    with _subs_lock:
        live = [v for v in _pages.values() if v.get("ts") and (v["subs"] or now - v.get("gone", 0) < PAGE_GRACE)]
    best = max(live, key=lambda v: v["ts"], default=None)
    return best and best["sid"]


def _notes_key():
    try:
        return {f.stem: f.stat().st_mtime_ns for f in NOTES.glob("*.md")}
    except OSError:
        return {}


def _publish(event):
    data = ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()
    with _subs_lock:
        subs = list(_subs)
    for sub in subs:
        sub.push(data)


def watcher():
    files, notes, terms_key, last_terms = {}, _notes_key(), None, 0
    while True:
        time.sleep(1)
        with _subs_lock:
            if not _subs:
                files, terms_key = {}, None  # 没人看就不干活;下次有人连上时全量比一次
                continue
        try:
            idx = session_index()
            now = {p.stem: mt for p, mt in idx.files}
            changed = [sid for sid, mt in now.items() if files.get(sid) != mt] if files else []
            changed += [sid for sid in files if sid not in now]
            nk = _notes_key()
            changed += [sid for sid in set(nk) | set(notes) if nk.get(sid) != notes.get(sid)]
            files, notes = now, nk
            ev = {}
            if changed:
                ev["changed"] = sorted(set(changed))
            if time.monotonic() - last_terms >= 2:  # 终端状态要读屏幕标题(tmux capture-pane),两秒一次
                last_terms = time.monotonic()
                terms = link_terms(term.list_sessions())
                k = json.dumps(terms, sort_keys=True)
                if k != terms_key:
                    terms_key, ev["terms"] = k, terms
            if ev:
                _publish(ev)
        except Exception as e:  # noqa: BLE001  推送失败不能让线程退出
            print("watcher:", repr(e), file=sys.stderr, flush=True)


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
        if t["texts"] or t.get("follows"):
            out += [f"### [{t['n']}] 答" + (f"(用了 {t['tools']} 次工具)" if t["tools"] else ""), ""]
            at = 0
            for k, fl in enumerate(t.get("follows", ()), 1):  # 回答中途的追问插在它出现的位置
                out += ["\n\n".join(t["texts"][at:fl["at"]]), "", f"#### [{t['n']}-{k}] 追问", "", fl["q"], ""]
                at = fl["at"]
            out += ["\n\n".join(t["texts"][at:]), ""]
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
        # 长会话的完整数据有几百 KB,经 SSH / VS Code 端口转发在差网络下要传好几秒;JSON 压缩后通常只剩 1/5
        if len(body) > 4096 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body, 5)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
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

    def _events(self, page=None):
        """SSE:推送 {"changed": [会话 ID…], "terms": [...]};没事时每 15 秒发一次 {"ping": 1}。"""
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        sub = _Sub()
        with _subs_lock:
            _subs.add(sub)
        _page_conn(page, 1)
        try:
            hello = json.dumps({"hello": True, "version": VERSION_INFO["version"]})
            self.wfile.write(f"retry: 3000\ndata: {hello}\n\n".encode())
            self.wfile.flush()
            while True:
                if sub.ev.wait(15):
                    sub.ev.clear()
                    while sub.q:
                        self.wfile.write(sub.q.pop(0))
                else:  # 用数据事件而不是注释保活:网页靠它判断推送连接是不是还活着
                    self.wfile.write(b'data: {"ping": 1}\n\n')
                self.wfile.flush()
        except OSError:
            pass
        finally:
            with _subs_lock:
                _subs.discard(sub)
            _page_conn(page, -1)

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
            self.close_connection = True  # 没读请求体:留在连接里的字节会被当成下一个请求
            return
        u = urlparse(self.path)
        try:
            m = re.fullmatch(r"/api/sessions/([\w-]+)/continue", u.path)
            if m:
                name = continue_session(m.group(1))
                return self._json({"term": name})
            m = re.fullmatch(r"/api/sessions/([\w-]+)/send", u.path)
            if m:
                b = self._body()
                mid = str(b.get("id") or "")
                mode = b.get("mode") if b.get("mode") in ("send", "queue", "steer") else "send"
                return self._json(send_message(m.group(1), str(b.get("text") or ""), mid if re.fullmatch(r"[\w-]{8,64}", mid) else None, mode))
            m = re.fullmatch(r"/api/queue/([\w-]+)/(remove|steer)", u.path)
            if m:
                fn = queue_remove if m.group(2) == "remove" else queue_steer
                return self._json(fn(m.group(1), str(self._body().get("id") or "")))
            if u.path == "/api/drafts":
                return self._json(drafts_op(self._body()))
            m = re.fullmatch(r"/api/sessions/([\w-]+)/keys", u.path)
            if m:
                return self._json(session_keys(m.group(1), self._body()))
            if u.path == "/api/focus":  # 网页切换会话时报告,cca 不带参数时进入它
                b = self._body()
                page, sid = str(b.get("page") or ""), b.get("sid")
                if not re.fullmatch(r"[\w-]{8,64}", page) or (sid is not None and not re.fullmatch(r"[\w-]{1,64}", str(sid))):
                    raise ValueError("bad focus")
                set_focus(page, sid)
                return self._json({"ok": True})
            if u.path == "/api/cli":
                return self._json(set_cli_prefs(self._body()))
            if u.path == "/api/stars":
                b = self._body()
                return self._json({"stars": toggle_star(b["sid"], b["ts"], bool(b.get("on", True)))})
            m = re.fullmatch(r"/api/terms/([\w-]+)/kill", u.path)
            if m:
                term.kill_session(m.group(1))
                return self._json({"ok": True})
            if u.path == "/api/terms/new":  # 侧栏项目名右边的 new:在这个目录新开一个 cc(或选好的 codex / agy)
                b = self._body()
                cwd = b.get("cwd") or ""
                if not os.path.isabs(cwd):
                    raise ValueError("需要绝对路径")
                prefs = cli_prefs()
                cli = b.get("cli") or prefs["cli"]
                name = term.new_session(cwd, cli=cli, effort=prefs["effort"].get(cli))
                t = next((t for t in term.list_sessions() if t["name"] == name), None)
                return self._json({"term": name, "sid": t and t["launch_sid"], "cli": cli})
            m = re.fullmatch(r"/api/sessions/([\w-]+)/send-note", u.path)
            if m:
                return self._json({"ok": True, "to": send_note(m.group(1))})
            if u.path == "/api/update/check":
                return self._json(updater.check())
            if u.path == "/api/update/settings":
                b = self._body()
                updater.save_settings(b.get("channel"), b.get("auto"))
                return self._json(updater.check(fetch=False))
            if u.path == "/api/update/apply":
                return self._json({"ok": True, "target": updater.apply()})
            if u.path == "/api/qq/allow":  # 网页上"允许"第一次给机器人发消息的 QQ 用户
                qqbot.allow(str(self._body().get("openid") or ""))
                return self._json({"ok": True, "qq": qqbot.info()})
            if u.path == "/api/summaries":
                b = self._body()
                return self._json(start_summary(b.get("topic"), b.get("from"), b.get("to"), b.get("sessions")))
            m = re.fullmatch(r"/api/summaries/([\w-]+)/(send|delete)", u.path)
            if m:
                if m.group(2) == "delete":
                    delete_summary(m.group(1))
                    return self._json({"ok": True})
                f = SUMMARIES / f"{m.group(1)}.md"
                if not f.exists():
                    raise ValueError("总结还没生成完")
                qqbot.send_text(f"✦ 总结\n\n{f.read_text(encoding='utf-8').strip()}")
                return self._json({"ok": True, "to": "QQ"})
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
                cid = q.get("cid", [""])[0]
                term.bridge(self, m.group(1), cid if re.fullmatch(r"[\w-]{8,64}", cid) else None)
            return
        if u.path == "/api/events":
            page = q.get("page", [""])[0]
            return self._events(page if re.fullmatch(r"[\w-]{8,64}", page) else None)
        try:
            if u.path == "/api/stars":
                return self._json({"stars": migrate_stars()})
            if u.path == "/api/version":
                return self._json(VERSION_INFO)
            if u.path == "/api/update":
                return self._json(updater.status() or updater.check(fetch=False))
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
            if u.path == "/api/cli":
                return self._json(cli_state())
            if u.path == "/api/focus":  # cca 用:网页上选中的会话
                return self._json({"sid": focused_session()})
            if u.path == "/api/summaries":
                return self._json({"summaries": list_summaries()})
            m = re.fullmatch(r"/api/queue/([\w-]+)", u.path)
            if m:
                return self._json({"queue": queue_items(m.group(1))})
            if u.path == "/api/drafts":
                return self._json({"drafts": drafts_list()})
            m = re.fullmatch(r"/api/summaries/([\w-]+)", u.path)
            if m:
                res = read_summary(m.group(1))
                return self._json(res or {"error": "not found"}, 200 if res else 404)
            if u.path == "/api/sessions":
                terms = link_terms(term.list_sessions())
                running = {t["sid"] or t["launch_sid"]: t["name"] for t in terms}
                items = []
                for t in terms:  # 刚启动、还没提问的会话也列出来
                    sid = t["sid"] or t["launch_sid"]
                    if sid and not (find_session(sid) and cached_meta(find_session(sid))["questions"]):
                        items.append({"id": sid, "title": pending_title(t), "cwd": t["cwd"], "first_q": "", "cli": t["cli"],
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
                return self._json({"sessions": items, "terms": terms, "now": time.time(), "qq": qqbot.info()})
            m = re.fullmatch(r"/api/session/([\w-]+)", u.path)
            if m:
                p = find_session(m.group(1))
                if not p or not cached_meta(p)["questions"]:
                    t = next((t for t in link_terms(term.list_sessions())
                              if (t["sid"] or t["launch_sid"]) == m.group(1)), None)
                    if t:
                        return self._json({"pending": True, "active": True, "version": "pending", "note": None, "turns": [],
                                           "meta": {"id": m.group(1), "title": pending_title(t), "cwd": t["cwd"], "cli": t["cli"],
                                                    "questions": 0, "started": None, "updated": None}})
                if not p:
                    return self._json({"error": "not found"}, 404)
                latest = latest_continuation(p)
                if latest != p:
                    return self._json({"moved": latest.stem})
                note = find_note(p)
                version = f"{p.stat().st_mtime_ns}-{note['mtime'] if note else 0}"
                have = q.get("v", [""])[0]
                owner = session_owner(p)
                if have == version:
                    return self._json({"unchanged": True, "version": version, "owner": owner})
                meta, turns = cached_parse(p)
                meta = {k: v for k, v in meta.items() if k != "_images"}
                res = {"meta": meta, "version": version, "active": time.time() - p.stat().st_mtime < ACTIVE_SECONDS,
                       "owner": owner, "via": session_summary(p)["via"]}
                changed = changed_turns(p, have, version, turns)
                if changed is not None:  # 进行中的会话:只回变化的轮次,前端只重画这些
                    return self._json({**res, "delta": True, "count": len(turns), "turns": changed})
                return self._json({**res, "turns": turns, "note": note})
        except Exception as e:  # noqa: BLE001  本地工具,错误直接回给页面
            return self._json({"error": repr(e)}, 500)
        if u.path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        return self._static_file(u.path, q)

    def _static_file(self, path, q):
        """前端文件。经 VS Code / SSH 转发访问时每个请求都要一个来回、每 KB 都慢,所以:
        - 文本类文件 gzip 压缩(930 KB → 260 KB);
        - index.html 里引用的文件网址带上版本号(?v=文件修改时间),这些网址长期缓存:
          再次打开页面时脚本、样式一个请求都不发,更新后版本号变了才重新下载;
        - index.html 本身每次都向服务器确认(没变时 304)。"""
        rel = path.lstrip("/") or "index.html"
        f = (STATIC / rel).resolve()
        try:
            f.relative_to(STATIC.resolve())
        except ValueError:
            f = STATIC / "index.html"
        if not f.is_file():
            f = STATIC / "index.html"  # 前端路由的网址都回到首页
        body, etag = _static_body(f)
        if f.name == "index.html":
            cache = "no-cache"
        elif q.get("v"):
            cache = "public, max-age=31536000, immutable"
        elif rel.startswith("vendor/"):
            cache = "public, max-age=604800"  # 第三方库和字体(KaTeX 的字体由它的 CSS 引用,网址不带版本号)
        else:
            cache = "no-cache"
        headers = {"Cache-Control": cache, "ETag": etag, "X-Content-Type-Options": "nosniff", "Vary": "Accept-Encoding"}
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        ctype = self.guess_type(str(f))
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml", "application/json"):
            ctype += "; charset=utf-8" if "charset" not in ctype else ""
        gz = f.suffix in GZIP_TYPES and "gzip" in (self.headers.get("Accept-Encoding") or "")
        if gz:
            body = _static_gzip(f, body)
            headers["Content-Encoding"] = "gzip"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


GZIP_TYPES = {".js", ".mjs", ".css", ".html", ".svg", ".json", ".txt", ".map", ".ttf"}
_static_cache = {}  # 路径 → (修改时间, 内容, ETag, gzip 后的内容)
_ASSET_RE = re.compile(r'((?:src|href)=")((?!https?:|//|#|data:)[^"?#]+\.(?:js|css|png|svg|ico|woff2?))(")')


def _asset_version(name):
    try:
        return format((STATIC / name).stat().st_mtime_ns // 1_000_000, "x")
    except OSError:
        return "0"


def _static_body(f):
    """文件内容和 ETag(按修改时间缓存在内存里)。index.html 里的资源网址加上 ?v=版本号。"""
    st = f.stat()
    key = str(f)
    hit = _static_cache.get(key)
    if hit and hit[0] == st.st_mtime_ns:
        if f.name != "index.html":
            return hit[1], hit[2]
    body = f.read_bytes()
    if f.name == "index.html":
        html_text = _ASSET_RE.sub(lambda m: f"{m[1]}{m[2]}?v={_asset_version(m[2])}{m[3]}", body.decode("utf-8"))
        body = html_text.encode("utf-8")
        etag = '"' + hashlib.sha1(body).hexdigest()[:16] + '"'  # 引用的文件变了,首页的版本号也跟着变
    else:
        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
    _static_cache[key] = (st.st_mtime_ns, body, etag, None)
    return body, etag


def _static_gzip(f, body):
    key = str(f)
    hit = _static_cache.get(key)
    if hit and hit[1] is body and hit[3] is not None:
        return hit[3]
    z = gzip.compress(body, 6)
    if hit and hit[1] is body:
        _static_cache[key] = (*hit[:3], z)
    return z


def port_open():
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((HOST if HOST != "0.0.0.0" else "127.0.0.1", PORT)) == 0


def url(sid=None):
    return f"http://localhost:{PORT}/" + (f"#/s/{sid}" if sid else "")


def prewarm(n=8, delay=1.5):
    """网站刚启动时在后台先解析最近的几个会话:打开最常看的长会话时不必现场解析几十 MB 的记录。
    稍等一下再开始,先让网页本身(列表、静态文件)的请求过去。"""
    time.sleep(delay)
    for p in all_session_files()[:n]:
        try:
            cached_parse(p)
        except OSError:
            pass
        time.sleep(0.05)  # 让出 GIL,别挡着同时到来的请求


def cmd_serve(_):
    NOTES.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    threading.Thread(target=watcher, daemon=True).start()
    threading.Thread(target=prewarm, daemon=True).start()
    threading.Thread(target=queue_worker, daemon=True).start()
    VERSION_INFO.update(updater.current())
    updater.on_change(lambda st: _publish({"update": st}))
    threading.Thread(target=updater.loop, daemon=True).start()
    qqbot.start(owner=qq_owner, link=lambda sid: url(sid))  # 有 data/qqbot.json 才会连 QQ
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


def _web_focus():
    """网页上选中的会话 ID(网站没开或网页没选会话时 None)。"""
    if not port_open():
        return None
    try:
        # 不走 http_proxy:设了代理的机器上,访问本机也会被转给代理
        with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{PORT}/api/focus", timeout=3) as r:
            return json.load(r).get("sid")
    except (OSError, ValueError):
        return None


def _session_title(sid):
    p = sid and find_session(sid)
    return cached_meta(p)["title"] if p and cached_meta(p)["questions"] else "新会话(尚未提问)"


def enter_choices(focus):
    """cca 列表:运行中的终端(最近启动的在前);网页选中的会话没在运行时,放一条"继续它"在最前面。"""
    rows = []
    for t in link_terms(term.list_sessions()):
        sid = t["sid"] or t["launch_sid"]
        title = _session_title(sid) if t["cli"] == "claude" or (sid and find_session(sid)) else pending_title(t)
        rows.append({"name": t["name"], "sid": sid, "title": title, "cwd": t["cwd"],
                     "clients": t["clients"], "created": t["created"], "focus": bool(focus) and sid == focus})
    if focus and not any(r["focus"] for r in rows) and find_session(focus):
        meta = cached_meta(find_session(focus))
        rows.insert(0, {"name": None, "sid": focus, "title": meta["title"], "cwd": meta["cwd"] or "",
                        "clients": 0, "created": None, "focus": True})
    return rows


def _dw(s):
    """终端里的显示宽度(中文等全角字符占两格)。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 0 if unicodedata.combining(c) else 1 for c in s)


def _clip(s, width):
    out, w = [], 0
    for c in s:
        cw = _dw(c)
        if w + cw > width:
            return "".join(out[:-1] if out and w + 1 > width else out) + "…" if width > 0 else ""
        out.append(c)
        w += cw
    return "".join(out)


def pick_terminal(focus):
    """全屏列表:↑↓ 选择,回车进入,x 结束会话(←→ 选"取消 / 结束",回车确认),q / Esc 退出。
    返回选中的行,退出时返回 None。"""
    import curses

    def run(scr):
        curses.curs_set(0)
        curses.use_default_colors()
        for i, fg in enumerate((curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED), 1):
            curses.init_pair(i, fg, -1)
        scr.keypad(True)
        rows = enter_choices(focus)
        cur = next((i for i, r in enumerate(rows) if r["focus"]), 0)
        top, confirm, msg = 0, None, ""
        home = str(Path.home())
        while True:
            if not rows:
                return None
            cur = max(0, min(cur, len(rows) - 1))
            H, W = scr.getmaxyx()
            scr.erase()

            def put(y, x, text, attr=0):
                if 0 <= y < H and x < W - 1:
                    try:
                        scr.addstr(y, x, _clip(text, W - 1 - x), attr)
                    except curses.error:
                        pass

            put(0, 1, "cca · 选择要进入的 cc 会话", curses.A_BOLD)
            put(1, 1, "↑↓ 选择   回车 进入   x 结束会话   q 退出", curses.A_DIM)
            body = max(1, H - 5)
            top = min(max(top, cur - body + 1), cur)
            tw = min(max(_dw(r["title"]) for r in rows), max(10, (W - 8) // 2))  # 标题列宽:各行信息对齐
            for k, r in enumerate(rows[top:top + body]):
                i, y = top + k, 3 + k
                sel = i == cur
                attr = curses.A_REVERSE if sel else 0
                put(y, 0, " " * (W - 1), attr)
                put(y, 1, ("▸ " if sel else "  ") + _clip(r["title"], tw), attr | curses.A_BOLD)
                cwd = r["cwd"].replace(home, "~", 1) if r["cwd"].startswith(home) else r["cwd"]
                if r["name"]:
                    state = "已连接" if r["clients"] else "后台"
                    when = datetime.fromtimestamp(r["created"]).strftime("%m-%d %H:%M") if r["created"] else ""
                    info = f"{r['name']} · {cwd} · {state} · {when}"
                else:
                    info = f"{cwd} · 未运行,回车以 --resume 启动"
                if r["focus"]:
                    info += " · 网页选中"
                x = 1 + 2 + tw + 3
                put(y, x, info, attr | (0 if sel else curses.A_DIM))
            if confirm is not None:
                r = rows[cur]
                put(H - 2, 1, f"结束 {r['name']}「{r['title']}」?其中的 cc 会被关闭,记录保留。", curses.color_pair(3) | curses.A_BOLD)
                opts = ["取消", "结束"]
                x = 3
                for j, o in enumerate(opts):
                    label = f" {o} "
                    put(H - 1, x, label, curses.A_REVERSE if j == confirm else 0)
                    x += _dw(label) + 2
                put(H - 1, x + 2, "←→ 选择  回车 确认", curses.A_DIM)
            elif msg:
                put(H - 1, 1, msg, curses.color_pair(2))
            scr.refresh()
            k = scr.getch()
            if confirm is not None:
                if k in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("h"), ord("l"), 9):
                    confirm = 1 - confirm
                elif k in (10, 13, curses.KEY_ENTER):
                    if confirm == 1:
                        r = rows[cur]
                        term.kill_session(r["name"])
                        msg = f"已结束 {r['name']}"
                        rows = enter_choices(focus)
                        if not rows:
                            return "empty"
                    confirm = None
                elif k in (27, ord("q"), ord("n")):
                    confirm = None
                continue
            msg = ""
            if k in (curses.KEY_UP, ord("k")):
                cur -= 1
            elif k in (curses.KEY_DOWN, ord("j")):
                cur += 1
            elif k in (curses.KEY_HOME, curses.KEY_PPAGE):
                cur = 0 if k == curses.KEY_HOME else cur - body
            elif k in (curses.KEY_END, curses.KEY_NPAGE):
                cur = len(rows) - 1 if k == curses.KEY_END else cur + body
            elif k in (10, 13, curses.KEY_ENTER):
                return rows[cur]
            elif k in (ord("x"), ord("X"), curses.KEY_DC):
                if rows[cur]["name"]:
                    confirm = 0  # 默认停在"取消",误按 x 再按回车也不会结束会话
                else:
                    msg = "这一条还没在运行,不需要结束"
            elif k in (27, ord("q")):
                return None
            elif k == curses.KEY_RESIZE:
                pass

    os.environ.setdefault("ESCDELAY", "25")  # 按 Esc 立即退出,不等 1 秒
    try:
        return curses.wrapper(run)
    except KeyboardInterrupt:
        return None


def cmd_enter(args):
    """cca 的实现:给了名字就进那个终端;否则列出运行中的终端让你选(网页上选中的会话排在默认位置),
    没在运行的网页选中会话也列出来,选它就 --resume 启动。不在交互终端里时直接进网页选中的 / 最近的一个。"""
    name = args.name
    if not name:
        focus = _web_focus()
        if sys.stdin.isatty() and sys.stdout.isatty():
            if not enter_choices(focus):
                sys.exit("没有运行中的 cc 会话(网页上也没有选中会话)")
            r = pick_terminal(focus)
            if r == "empty":
                sys.exit("已没有运行中的 cc 会话")
            if not r:
                return
        else:
            rows = enter_choices(focus)
            r = next((x for x in rows if x["focus"]), rows[0] if rows else None)
            if not r:
                sys.exit("没有运行中的 cc 会话(网页上也没有选中会话)")
        name = r["name"]
        if not name:
            try:
                name = continue_session(r["sid"])
            except ValueError as e:
                sys.exit(f"「{r['title']}」: {e}")
        print(f"进入「{r['title']}」({name})")
    if not term.session_exists(name):
        sys.exit(f"没有名为 {name} 的会话,用 ccls 查看")
    sys.exit(term.attach(name))


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


def cmd_link(args):
    sid = args.session or os.environ.get(f"{term._P}_CODE_SESSION_ID")  # cc 给工具子进程设的当前会话 ID
    p = find_session(sid) if sid else current_session()
    if not p:
        sys.exit("找不到会话")
    print(url(latest_continuation(p).stem) + (f"/{args.turn}" if args.turn else ""))


def cmd_pending(args):
    day0 = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() \
        - 86400 * (args.days - 1)
    for p in all_session_files():
        if successor(p) or p.stat().st_mtime < day0:
            continue
        meta = cached_meta(p)
        if not meta["questions"]:
            continue
        note = find_note(p)
        covered = int((note or {}).get("meta", {}).get("covered_turns", "0") or 0)
        total = len(cached_parse(p)[1])
        if covered < total:
            print(f"{p.stem}\t{meta['questions']}问\t已整理到第 {covered}/{total} 轮\t{meta['title']}")


def qq_setup():
    """交互式写 data/qqbot.json。AppSecret 用 getpass 输入,不会出现在屏幕、命令历史或 cc 的会话记录里。"""
    import getpass
    if not sys.stdin.isatty():
        sys.exit("请在你自己的终端里运行(需要输入 AppSecret),不要在 cc 里用 ! 运行")
    old = qqbot.load_config() or {}
    hint = f" [{old['app_id']}]" if old.get("app_id") else ""
    default_dir = old.get("work_dir") or str(Path.home())
    try:
        app_id = input(f"AppID{hint}: ").strip() or old.get("app_id")
        secret = getpass.getpass("AppSecret(输入时不显示,回车沿用原来的): ").strip() or old.get("app_secret")
        work_dir = input(f"QQ 里新会话的工作目录 [{default_dir}]: ").strip() or default_dir
    except (EOFError, KeyboardInterrupt):
        sys.exit("\n已取消,什么都没改")
    if not (app_id and secret):
        sys.exit("AppID / AppSecret 不能为空,什么都没改")
    cfg = {**old, "app_id": app_id, "app_secret": secret, "work_dir": str(Path(work_dir).expanduser()),
           "allow_from": old.get("allow_from", []), "sandbox": old.get("sandbox", False)}
    try:
        bot = qqbot.Bot(cfg)
        bot.token()
        name = bot.api("GET", "/users/@me").get("username")
    except Exception as e:  # noqa: BLE001
        sys.exit(f"验证失败,没有保存:{e}\n(AppID / AppSecret 对吗?服务器的公网 IP 加进 q.qq.com 的 IP 白名单了吗?)")
    qqbot.CONFIG.parent.mkdir(parents=True, exist_ok=True)
    qqbot._write_private(qqbot.CONFIG, cfg)
    print(f"已保存 {qqbot.CONFIG}(权限 600),机器人:{name}")
    print("下一步:重启网站(结束旧的 ccnb.py serve,再 ccweb),然后在 QQ 里给机器人发一条消息,到网页侧栏点「允许」。")


def cmd_qq(args):
    if args.action == "setup":
        return qq_setup()
    if args.action == "probe":
        cfg = qqbot.load_config()
        if not cfg:
            sys.exit(f"没有 {qqbot.CONFIG}(app_id / app_secret)")
        qqbot.Bot(cfg, verbose=True).run()


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
    lk = sub.add_parser("link")
    lk.add_argument("--session")
    lk.add_argument("--turn", type=int)
    lk.set_defaults(fn=cmd_link)
    pe = sub.add_parser("pending")
    pe.add_argument("--days", type=int, default=1)
    pe.set_defaults(fn=cmd_pending)
    qa = sub.add_parser("qq")
    qa.add_argument("action", choices=["setup", "probe"])
    qa.set_defaults(fn=cmd_qq)
    la = sub.add_parser("launch")
    la.add_argument("--detach", action="store_true", help="只在后台启动,不在本终端 attach")
    la.add_argument("cc_args", nargs=argparse.REMAINDER)
    la.set_defaults(fn=cmd_launch)
    at = sub.add_parser("attach")
    at.add_argument("name")
    at.set_defaults(fn=lambda a: sys.exit(term.attach(a.name)))
    en = sub.add_parser("enter", help="cca:选择要进入的会话(或直接进指定名字的终端)")
    en.add_argument("name", nargs="?")
    en.set_defaults(fn=cmd_enter)
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
