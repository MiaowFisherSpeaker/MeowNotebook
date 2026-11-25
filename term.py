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
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
import uuid
from collections import OrderedDict
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


# ---------------------------------------------------------------- 支持的命令行(cc 之外还有 codex / agy)
# 网页上"用什么打开项目"可以选;cc(Claude Code)仍是默认,记录解析、hook、总结等目前只对它。
# effort:思考强度,网页上的选项跟着所选的命令行变;None = 用它自己的默认值。
#   claude / agy: --effort <级别>;codex: -c model_reasoning_effort="<级别>"(可选级别跟着它当前的模型)
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
AGY_BIN = os.environ.get("AGY_BIN", "agy")
CLIS = {
    # marker:菜单里选中项前面的符号;prompt:输入框开头的符号;steer:回答中插话的按键(None = 不支持,只能排队);
    # busy:回答中屏幕底部会出现的提示文字
    "claude": {"label": "Claude Code", "short": "cc", "bin": CC_BIN, "cmd": CC_CMD, "marker": "❯", "prompt": "❯",
               "steer": ["C-x", "C-s"], "busy": ["esc to interrupt"]},
    "codex": {"label": "Codex", "short": "codex", "bin": CODEX_BIN,
              "cmd": [CODEX_BIN, "--dangerously-bypass-approvals-and-sandbox"], "marker": "›", "prompt": "›",
              "steer": ["Enter"], "busy": ["esc to interrupt"],  # codex 回答中按 Enter 就是插进当前回答(Tab 是排队)
              # 不用 codex 共享的后台服务:那样会话由后台进程执行、记录也由它写,看不出哪个终端在哪个会话;
              # 自己跑的话,终端里的 codex 进程一直开着当前会话的记录文件(网页据此对应终端和会话)
              "extra": ["--no-daemon"]},
    "agy": {"label": "Antigravity", "short": "agy", "bin": AGY_BIN, "cmd": [AGY_BIN, "--dangerously-skip-permissions"],
            "marker": ">", "prompt": ">", "steer": None, "busy": ["esc to interrupt", "esc to stop"]},
}
DEFAULT_CLI = "claude"
# 模型:claude 的固定几个(和它 /model 菜单里的名字一样);codex 的读它自己的模型缓存;agy 暂不列
CLAUDE_MODELS = [("claude-opus-5-5", "Opus 5.5"), ("claude-sonnet-5-5", "Sonnet 5.5"),
                 ("claude-fable-5-2", "Fable 5.2"), ("claude-haiku-5-5", "Haiku 5.5")]
# 网页新开会话的默认模型 / 思考强度(网页上改过就用改过的)
DEFAULTS = {"claude": {"model": "claude-opus-5-5", "effort": "medium"}}


def models(cli):
    """[{id, label, efforts, default_effort}]"""
    if cli == "claude":
        return [{"id": m, "label": lab, "efforts": EFFORTS, "default_effort": None} for m, lab in CLAUDE_MODELS]
    if cli == "codex":
        try:
            ms = json.loads((CODEX_HOME / "models_cache.json").read_text())
            ms = ms.get("models", []) if isinstance(ms, dict) else ms
        except (OSError, ValueError):
            return []
        ms = sorted((x for x in ms if isinstance(x, dict) and x.get("slug") and x.get("visibility", "list") == "list"),
                    key=lambda x: x.get("priority") or 99)
        return [{"id": x["slug"], "label": x.get("display_name") or x["slug"],
                 "efforts": [y["effort"] for y in x.get("supported_reasoning_levels") or [] if y.get("effort")] or EFFORTS,
                 "default_effort": x.get("default_reasoning_level")} for x in ms]
    return []


def model_label(cli, model):
    return next((m["label"] for m in models(cli) if m["id"] == model), model)


def model_args(cli, model):
    if not model:
        return []
    return ["-m", model] if cli == "codex" else ["--model", model]


def effort_args(cli, effort):
    if not effort:
        return []
    if cli == "codex":
        return ["-c", f'model_reasoning_effort="{effort}"']
    return ["--effort", effort]


def codex_efforts():
    """codex 当前模型支持的思考强度(读它自己的 config.toml 和模型缓存),读不到就用通用的几档。"""
    try:
        m = re.search(r'^\s*model\s*=\s*"([^"]+)"', (CODEX_HOME / "config.toml").read_text(), re.M)
        models = json.loads((CODEX_HOME / "models_cache.json").read_text())
        models = models.get("models", []) if isinstance(models, dict) else models
        cur = next((x for x in models if m and x.get("slug") == m.group(1)), None) or (models[0] if models else None)
        levels = [x["effort"] for x in cur.get("supported_reasoning_levels") or [] if x.get("effort")]
        return levels or EFFORTS, cur.get("default_reasoning_level"), cur.get("display_name") or cur.get("slug")
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return EFFORTS, None, None


def cli_info():
    """网页上的选项:每个命令行装没装、可选的思考强度。"""
    out = []
    for k, c in CLIS.items():
        efforts, default, model = codex_efforts() if k == "codex" else (EFFORTS, None, None)
        out.append({"id": k, "label": c["label"], "short": c["short"], "available": bool(shutil.which(c["bin"])),
                    "efforts": efforts, "default_effort": default, "model": model, "steer": bool(c["steer"])})
    return out


_cli_of = {}  # tmux 会话名 → 命令行;list_sessions / new_session 时记下,screen_state 不必每次问 tmux


def session_cli(name):
    if name not in _cli_of:
        r = tmux("show-options", "-v", "-t", f"={name}:", "@ccnb_cli")
        _cli_of[name] = r.stdout.strip() if r.stdout.strip() in CLIS else DEFAULT_CLI
    return _cli_of[name]


def clean_env(env):
    return {k: v for k, v in env.items() if k not in CC_RUNTIME_ENV}


def attach(name):
    """在当前终端 attach 到会话。

    CCNB_MOUSE=off 时给这个 client 换一个去掉 kmous 的 terminfo,tmux 就不会让这个终端上报鼠标
    (Windows 的 PowerShell / cmd 经 ssh 时,鼠标事件可能变成乱码文字输进 cc)。
    tmux 按 client 读 terminfo,所以网页终端和其他终端照常用鼠标。
    """
    env = {k: v for k, v in clean_env(os.environ).items() if k not in ("TMUX", "TMUX_PANE")}
    base = env.get("TERM", "")
    if env.get("CCNB_MOUSE") == "off" and base and not base.endswith("-ccnb-nomouse"):
        nomouse = f"{base}-ccnb-nomouse"  # 保留原名前缀,tmux.conf 里 xterm* 的 terminal-features 仍然匹配
        if subprocess.run(["infocmp", nomouse], capture_output=True).returncode != 0:
            subprocess.run(["tic", "-x", "-"], input=f"{nomouse}|{base} without mouse (MeowNotebook),\n\tkmous@, use={base},\n",
                           capture_output=True, text=True)
        if subprocess.run(["infocmp", nomouse], capture_output=True).returncode == 0:
            env["TERM"] = nomouse
    return subprocess.call(["tmux", "-L", SOCKET, "-f", str(TMUX_CONF), "attach", "-t", f"={name}"], env=env)


def tmux(*args, check=False):
    return subprocess.run(["tmux", "-L", SOCKET, "-f", str(TMUX_CONF), *args],
                          capture_output=True, text=True, check=check)


def list_sessions():
    """ccnb socket 上跑着 cc 的 tmux 会话。别的程序(比如在这里开着的 cc-connect)不算,
    否则网页会把它们当成 cc 终端,猜一个会话挂上去,底部的输入就打进了那个程序。"""
    r = tmux("list-sessions", "-F", "#{session_name}\t#{session_path}\t#{session_created}\t#{session_attached}"
             "\t#{@ccnb_sid}\t#{@ccnb_attach}\t#{pane_id}\t#{pane_pid}\t#{@ccnb_cli}\t#{@ccnb_effort}\t#{@ccnb_model}"
             "\t#{pane_start_command}")
    out = []
    for line in r.stdout.splitlines():
        name, path, created, attached, sid, job, pane, pid, cli, effort, model, cmd = (line.split("\t", 11) + [""] * 12)[:12]
        # attach:接入 cc 后台会话的终端(`claude attach <id>`),关掉它后台会话照常运行
        if job and shlex.join([CC_BIN, "attach", job]) in cmd:
            cli = "claude"
        else:  # 以前开的终端没有 @ccnb_cli:看启动命令
            cli = next((k for k in ([cli] if cli in CLIS else []) + list(CLIS) if shlex.join(CLIS[k]["cmd"]) in cmd), None)
        if not cli:
            continue
        _cli_of[name] = cli
        out.append({"name": name, "cwd": path, "created": int(created or 0), "clients": int(attached or 0),
                    "launch_sid": sid or None, "attach": job or None, "cli": cli, "effort": effort or None, "model": model or None,
                    "pane": pane.lstrip("%"), "pid": int(pid) if pid.isdigit() else None})
    return sorted(out, key=lambda s: -s["created"])


_BORDER_TITLE = re.compile(r"^[─━]{2,}\s+(.+?)\s+[─━]+\s*$")
_BORDER = re.compile(r"^\s*[─━]{8,}")


def _option_col(line, marker="❯", numbered=False):
    """菜单里被选中的那一行("  ❯ 2. 选项"):返回选项文字开始的列,否则 None。
    numbered:选项必须带序号(codex / agy 的输入框也以同一个符号开头,不带序号的是输入框)。"""
    ahead = r"\d+\.\s" if numbered else r"\S"
    m = re.match(r"^(\s*)" + re.escape(marker) + r"\s+(?=" + ahead + ")", line)
    return m.end() if m else None


def _prompt_line(lines, prompt):
    """codex / agy 的输入框:最下面几行里以输入符号开头、不是菜单选项的一行(下面只有状态栏)。"""
    for i in range(len(lines) - 1, max(-1, len(lines) - 12), -1):
        x = lines[i].lstrip(" │┃")
        if (x.startswith(prompt + " ") or x.rstrip() == prompt) and not re.match(rf"{re.escape(prompt)}\s+\d+\.\s", x):
            return i
    return None


def parse_screen(lines, cli="claude", raw=None):
    """从 cc 的屏幕内容看出它在干什么:
    ready  底部有输入框(边框之间以 ❯ 开头的一行),可以输入新消息;
    busy   正在回答(状态栏有 "esc to interrupt");
    menu   弹着选择菜单(权限确认、AskUserQuestion、/model…)时的选项 [{text, sel}] 和上方的说明文字;
    title  输入框上边框里显示的会话标题。
    cli 是 codex / agy 时按它们的界面认:菜单选中项和输入框用各自的符号,菜单优先(弹菜单时输入框会被收起,
    但上面的对话记录里也有以同一符号开头的历史提问)。
    raw:同样的几行但带着颜色(capture-pane -e),用来看提问菜单的标签里当前是哪一题。"""
    c = CLIS.get(cli) or CLIS[DEFAULT_CLI]
    marker = c["marker"]
    st = {"ready": False, "busy": False, "menu": None, "title": None}
    box = None
    if cli == "claude":
        for i in range(len(lines) - 1, 0, -1):
            if lines[i].startswith("❯") and _BORDER.match(lines[i - 1]):
                box = i
                m = _BORDER_TITLE.match(lines[i - 1].strip())
                st["title"] = m.group(1).strip() if m else None
                break
    while lines and not lines[-1].strip():  # 屏幕没写满时末尾是空行
        lines = lines[:-1]
    tail = "\n".join(lines[-8:]).lower()
    st["busy"] = any(b in tail for b in c["busy"])  # 回答中状态栏上的提示(菜单底下的 "Esc to cancel" 不算)
    numbered = cli != "claude"
    if cli != "claude":
        sel = next((i for i in range(len(lines) - 1, max(-1, len(lines) - 40), -1)
                    if _option_col(lines[i], marker, True) is not None), None)
        if sel is None:
            box = _prompt_line(lines, c["prompt"])
    st["ready"] = box is not None
    if box is not None:
        return st
    # 没有输入框:找最下面一个被选中的菜单项,以它的文字列为准收集同一列开头的选项
    sel = next((i for i in range(len(lines) - 1, -1, -1) if _option_col(lines[i], marker, numbered) is not None), None)
    if sel is None:
        return st
    col = _option_col(lines[sel], marker, numbered)

    def is_opt(line):
        if len(line) <= col or line[col] == " ":
            return False
        pre = line[:col]
        return pre.strip() in ("", marker)

    def deeper(line):  # 选项下面的说明行(缩进更深)
        return len(line) > col and not line[:col + 1].strip()

    def part(i):  # 选项、说明行,或夹在选项之间的分隔线(提问菜单里最后的 "Chat about this" 在分隔线下面)
        if i < 0 or i >= len(lines):
            return False
        if is_opt(lines[i]) or deeper(lines[i]):
            return True
        return bool(_BORDER.match(lines[i])) and i + 1 < len(lines) and is_opt(lines[i + 1]) and i - 1 >= 0 \
            and (is_opt(lines[i - 1]) or deeper(lines[i - 1]))

    lo = sel
    while part(lo - 1):
        lo -= 1
    hi = sel
    while part(hi + 1):
        hi += 1
    opts, dcol = [], None
    for i in range(lo, hi + 1):
        x = lines[i]
        indent = len(x) - len(x.lstrip())
        # 说明行和选项文字对齐("1. [ ] " 之后);缩进比它浅的是单独的一项(多选题下面的 "Next")
        extra = opts and deeper(x) and x.strip() and dcol is not None and indent < dcol
        if is_opt(x) or extra:
            text = x[col:].strip() if is_opt(x) else x.strip()
            m = re.match(r"\d+\.\s+(\[.\]\s+)?", text)
            dcol = col + m.end() if m else None
            # "1. Red ✔":提问菜单里回到前面的题时,之前选的答案后面有 ✔
            chosen = bool(re.search(r"\s✔$", text))
            text = re.sub(r"\s+✔$", "", text)
            opts.append({"text": re.sub(r"\s{2,}", "  ", text), "sel": i == sel, "desc": "", "chosen": chosen})
        elif opts and deeper(x) and x.strip():
            opts[-1]["desc"] = (opts[-1]["desc"] + " " + x.strip()).strip()
    while opts and not opts[0]["text"]:
        opts.pop(0)
    if not 1 <= len(opts) <= 20:
        return st
    # 说明文字:选项上方连续的几行非空内容(问题、"Select model"…)
    head, i = [], lo - 1
    while i >= 0 and len(head) < 12:  # 提问的检查页:每题一行问题、一行答案
        x = lines[i].strip()
        if x and (_BORDER.match(lines[i]) or set(x) <= set("▔▁─━")):
            break
        if x:  # 空行跳过:说明和选项之间、标签行和问题之间常隔空行
            head.insert(0, x)
        elif head and cli != "claude":  # codex / agy 的菜单上面没有边框:说明只取紧挨着的一段,再往上是对话记录
            break
        i -= 1
    foot = next((x.strip() for x in lines[hi + 1:] if x.strip() and not _BORDER.match(x)), "")
    # 一次问几个问题时,问题上面有一行标签:"←  ☒ Color  ☐ Pets  ✔ Submit  →"(☒ 答过,☐ 没答,✔ Submit 是最后检查答案那页)
    tabs = None
    for k in range(lo - 1, max(-1, lo - 30), -1):
        m = _TABS.match(lines[k])
        if m:
            tabs = [{"label": lab.strip(), "done": mark == "☒", "submit": mark == "✔"} for mark, lab in _TAB_ITEM.findall(m.group(1))]
            cur = _current_tab(raw[k]) if raw and k < len(raw) else None
            for t in tabs:
                t["cur"] = t["label"] == cur
            head = [h for h in head if not _TABS.match(h)]
            break
        if _BORDER.match(lines[k]):
            continue
    st["menu"] = {"options": opts, "head": "\n".join(head), "foot": foot[:200], "tabs": tabs or None}
    return st


_TABS = re.compile(r"^\s*←\s+(.*?)\s+→\s*$")
_TAB_ITEM = re.compile(r"([☐☒✔])\s+(.+?)(?=\s{2,}[☐☒✔]|\s*$)")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x1b[=>]")


def _current_tab(raw):
    """标签行的原始输出(带颜色):当前这一题的标签有背景色(\\e[48;…m)。返回它的文字,看不出来就 None。"""
    m = re.search(r"\x1b\[[0-9;]*\b48;[0-9;]*m\s*[☐☒✔]\s*([^\x1b]+?)\s*(?:\x1b|$)", raw)
    return m.group(1).strip() if m else None


def screen_state(name, cli=None):
    raw = tmux("capture-pane", "-p", "-e", "-t", f"={name}:").stdout.splitlines()
    return parse_screen([_ANSI.sub("", x) for x in raw], cli or session_cli(name), raw)


def screen_title(name):
    """读取 cc 输入框上边框里显示的会话标题(如 "──── 某某会话 ─")。没有则返回 None。
    cc 的会话可能由后台 worker 运行(/resume 时),hook 不一定触发,但界面上的标题总是对的。"""
    return screen_state(name)["title"]


_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _paste(name, text):
    """用 tmux 的粘贴(bracketed paste)输入文字:多行原样保留,cc 不会把中间的换行当成回车。
    控制字符(ESC 等)去掉,免得文字被当成按键。"""
    text = _CTRL.sub("", text.replace("\r\n", "\n").replace("\r", "\n")).strip("\n")
    if not text.strip():
        raise ValueError("消息是空的")
    buf = f"ccnb-{uuid.uuid4().hex[:12]}"
    r = subprocess.run(["tmux", "-L", SOCKET, "-f", str(TMUX_CONF), "load-buffer", "-b", buf, "-"],
                       input=text.encode(), capture_output=True)
    if r.returncode:
        raise RuntimeError(r.stderr.decode(errors="replace").strip() or "tmux load-buffer 失败")
    tmux("paste-buffer", "-p", "-d", "-b", buf, "-t", f"={name}:")
    time.sleep(0.25 + min(len(text), 20000) / 100000)  # 等 cc 收完粘贴再回车,长文本多等一会儿


def send_text(name, text):
    """把一条消息输进 cc 的输入框并回车。"""
    _paste(name, text)
    tmux("send-keys", "-t", f"={name}:", "Enter")


def steer(name, text):
    """cc 正在回答时把消息插进去(cc 的 Ctrl+X Ctrl+S "send now";codex 回答中的 Enter):
    不用等它答完,也不打断正在跑的命令。"""
    keys = CLIS[session_cli(name)]["steer"]
    if not keys:
        raise ValueError(f"{CLIS[session_cli(name)]['label']} 不支持插进正在进行的回答,请用排队")
    _paste(name, text)
    for i, k in enumerate(keys):
        if i:
            time.sleep(0.05)
        tmux("send-keys", "-t", f"={name}:", k)


_FREE = re.compile(r"^(\d+\.\s+)?(\[.\]\s+)?Type something", re.I)


def free_option(menu):
    """提问菜单里可以自己填答案的那一项("Type something")的序号,没有则 None。"""
    return next((i for i, o in enumerate((menu or {}).get("options", [])) if _FREE.match(o["text"])), None)


def answer_free(name, text):
    """cc 提问时用自己的话回答:移到 "Type something" 那一项直接打字(cc 就是这样填的)。
    单选题接着回车提交;多选题打字后 cc 自动勾上这一项,再回车反而会取消,所以不回车,由网页上点 Next / Submit。"""
    menu = screen_state(name)["menu"]
    i = free_option(menu)
    if i is None:
        raise ValueError("cc 现在的菜单不能自己填答案,请点一个选项")
    cur = next(k for k, o in enumerate(menu["options"]) if o["sel"])
    if i != cur:
        send_keys(name, ["Down" if i > cur else "Up"] * abs(i - cur))
        time.sleep(0.2)
    _paste(name, text)
    multi = re.match(r"^(\d+\.\s+)?\[.\]", menu["options"][i]["text"])
    if not multi:
        tmux("send-keys", "-t", f"={name}:", "Enter")
    return {"multi": bool(multi)}


KEYS = {"Escape", "Enter", "Up", "Down", "Left", "Right", "Tab", "BTab", "Space", *"0123456789"}


def send_keys(name, keys):
    for k in keys:
        if k not in KEYS:
            raise ValueError(f"不支持的按键: {k}")
    if keys:
        tmux("send-keys", "-t", f"={name}:", *keys)


def choose(name, index):
    """在 cc 当前弹出的菜单里选第 index 项:按 ↑/↓ 移过去再回车(按当时的屏幕现算,不靠网页上可能过时的画面)。"""
    menu = screen_state(name)["menu"]
    if not menu or not 0 <= index < len(menu["options"]):
        raise ValueError("cc 现在没有弹出这个菜单,刷新一下再选")
    cur = next(i for i, o in enumerate(menu["options"]) if o["sel"])
    d = index - cur
    send_keys(name, ["Down" if d > 0 else "Up"] * abs(d) + ["Enter"])


def goto_tab(name, index):
    """提问菜单:跳到第 index 个标签(某一题,或最后的 Submit 检查页),按 ←/→ 过去。可以回到答过的题改答案。"""
    menu = screen_state(name)["menu"]
    tabs = (menu or {}).get("tabs")
    if not tabs or not 0 <= index < len(tabs):
        raise ValueError("cc 现在没有在提问,刷新一下再试")
    cur = next((i for i, t in enumerate(tabs) if t["cur"]), None)
    if cur is None:
        raise ValueError("看不出现在在第几题,打开终端用 ←/→ 切换")
    d = index - cur
    send_keys(name, ["Right" if d > 0 else "Left"] * abs(d))


def _wait_menu(name, pred, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        m = screen_state(name)["menu"]
        if m and pred(m):
            return m
        time.sleep(0.2)
    return None


_EFFORT_WORDS = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "extra high": "xhigh", "x-high": "xhigh",
                 "max": "max", "maximum": "max"}


def _claude_menu_effort(name):
    """/model 菜单底下 "◐ Medium effort (default) ←/→ to adjust" 里现在的强度。"""
    for line in reversed(tmux("capture-pane", "-p", "-t", f"={name}:").stdout.splitlines()):
        if "to adjust" in line:
            m = re.search(r"([A-Za-z][A-Za-z -]*?)\s+effort\b", line)
            return _EFFORT_WORDS.get(m.group(1).strip().lower().lstrip("◐◑◒◓○●◔◕ ")) if m else None
    return None


def claude_set_model(name, model=None, effort=None):
    """在 cc 里只对这个会话换模型 / 思考强度:打开 /model 菜单,移到这个模型,←/→ 调强度,按 s(= 只用于本会话)。
    不用 "/model xxx" "/effort xxx":那样会存成 cc 新会话的全局默认,影响以后所有的 cc。"""
    st = screen_state(name)
    if st["busy"] or st["menu"] or not st["ready"]:
        raise ValueError("cc 正在回答或弹着菜单,等它停下再换")
    send_text(name, "/model")
    menu = _wait_menu(name, lambda m: "select model" in m["head"].lower())
    if not menu:
        raise RuntimeError("没打开 cc 的 /model 菜单,打开终端看看")
    try:
        if model:
            label = model_label("claude", model)
            i = next((k for k, o in enumerate(menu["options"]) if re.match(rf"\d+\.\s+{re.escape(label)}(\s|$)", o["text"])), None)
            if i is None:
                raise ValueError(f"cc 的 /model 菜单里没有 {label}")
            cur = next(k for k, o in enumerate(menu["options"]) if o["sel"])
            send_keys(name, ["Down" if i > cur else "Up"] * abs(i - cur))
            time.sleep(0.3)
        if effort:
            for _ in range(8):
                now = _claude_menu_effort(name)
                if now is None:
                    raise ValueError("这个模型不能调思考强度")
                if now == effort:
                    break
                send_keys(name, ["Right" if EFFORTS.index(effort) > EFFORTS.index(now) else "Left"])
                time.sleep(0.25)
            else:
                raise ValueError(f"没调到 {effort}")
        tmux("send-keys", "-t", f"={name}:", "s")  # s = 只用于本会话(Enter 会存成默认)
    except Exception:
        send_keys(name, ["Escape"])
        raise
    if model:
        set_session_opt(name, "model", model)
    if effort:
        set_session_opt(name, "effort", effort)


def wait_ready(name, timeout=30):
    """等 cc 启动完、出现输入框。弹着菜单(比如信任目录的确认)时返回菜单状态,让网页去选。"""
    end = time.monotonic() + timeout
    st = {"ready": False}
    while time.monotonic() < end:
        if not session_exists(name):
            raise RuntimeError("cc 启动后又退出了,打开终端看看原因")
        st = screen_state(name)
        if st["ready"] or st["menu"]:
            return st
        time.sleep(0.3)
    return st


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


def new_session(cwd, cc_args=(), cols=200, rows=50, latest_sid=None, attach=None, sid=None, cli=DEFAULT_CLI, effort=None,
                model=None):
    """新开一个 tmux 终端运行 cc。attach=<后台会话 id>:改为运行 `claude attach <id>` 接入 cc 后台会话
    (agent 视图里 fork 出的会话都在 cc 的后台进程里跑,不能再 --resume),sid 是它的会话 ID。
    cli:用哪个命令行(CLIS 里的键);effort:思考强度,None 用它自己的默认值。
    codex / agy 不能事先指定会话 ID:给终端一个 "<cli>-<随机>" 的临时 ID,网页先用它打开这个终端的会话页。"""
    if cli not in CLIS:
        raise ValueError(f"不支持的命令行: {cli}")
    if effort and effort not in EFFORTS + ["ultra", "minimal", "none"]:
        raise ValueError(f"不支持的思考强度: {effort}")
    if not shutil.which(CLIS[cli]["bin"]):
        raise ValueError(f"没找到 {CLIS[cli]['bin']} 命令,先安装 {CLIS[cli]['label']}")
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
    # tmux 服务器的全局环境可能带着这些变量,用 env -u 在 pane 里显式清掉
    unset = [x for k in CC_RUNTIME_ENV for x in ("-u", k)]
    if model and not re.fullmatch(r"[\w.:/-]{1,80}", model):
        raise ValueError(f"模型名不对: {model}")
    if attach:
        cli, effort, model = "claude", None, None
        cmd = shlex.join(["env", *unset, CC_BIN, "attach", attach])
    elif cli == "claude":
        cc_args, sid = plan_session_id(cc_args, latest_sid)
        hook = ["--settings", HOOK_SETTINGS] if CC_CMD[0] == CC_BIN else []
        cmd = shlex.join(["env", *unset, *CC_CMD, *hook, *model_args(cli, model), *effort_args(cli, effort), *cc_args])
    else:
        sid = sid or f"{cli}-{uuid.uuid4().hex[:12]}"
        cmd = shlex.join(["env", *unset, *CLIS[cli]["cmd"], *CLIS[cli].get("extra", []), *model_args(cli, model),
                          *effort_args(cli, effort), *cc_args])
    r = tmux("new-session", "-d", "-s", name, "-c", cwd, "-x", str(cols), "-y", str(rows), *envs, cmd)
    if r.returncode:
        raise RuntimeError(r.stderr.strip() or "tmux new-session 失败")
    if sid:
        tmux("set-option", "-t", f"={name}:", "@ccnb_sid", sid)
    if attach:
        tmux("set-option", "-t", f"={name}:", "@ccnb_attach", attach)
    tmux("set-option", "-t", f"={name}:", "@ccnb_cli", cli)
    _cli_of[name] = cli
    if effort:
        tmux("set-option", "-t", f"={name}:", "@ccnb_effort", effort)
    if model:
        tmux("set-option", "-t", f"={name}:", "@ccnb_model", model)
    return name


def set_session_opt(name, key, value):
    if value:
        tmux("set-option", "-t", f"={name}:", f"@ccnb_{key}", value)
    else:
        tmux("set-option", "-u", "-t", f"={name}:", f"@ccnb_{key}")


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


# 带序号的键盘输入:每个网页终端(cid)已写进 PTY 的最大序号。网络差时连接可能悄悄断掉,
# 还在浏览器发送缓冲里的按键就丢了;客户端会保留没确认的输入,重连后补发,这里按序号去重,保证每个按键只写一次。
_input_seq = OrderedDict()
_input_lock = threading.Lock()


def _last_seq(cid):
    with _input_lock:
        return _input_seq.get(cid, 0)


def _claim_seq(cid, seq):
    """seq 还没写过就记下并返回 True。旧连接还没察觉断开、新连接已在补发时,两边可能同时收到同一条。"""
    with _input_lock:
        if seq <= _input_seq.get(cid, 0):
            return False
        _input_seq[cid] = seq
        _input_seq.move_to_end(cid)
        while len(_input_seq) > 256:
            _input_seq.popitem(last=False)
        return True


def bridge(handler, name, cid=None):
    """把一个 WebSocket 连接接到 `tmux attach -t name` 的 PTY 上。
    协议:客户端发文本帧,首字符 '0' = 键盘输入,'1' = {"cols","rows"} 调整大小,
    '2' = "<序号>:<输入>"(带确认的键盘输入,需要 URL 带 cid)。
    服务端发二进制帧 = 终端输出;文本帧 "a<序号>" = 该序号及之前的输入都已写入(连上时先发一次)。"""
    handler.close_connection = True  # WebSocket 结束后不再复用这条 HTTP 连接
    if not ws_accept(handler):
        return
    sock = handler.connection
    cid = cid and (name, cid)
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
        if cid:
            ws_send(sock, f"a{_last_seq(cid)}".encode(), 1)
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
                elif kind == b"2" and cid:
                    seq, _, data = body.partition(b":")
                    seq = int(seq)
                    if _claim_seq(cid, seq):
                        os.write(fd, data)
                    ws_send(sock, f"a{seq}".encode(), 1)
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
