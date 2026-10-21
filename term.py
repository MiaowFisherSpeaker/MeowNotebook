"""网页终端:tmux 会话管理 + WebSocket ↔ PTY 桥接(仅标准库)。

所有由 `cc` 启动的会话都跑在专用 tmux socket(-L ccnb)里,
本地终端和浏览器 attach 的是同一个会话,画面与输入完全同步。
"""
import base64
import fcntl
import hashlib
import json
import os
import pty
import re
import select
import shlex
import signal
import struct
import subprocess
import termios
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOCKET = "ccnb"
TMUX_CONF = ROOT / "tmux.conf"
# cc 命令行的可执行文件名与数据目录,可用环境变量覆盖。全项目只在这里写出默认值。
CC_BIN = os.environ.get("CC_BIN", "claude")
CC_HOME = Path(os.environ.get("CC_HOME") or os.environ.get(f"{CC_BIN.upper()}_CONFIG_DIR")
               or Path.home() / f".{CC_BIN}").expanduser()
CC_CMD = [CC_BIN, "--dangerously-skip-permissions"]
# 只挂在 cc 启动的进程上的 hook:记录"窗格 → 当前会话",见 hook.py
_HOOK = {"type": "command", "command": f"python3 -I {ROOT / 'hook.py'}", "timeout": 5}
HOOK_SETTINGS = json.dumps({"hooks": {"SessionStart": [{"hooks": [_HOOK]}],
                                      "UserPromptSubmit": [{"hooks": [_HOOK]}]}})
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
# 不传给 tmux 会话的环境变量
ENV_SKIP = {"TMUX", "TMUX_PANE", "TERM", "SHLVL", "PWD", "OLDPWD", "_", "COLUMNS", "LINES", "TERM_PROGRAM", "TERM_PROGRAM_VERSION"}
# 某个 cc 会话自身的运行时变量(会话 ID、消息 socket 等):如果 cc 是在另一个 cc 会话里被调用的,不能继承
_P = CC_BIN.upper()
CC_RUNTIME_ENV = [f"{_P}CODE", f"{_P}_PID", f"{_P}_JOB_DIR", f"{_P}_EFFORT",
                  *(f"{_P}_CODE_{x}" for x in ("CHILD_SESSION", "SESSION_ID", "SESSION_ATTENDED", "MESSAGING_SOCKET",
                                                "MESSAGING_TOKEN", "ENTRYPOINT", "EXECPATH"))]


def clean_env(env):
    return {k: v for k, v in env.items() if k not in CC_RUNTIME_ENV}


def tmux(*args, check=False):
    return subprocess.run(["tmux", "-L", SOCKET, "-f", str(TMUX_CONF), *args],
                          capture_output=True, text=True, check=check)


def list_sessions():
    r = tmux("list-sessions", "-F", "#{session_name}\t#{session_path}\t#{session_created}\t#{session_attached}\t#{@ccnb_sid}\t#{pane_id}")
    out = []
    for line in r.stdout.splitlines():
        name, path, created, attached, sid, pane = (line.split("\t") + [""] * 6)[:6]
        out.append({"name": name, "cwd": path, "created": int(created or 0),
                    "clients": int(attached or 0), "launch_sid": sid or None, "pane": pane.lstrip("%")})
    return sorted(out, key=lambda s: -s["created"])


_BORDER_TITLE = re.compile(r"^[─━]{2,}\s+(.+?)\s+[─━]+\s*$")


def screen_title(name):
    """读取 cc 输入框上边框里显示的会话标题(如 "──── 某某会话 ─")。没有则返回 None。
    cc 的会话可能由后台 worker 运行(/resume 时),hook 不一定触发,但界面上的标题总是对的。"""
    lines = tmux("capture-pane", "-p", "-t", f"={name}:").stdout.splitlines()
    for i in range(len(lines) - 1, 0, -1):
        if lines[i].lstrip().startswith("❯"):
            m = _BORDER_TITLE.match(lines[i - 1].strip())
            return m.group(1).strip() if m else None
    return None


def session_exists(name):
    return tmux("has-session", "-t", f"={name}").returncode == 0


def plan_session_id(args, latest_sid=None):
    """确定 cc 将使用的会话 ID,以便网页把终端和问答记录对应起来。
    返回 (最终参数, 会话 ID 或 None)。latest_sid: 该目录最近的会话(用于 -c)。"""
    args = list(args)
    if "--fork-session" in args:
        return args, None
    for i, a in enumerate(args):
        nxt = args[i + 1] if i + 1 < len(args) else None
        if a == "--session-id" and nxt:
            return args, nxt
        if a.startswith("--session-id="):
            return args, a.split("=", 1)[1]
        if a in ("-r", "--resume"):
            return args, (nxt if nxt and not nxt.startswith("-") else None)
        if a.startswith("--resume="):
            return args, a.split("=", 1)[1]
        if a in ("-c", "--continue"):
            return args, latest_sid
        if a in ("-p", "--print", "-h", "--help", "-v", "--version"):
            return args, None
    sid = str(uuid.uuid4())
    return ["--session-id", sid, *args], sid


def new_session(cwd, cc_args=(), cols=200, rows=50, latest_sid=None):
    cwd = str(Path(cwd).expanduser().resolve())
    if not os.path.isdir(cwd):
        raise ValueError(f"目录不存在: {cwd}")
    base = re.sub(r"[^\w-]", "_", Path(cwd).name or "root")[:24]
    name = stem = f"{base}-{time.strftime('%H%M%S')}"
    i = 2
    while session_exists(name):  # 同一秒在同一目录启动多次
        name, i = f"{stem}-{i}", i + 1
    envs = []
    for k, v in os.environ.items():
        if k not in ENV_SKIP and k not in CC_RUNTIME_ENV and "\n" not in v:
            envs += ["-e", f"{k}={v}"]
    cc_args, sid = plan_session_id(cc_args, latest_sid)
    # tmux 服务器的全局环境可能带着这些变量,用 env -u 在 pane 里显式清掉
    unset = [x for k in CC_RUNTIME_ENV for x in ("-u", k)]
    hook = ["--settings", HOOK_SETTINGS] if CC_CMD[0] == CC_BIN else []
    cmd = shlex.join(["env", *unset, *CC_CMD, *hook, *cc_args])
    r = tmux("new-session", "-d", "-s", name, "-c", cwd, "-x", str(cols), "-y", str(rows), *envs, cmd)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or "tmux new-session 失败")
    if sid:
        tmux("set-option", "-t", f"={name}:", "@ccnb_sid", sid)
    return name


def kill_session(name):
    tmux("kill-session", "-t", f"={name}")


# ---------------------------------------------------------------- WebSocket

def ws_accept(handler):
    key = handler.headers.get("Sec-WebSocket-Key")
    if not key or handler.headers.get("Upgrade", "").lower() != "websocket":
        handler.send_error(400, "expected websocket")
        return False
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    handler.send_response(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept)
    handler.end_headers()
    handler.wfile.flush()
    return True


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError
        buf += chunk
    return buf


WS_MAX_MESSAGE = 1 << 20  # 单条消息(含分片拼接后)上限 1 MiB:键盘输入 / 粘贴 / resize 远用不到


class MessageTooBig(ValueError):
    pass


def _unmask(data, mask):
    if not data:
        return data
    k = int.from_bytes((mask * (len(data) // 4 + 1))[:len(data)], "big")
    return (int.from_bytes(data, "big") ^ k).to_bytes(len(data), "big")


def ws_recv(sock):
    """读一条完整消息,返回 (opcode, payload)。超过大小上限时在读 payload 之前抛 MessageTooBig。"""
    data, first_op = b"", None
    while True:
        b1, b2 = _recv_exact(sock, 2)
        fin, op = b1 & 0x80, b1 & 0x0F
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", _recv_exact(sock, 2))[0]
        elif n == 127:
            n = struct.unpack(">Q", _recv_exact(sock, 8))[0]
        if op >= 8 and (n > 125 or not fin):  # 协议规定控制帧不分片、payload ≤ 125
            raise MessageTooBig
        if op < 8 and len(data) + n > WS_MAX_MESSAGE:
            raise MessageTooBig
        mask = _recv_exact(sock, 4) if b2 & 0x80 else b"\0\0\0\0"
        payload = _unmask(_recv_exact(sock, n), mask)
        if op >= 8:  # 控制帧可以插在分片中间
            return op, payload
        first_op = first_op if op == 0 else op
        data += payload
        if fin:
            return first_op, data


def ws_send(sock, payload, op=2):
    n = len(payload)
    head = bytes([0x80 | op])
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    sock.sendall(head + payload)


def set_winsize(fd, rows, cols):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def bridge(handler, name):
    """把一个 WebSocket 连接接到 `tmux attach -t name` 的 PTY 上。
    协议:客户端发文本帧,首字符 '0' = 键盘输入,'1' = {"cols","rows"} 调整大小。"""
    handler.close_connection = True  # WebSocket 结束后不再复用这条 HTTP 连接
    if not ws_accept(handler):
        return
    sock = handler.connection
    if not session_exists(name):
        ws_send(sock, "\r\n[会话不存在或已结束]\r\n".encode(), 2)
        ws_send(sock, b"", 8)
        return
    pid, fd = pty.fork()
    if pid == 0:  # 子进程
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        env.update(TERM="xterm-256color", COLORTERM="truecolor", LANG=env.get("LANG", "C.UTF-8"))
        os.execvpe("tmux", ["tmux", "-L", SOCKET, "-f", str(TMUX_CONF), "attach", "-t", f"={name}"], env)
    set_winsize(fd, 40, 120)
    close_code = b""
    try:
        while True:
            r, _, _ = select.select([sock, fd], [], [], 30)
            if fd in r:
                try:
                    out = os.read(fd, 65536)
                except OSError:
                    out = b""
                if not out:  # tmux 退出(会话结束)
                    ws_send(sock, "\r\n[会话已结束]\r\n".encode(), 2)
                    break
                ws_send(sock, out, 2)
            if sock in r:
                op, payload = ws_recv(sock)
                if op == 8:
                    break
                if op == 9:
                    ws_send(sock, payload, 10)
                    continue
                if not payload:
                    continue
                kind, body = payload[:1], payload[1:]
                if kind == b"0":
                    os.write(fd, body)
                elif kind == b"1":
                    size = json.loads(body)
                    set_winsize(fd, max(5, int(size["rows"])), max(20, int(size["cols"])))
            if not r:
                ws_send(sock, b"", 9)  # 保活 ping
    except MessageTooBig:
        close_code = struct.pack(">H", 1009)  # 1009 = message too big
    except (ConnectionError, OSError, ValueError):
        pass
    finally:
        try:
            os.kill(pid, signal.SIGTERM)  # 只断开这个 client,tmux 会话和 cc 继续运行
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
        os.close(fd)
        try:
            ws_send(sock, close_code, 8)
        except OSError:
            pass
