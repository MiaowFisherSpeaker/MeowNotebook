#!/usr/bin/env python3
"""cc hook(仅挂在 cc 启动的进程上):记录"tmux 窗格 → 当前会话"。

SessionStart(启动 / --resume / 终端内 /resume / /clear / compact)和 UserPromptSubmit 时触发,
从 stdin 读到 session_id,连同环境变量 TMUX_PANE 写到 data/panes/<pane>.json,
网页据此精确知道每个终端此刻在哪个会话。不向 stdout 输出任何内容(否则会进入对话上下文)。
"""
import json
import os
import sys
import time
from pathlib import Path

try:
    pane = os.environ.get("TMUX_PANE", "").lstrip("%")
    data = json.load(sys.stdin)
    if pane.isdigit() and data.get("session_id"):
        d = Path(__file__).resolve().parent / "data" / "panes"
        d.mkdir(parents=True, exist_ok=True)
        # 记下切换前的会话:从 agent 视图重新进入时 cc 会换新 id,新会话写出历史之前网页先留在旧会话上
        try:
            old = json.loads((d / f"{pane}.json").read_text())
        except (OSError, ValueError):
            old = {}
        prev = old.get("session_id") if old.get("session_id") != data["session_id"] else old.get("prev")
        tmp = d / f"{pane}.tmp"
        tmp.write_text(json.dumps({"session_id": data["session_id"], "event": data.get("hook_event_name"),
                                   "source": data.get("source"), "cwd": data.get("cwd"), "ts": time.time(),
                                   "prev": prev}))
        tmp.replace(d / f"{pane}.json")
except Exception:  # noqa: BLE001  hook 失败绝不能影响 cc
    pass
sys.exit(0)
