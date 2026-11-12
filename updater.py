"""版本号与网站自身的更新(仅标准库)。

- 版本号:仓库根目录的 VERSION(语义化版本)。HEAD 正好在 tag vX.Y.Z 上 = 正式版 X.Y.Z;
  否则显示为 X.Y.Z-dev+<提交>(开发版)。
- 更新通道:正式版跟最新的 vX.Y.Z tag,开发版跟 origin/main。
- 每 6 小时 git fetch 一次(CCNB_UPDATE=off 关掉);有新版本时网页上提示,点"更新"才更新。
  自动更新默认关(网页设置里打开)。更新只做快进(git merge --ff-only):本地有改动、有自己的提交时一律不动,只提示原因。
- 更新完重启网站进程;cc 会话在 tmux 里,不受影响。
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SETTINGS = ROOT / "data" / "settings.json"
INTERVAL = float(os.environ.get("CCNB_UPDATE_HOURS", "6")) * 3600
ENABLED = os.environ.get("CCNB_UPDATE", "on") != "off"
DEV_BRANCH = "main"
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

_lock = threading.Lock()
_status = {}
_listeners = []


def file_version():
    try:
        return (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


def _git(*args, timeout=20):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "", "LC_ALL": "C"}
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=timeout, env=env)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else f"git {args[0]} 失败")
    return r.stdout.strip()


def _is_repo():
    try:
        return _git("rev-parse", "--is-inside-work-tree") == "true"
    except Exception:  # noqa: BLE001  没装 git / 不是 git 仓库
        return False


def settings():
    try:
        s = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        s = {}
    u = s.get("update") or {}
    return {"channel": u.get("channel") if u.get("channel") in ("stable", "dev") else "stable", "auto": bool(u.get("auto"))}


def save_settings(channel=None, auto=None):
    try:
        s = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        s = {}
    u = {**settings(), **{k: v for k, v in (("channel", channel), ("auto", auto)) if v is not None}}
    if u["channel"] not in ("stable", "dev"):
        raise ValueError("通道只能是 stable / dev")
    s["update"] = u
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    with _lock:
        _status.update(channel=u["channel"], auto=u["auto"])
    return u


def current():
    """当前运行的版本:{version, commit, release}。release = HEAD 正好在正式版 tag 上。"""
    v = file_version()
    if not _is_repo():
        return {"version": v, "commit": None, "release": True}
    try:
        commit = _git("rev-parse", "--short=7", "HEAD")
    except Exception:  # noqa: BLE001
        return {"version": v, "commit": None, "release": True}
    tags = [t for t in _git("tag", "--points-at", "HEAD").split() if TAG_RE.match(t)]
    if tags:
        return {"version": max(tags, key=_tag_key)[1:], "commit": commit, "release": True}
    return {"version": f"{v}-dev+{commit}", "commit": commit, "release": False}


def _tag_key(t):
    return tuple(int(x) for x in TAG_RE.match(t).groups())


def _target(channel):
    """(提交, 版本号) — 正式版:最新的 vX.Y.Z;开发版:origin/main。"""
    if channel == "dev":
        sha = _git("rev-parse", f"origin/{DEV_BRANCH}")
        try:
            v = _git("show", f"{sha}:VERSION").strip()
        except Exception:  # noqa: BLE001
            v = file_version()
        return sha, f"{v}-dev+{sha[:7]}"
    tags = [t for t in _git("tag", "-l", "v*").split() if TAG_RE.match(t)]
    if not tags:
        return None, None
    best = max(tags, key=_tag_key)
    return _git("rev-parse", f"{best}^{{commit}}"), best[1:]


def check(fetch=True):
    """检查更新,返回状态(也缓存起来给 /api/update)。"""
    s = settings()
    st = {"current": current(), "channel": s["channel"], "auto": s["auto"], "enabled": ENABLED,
          "checked": time.time(), "available": False, "can_apply": False, "reason": None, "error": None,
          "target": None, "commits": []}
    try:
        if not _is_repo():
            raise RuntimeError("不是 git 仓库,无法自动更新")
        if fetch:
            _git("fetch", "--quiet", "--tags", "--prune", "--no-auto-gc", "origin", timeout=60)
        sha, ver = _target(s["channel"])
        head = _git("rev-parse", "HEAD")
        if sha and sha != head:
            ancestor = subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", "HEAD", sha]).returncode == 0
            if ancestor:
                log = _git("log", "--format=%h%x09%s", "-n", "40", f"HEAD..{sha}")
                st.update(available=True, target={"version": ver, "commit": sha[:7]},
                          commits=[dict(zip(("commit", "subject"), ln.split("\t", 1))) for ln in log.splitlines() if "\t" in ln])
                dirty = _git("status", "--porcelain", "--untracked-files=no")
                if dirty:
                    st["reason"] = "本地有未提交的改动,不能自动更新(先提交或还原,或者手动 git pull)"
                else:
                    st["can_apply"] = True
    except Exception as e:  # noqa: BLE001
        st["error"] = str(e)
    with _lock:
        _status.clear()
        _status.update(st)
    return st


def status():
    with _lock:
        return dict(_status) if _status else None


def apply():
    """快进到目标版本,然后重启网站进程。"""
    st = check(fetch=False)
    if not st["available"]:
        raise RuntimeError(st["error"] or "已经是最新版本")
    if not st["can_apply"]:
        raise RuntimeError(st["reason"] or "不能自动更新")
    sha, _ = _target(st["channel"])
    _git("merge", "--ff-only", "--quiet", sha, timeout=60)
    threading.Timer(0.8, restart).start()
    return st["target"]


def restart():
    """原地换成新代码:监听端口随 exec 关闭,新进程重新监听;tmux 里的 cc 会话不受影响。"""
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, str(ROOT / "ccnb.py"), "serve"])


def on_change(fn):
    _listeners.append(fn)


def loop():
    """后台:启动 1 分钟后检查一次,之后每 6 小时;打开了自动更新且能快进就直接更新。"""
    if not ENABLED:
        return
    time.sleep(60)
    while True:
        before = (status() or {}).get("target")
        st = check()
        if st["available"] and st["auto"] and st["can_apply"]:
            print(f"updater: 自动更新到 {st['target']['version']}", file=sys.stderr, flush=True)
            try:
                apply()
            except Exception as e:  # noqa: BLE001
                print("updater:", e, file=sys.stderr, flush=True)
        elif st["target"] != before:
            for fn in _listeners:
                fn(st)
        time.sleep(INTERVAL)
