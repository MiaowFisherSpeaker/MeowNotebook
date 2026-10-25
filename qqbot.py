"""QQ 机器人(QQ 开放平台官方机器人,只做私聊):在 QQ 里和 cc 对话。可选,data/qqbot.json 存在才启用。

收到私聊消息 → 在后台用 `cc -p --resume <会话>` 运行 → 把回答发回 QQ。会话就是普通的 cc 会话,
网站上照常显示(侧栏标 "QQ"),也能在网页上继续。只用标准库:WebSocket 客户端、HTTPS 接口都在这里。

配置 data/qqbot.json(权限 600,不进仓库):
  app_id / app_secret   QQ 开放平台的 AppID / AppSecret
  allow_from            允许使用的用户 openid 列表;空 = 谁都不响应(第一条消息的 openid 会记下来,网页上一键允许)
  work_dir              新会话的工作目录
  sandbox               true = 沙箱环境
  permission_mode       cc 的权限模式,默认 bypassPermissions(和 cc 命令一致)

QQ 的限制:机器人只能回复用户发来的消息(被动回复),每条消息 60 分钟内最多回 5 次;不能主动发起私聊。
"""
import base64
import json
import os
import re
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse

import term

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "data" / "qqbot.json"
STATE = ROOT / "data" / "qqbot-state.json"  # openid → {sid, cwd, last: {id, ts, seq}}
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
INTENTS = 1 << 25  # GROUP_AND_C2C_EVENT:私聊消息(C2C_MESSAGE_CREATE)、加好友等
REPLY_WINDOW = 55 * 60  # 被动回复有效期 60 分钟,留点余量
REPLY_MAX = 5  # 每条用户消息最多回复次数
CHUNK = 1500  # 每条回复的字数上限(QQ 对单条长度有限制,太长也难读)
QQ_PROMPT = ("这次对话的回复会发到 QQ 私聊:QQ 不渲染 Markdown 和 LaTeX。回答简洁;不要用表格;"
             "公式用 Unicode 书写(如 σ²、xᵢ、∑、√),不要写 $...$。")


def log(*a):
    print(time.strftime("%H:%M:%S"), "qqbot:", *a, file=sys.stderr, flush=True)


def load_config():
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return cfg if cfg.get("app_id") and cfg.get("app_secret") else None


def _write_private(path, obj):
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


# ---------------------------------------------------------------- WebSocket 客户端(只需要读文本帧、发带掩码的文本帧)

def ws_connect(url, timeout=30):
    u = urlparse(url)
    raw = socket.create_connection((u.hostname, u.port or 443), timeout=timeout)
    sock = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall((f"GET {u.path or '/'}{'?' + u.query if u.query else ''} HTTP/1.1\r\nHost: {u.hostname}\r\n"
                  f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                  "Sec-WebSocket-Version: 13\r\n\r\n").encode())
    f = sock.makefile("rb")
    status = f.readline().decode(errors="replace")
    if " 101 " not in status:
        raise ConnectionError("WebSocket 握手失败: " + status.strip())
    while f.readline() not in (b"\r\n", b"\n", b""):
        pass
    return sock, f


def ws_send(sock, text, lock, op=1):
    p = text.encode() if isinstance(text, str) else text
    n, mask = len(p), os.urandom(4)
    head = bytes([0x80 | op]) + (bytes([0x80 | n]) if n < 126 else
                                 bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else
                                 bytes([0x80 | 127]) + struct.pack(">Q", n))
    k = int.from_bytes((mask * (n // 4 + 1))[:n], "big")
    body = (int.from_bytes(p, "big") ^ k).to_bytes(n, "big") if n else b""
    with lock:
        sock.sendall(head + mask + body)


def ws_recv(f):
    """返回 (opcode, payload);分片拼好再返回。"""
    data, first = b"", None
    while True:
        h = f.read(2)
        if len(h) < 2:
            raise ConnectionError("连接已关闭")
        fin, op, n = h[0] & 0x80, h[0] & 0x0F, h[1] & 0x7F
        if n == 126:
            n = struct.unpack(">H", f.read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", f.read(8))[0]
        mask = f.read(4) if h[1] & 0x80 else None
        p = f.read(n)
        if mask:
            p = bytes(b ^ mask[i % 4] for i, b in enumerate(p))
        if op >= 8:
            return op, p
        first = first if op == 0 else op
        data += p
        if fin:
            return first, data


# ---------------------------------------------------------------- QQ 开放平台

class Bot:
    def __init__(self, cfg, on_message=None, verbose=False):
        self.cfg, self.on_message, self.verbose = cfg, on_message, verbose
        self.api_base = "https://sandbox.api.sgroup.qq.com" if cfg.get("sandbox") else "https://api.sgroup.qq.com"
        self._tok, self._tok_exp = None, 0
        self.session_id, self.seq = None, None
        self.connected, self.error, self.events = False, None, 0
        self._lock = threading.Lock()

    def token(self):
        if self._tok and time.time() < self._tok_exp - 60:
            return self._tok
        body = json.dumps({"appId": self.cfg["app_id"], "clientSecret": self.cfg["app_secret"]}).encode()
        r = json.load(urllib.request.urlopen(urllib.request.Request(
            TOKEN_URL, body, {"Content-Type": "application/json"}), timeout=15))
        if "access_token" not in r:
            raise RuntimeError(f"获取 access_token 失败: {r}")
        self._tok, self._tok_exp = r["access_token"], time.time() + int(r.get("expires_in", 7200))
        return self._tok

    def api(self, method, path, body=None):
        req = urllib.request.Request(self.api_base + path, json.dumps(body).encode() if body is not None else None,
                                     {"Authorization": "QQBot " + self.token(), "Content-Type": "application/json"},
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{method} {path} → {e.code}: {e.read().decode(errors='replace')[:300]}") from None
        return json.loads(raw) if raw else {}

    def reply(self, openid, msg_id, seq, text):
        """被动回复;seq 是这条用户消息的第几次回复(1 起)。"""
        return self.api("POST", f"/v2/users/{openid}/messages",
                        {"content": text, "msg_type": 0, "msg_id": msg_id, "msg_seq": seq})

    def run(self):
        """连接网关,断了就重连,永不返回。"""
        backoff = 1
        while True:
            try:
                self._session()
                backoff = 1
            except Exception as e:  # noqa: BLE001
                self.error = str(e)
                log("连接断开:", e)
            self.connected = False
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    def _session(self):
        url = self.api("GET", "/gateway")["url"]
        sock, f = ws_connect(url)
        alive = threading.Event()
        try:
            op, p = ws_recv(f)
            hello = json.loads(p)
            if hello.get("op") != 10:
                raise ConnectionError(f"期望 Hello,收到 {hello}")
            interval = hello["d"]["heartbeat_interval"] / 1000
            if self.session_id:  # 断线续上,期间的事件会补发
                ws_send(sock, json.dumps({"op": 6, "d": {"token": "QQBot " + self.token(),
                                                         "session_id": self.session_id, "seq": self.seq}}), self._lock)
            else:
                ws_send(sock, json.dumps({"op": 2, "d": {"token": "QQBot " + self.token(), "intents": INTENTS,
                                                         "shard": [0, 1], "properties": {}}}), self._lock)
            acked = [time.monotonic()]

            def beat():
                while not alive.wait(interval):
                    if time.monotonic() - acked[0] > interval * 2.5:  # 收不到心跳回应:连接多半已死
                        log("心跳无回应,重连")
                        try:
                            sock.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        return
                    try:
                        ws_send(sock, json.dumps({"op": 1, "d": self.seq}), self._lock)
                    except OSError:
                        return

            threading.Thread(target=beat, daemon=True).start()
            sock.settimeout(None)
            while True:
                op, p = ws_recv(f)
                if op == 8:
                    code = struct.unpack(">H", p[:2])[0] if len(p) >= 2 else None
                    raise ConnectionError(f"服务器关闭连接 code={code} {p[2:].decode(errors='replace')}")
                if op == 9:
                    ws_send(sock, p, self._lock, op=10)
                    continue
                if op != 1:
                    continue
                msg = json.loads(p)
                if msg.get("s"):
                    self.seq = msg["s"]
                o = msg.get("op")
                if o == 11:
                    acked[0] = time.monotonic()
                elif o == 7:
                    raise ConnectionError("服务器要求重连")
                elif o == 9:
                    self.session_id, self.seq = None, None
                    raise ConnectionError("会话失效,重新登录")
                elif o == 0:
                    self._dispatch(msg.get("t"), msg.get("d") or {})
                elif self.verbose:
                    log("op", o, msg)
        finally:
            alive.set()
            try:
                sock.close()
            except OSError:
                pass

    def _dispatch(self, t, d):
        self.events += 1
        if t in ("READY", "RESUMED"):
            if t == "READY":
                self.session_id = d.get("session_id")
                log("已连接,机器人:", (d.get("user") or {}).get("username"))
            self.connected, self.error = True, None
            return
        if self.verbose:
            log("事件", t, json.dumps(d, ensure_ascii=False)[:500])
        if t == "C2C_MESSAGE_CREATE" and self.on_message:
            threading.Thread(target=self.on_message, args=(d,), daemon=True).start()


# ---------------------------------------------------------------- 把私聊消息交给 cc

_state_lock = threading.Lock()
RUNNING = {}  # pid → openid:正在处理 QQ 消息的 cc 进程(网页据此知道会话被 QQ 占用)
_bot = None
_queues = {}  # openid → threading.Lock:同一个人的消息一条条处理
_hooks = {"owner": lambda sid: None, "link": lambda sid: ""}


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def update_state(fn):
    with _state_lock:
        st = load_state()
        fn(st)
        _write_private(STATE, st)
        return st


def origins():
    """经 QQ 使用过的 cc 会话 ID 集合。"""
    st = load_state()
    return {sid for u in st.values() if isinstance(u, dict) for sid in [u.get("sid"), *u.get("past", [])] if sid}


def _reply_all(openid, msg_id, text, start_seq=1):
    """把一段文字按条数限制发出去;返回用掉的序号。超出部分换成网页链接提示。"""
    parts = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] or ["(空回复)"]
    room = REPLY_MAX - start_seq + 1
    if len(parts) > room:
        parts = parts[:max(room, 1)]
        parts[-1] = parts[-1][:CHUNK - 60] + "\n…(太长,后面省略;完整内容看 MeowNotebook)"
    seq = start_seq
    for part in parts:
        _bot.reply(openid, msg_id, seq, part)
        seq += 1
    return seq


def _note_last(openid, msg_id, seq):
    def f(st):
        st.setdefault(openid, {})["last"] = {"id": msg_id, "ts": time.time(), "seq": seq}
    update_state(f)


HELP = ("在 QQ 里直接和 cc 对话(回复可能要几十秒)。命令:\n"
        "/new [目录]  开一个新会话(默认目录见配置)\n/where  当前会话和网页链接\n/stop  打断正在运行的回答\n/help  本说明")


def handle_message(d):
    openid = (d.get("author") or {}).get("user_openid")
    text, msg_id = (d.get("content") or "").strip(), d.get("id")
    if not openid or not msg_id:
        return
    cfg = load_config() or {}
    if openid not in (cfg.get("allow_from") or []):
        log("未授权的用户,不响应:", openid)
        update_state(lambda st: st.__setitem__("_pending", {"openid": openid, "ts": time.time(), "text": text[:80]}))
        return
    _note_last(openid, msg_id, 1)
    user = load_state().get(openid, {})
    if text in ("/help", "帮助"):
        return _reply_all(openid, msg_id, HELP)
    if text == "/where":
        sid = user.get("sid")
        return _reply_all(openid, msg_id, f"会话 {sid}\n目录 {user.get('cwd') or cfg.get('work_dir')}\n{_hooks['link'](sid)}"
                          if sid else "还没有会话,直接发消息就会开一个。")
    if text == "/stop":
        pids = [p for p, o in RUNNING.items() if o == openid]
        for p in pids:
            try:
                os.killpg(p, signal.SIGINT)
            except OSError:
                pass
        return _reply_all(openid, msg_id, "已打断。" if pids else "现在没有在运行的回答。")
    if text.startswith("/new"):
        cwd = text[4:].strip() or cfg.get("work_dir") or str(Path.home())
        cwd = str(Path(cwd).expanduser())
        if not os.path.isdir(cwd):
            return _reply_all(openid, msg_id, f"目录不存在:{cwd}")

        def f(st):
            u = st.setdefault(openid, {})
            if u.get("sid"):
                u.setdefault("past", []).append(u["sid"])
            u.update(sid=None, cwd=cwd)
        update_state(f)
        return _reply_all(openid, msg_id, f"好的,下一条消息开始新会话(目录 {cwd})。")
    lock = _queues.setdefault(openid, threading.Lock())
    with lock:  # 前一条还没回完就排队
        _run_cc(openid, msg_id, text, cfg)


def _run_cc(openid, msg_id, text, cfg):
    user = load_state().get(openid, {})
    cwd = user.get("cwd") or cfg.get("work_dir") or str(Path.home())
    os.makedirs(cwd, exist_ok=True)
    sid = user.get("sid")
    if sid:
        owner = _hooks["owner"](sid)
        if owner:
            return _reply_all(openid, msg_id, f"这个会话正在{owner}里打开着,为避免两边同时写同一会话,这条没有执行。"
                                                f"先在那边退出,或发 /new 开新会话。")
        args = ["--resume", sid]
    else:
        sid = str(uuid.uuid4())
        args = ["--session-id", sid]
    cmd = [term.CC_BIN, "-p", "--output-format", "json", "--permission-mode", cfg.get("permission_mode") or "bypassPermissions",
           "--append-system-prompt", QQ_PROMPT, *args, text]
    env = {**term.clean_env(os.environ), "CCNB_QQ": "1"}
    t0 = time.time()
    try:
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=True)
    except OSError as e:
        return _reply_all(openid, msg_id, f"启动 cc 失败:{e}")
    RUNNING[p.pid] = openid
    try:
        out, err = p.communicate()
    finally:
        RUNNING.pop(p.pid, None)
    try:
        res = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        res = {"is_error": True, "result": (err or out or f"退出码 {p.returncode}").strip()[-800:]}
    new_sid = res.get("session_id") or sid
    update_state(lambda st: st.setdefault(openid, {}).update(sid=new_sid, cwd=cwd))
    answer = (res.get("result") or "").strip()
    if p.returncode and p.returncode < 0 or p.returncode == 130:
        answer = answer or "(已打断)"
    elif res.get("is_error"):
        answer = "出错了:" + (answer or res.get("subtype") or "未知错误")
    if time.time() - t0 > REPLY_WINDOW:
        log("回答用时超过 QQ 回复窗口,无法发回:", new_sid)
        return
    seq = _reply_all(openid, msg_id, answer)
    _note_last(openid, msg_id, seq)


def can_send(openid=None):
    """网页上"发送到 QQ"能不能用:QQ 只允许在用户最近一条消息后 60 分钟内回复,最多 5 次。返回 (openid, last) 或 None。"""
    st = load_state()
    cands = [(o, u["last"]) for o, u in st.items() if isinstance(u, dict) and u.get("last") and (openid in (None, o))]
    cands = [(o, l) for o, l in cands if time.time() - l["ts"] < REPLY_WINDOW and l["seq"] <= REPLY_MAX]
    return max(cands, key=lambda x: x[1]["ts"]) if cands else None


def send_text(text):
    """借用户最近一条消息的回复机会,把文字发回 QQ。"""
    if not _bot or not _bot.connected:
        raise RuntimeError("QQ 机器人没有连接")
    c = can_send()
    if not c:
        raise RuntimeError("QQ 只允许在你发消息后 60 分钟内回复:先在 QQ 里给机器人发一条消息,再点发送")
    openid, last = c
    seq = _reply_all(openid, last["id"], text, last["seq"])
    _note_last(openid, last["id"], seq)


def info():
    cfg = load_config()
    if not cfg:
        return {"enabled": False}
    pend = load_state().get("_pending")
    return {"enabled": True, "connected": bool(_bot and _bot.connected), "error": _bot and _bot.error,
            "can_send": bool(can_send()), "allowed": len(cfg.get("allow_from") or []),
            "pending": pend if pend and pend["openid"] not in (cfg.get("allow_from") or []) else None}


def allow(openid):
    if not re.fullmatch(r"[0-9A-Fa-f]{16,64}", openid or ""):
        raise ValueError("openid 格式不对")
    cfg = load_config()
    if not cfg:
        raise RuntimeError("没有 data/qqbot.json")
    if openid not in cfg.setdefault("allow_from", []):
        cfg["allow_from"].append(openid)
        _write_private(CONFIG, cfg)
    update_state(lambda st: st.pop("_pending", None))


def start(owner=None, link=None):
    """服务器启动时调用:有配置就在后台连上 QQ。owner(sid) → 占用这个会话的地方(网页终端等)或 None。"""
    global _bot
    cfg = load_config()
    if not cfg or _bot:
        return
    _hooks.update({k: v for k, v in (("owner", owner), ("link", link)) if v})
    _bot = Bot(cfg, handle_message)
    threading.Thread(target=_bot.run, daemon=True).start()

