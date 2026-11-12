#!/usr/bin/env python3
"""发布新版本:统一改版本号、写 CHANGELOG、提交并打 tag(不推送)。

  python3 scripts/release.py 1.2.0      改 VERSION / 桌面客户端的版本号,CHANGELOG 加一节,提交 "Release v1.2.0",打 tag v1.2.0
  python3 scripts/release.py --check    检查各处版本号是否一致(CI 用)

推送后(git push && git push origin v1.2.0)GitHub Actions 自动构建 Windows / macOS 安装包并发布;
网站这边,用"正式版"通道的人会在网页上收到更新提示。
"""
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = {
    "VERSION": ROOT / "VERSION",
    "tauri.conf.json": ROOT / "app" / "src-tauri" / "tauri.conf.json",
    "Cargo.toml": ROOT / "app" / "src-tauri" / "Cargo.toml",
    "package.json": ROOT / "app" / "package.json",
}
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def git(*a):
    return subprocess.run(["git", "-C", str(ROOT), *a], check=True, capture_output=True, text=True).stdout.strip()


def versions():
    return {
        "VERSION": FILES["VERSION"].read_text().strip(),
        "tauri.conf.json": json.loads(FILES["tauri.conf.json"].read_text())["version"],
        "Cargo.toml": re.search(r'^version = "([^"]+)"', FILES["Cargo.toml"].read_text(), re.M).group(1),
        "package.json": json.loads(FILES["package.json"].read_text()).get("version", ""),
    }


def set_version(v):
    FILES["VERSION"].write_text(v + "\n")
    for key in ("tauri.conf.json", "package.json"):
        f = FILES[key]
        f.write_text(re.sub(r'^(\s*"version":\s*)"[^"]*"', rf'\g<1>"{v}"', f.read_text(), count=1, flags=re.M))
    f = FILES["Cargo.toml"]
    f.write_text(re.sub(r'^version = "[^"]+"', f'version = "{v}"', f.read_text(), count=1, flags=re.M))
    lock = ROOT / "app" / "src-tauri" / "Cargo.lock"
    if lock.exists():  # Cargo.lock 里本包的版本也跟着改,免得构建时又改动它
        lock.write_text(re.sub(r'(name = "meownotebook"\nversion = )"[^"]+"', rf'\1"{v}"', lock.read_text()))


def changelog(v):
    f = ROOT / "CHANGELOG.md"
    try:
        last = git("describe", "--tags", "--abbrev=0", "--match", "v*")
        rng = f"{last}..HEAD"
    except subprocess.CalledProcessError:
        rng = "HEAD"
    log = [ln for ln in git("log", "--no-merges", "--format=- %s", rng).splitlines() if not ln.startswith("- Release v")]
    head = "# 更新记录\n\n"
    old = f.read_text() if f.exists() else head
    body = old[len(head):] if old.startswith(head) else old
    f.write_text(head + f"## v{v} · {date.today().isoformat()}\n\n" + ("\n".join(log) or "- 维护更新") + "\n\n" + body)


def main():
    if sys.argv[1:] == ["--check"]:
        vs = versions()
        ok = len(set(vs.values())) == 1
        print(("版本号一致: " if ok else "版本号不一致: ") + json.dumps(vs, ensure_ascii=False))
        sys.exit(0 if ok else 1)
    if len(sys.argv) != 2 or not SEMVER.match(sys.argv[1].lstrip("v")):
        sys.exit(__doc__)
    v = sys.argv[1].lstrip("v")
    if git("status", "--porcelain", "--untracked-files=no"):
        sys.exit("工作区有未提交的改动,先提交或还原")
    if git("tag", "-l", f"v{v}"):
        sys.exit(f"tag v{v} 已经存在")
    set_version(v)
    changelog(v)
    paths = [str(p.relative_to(ROOT)) for p in FILES.values()] + ["CHANGELOG.md"]
    lock = ROOT / "app" / "src-tauri" / "Cargo.lock"
    if lock.exists():
        paths.append(str(lock.relative_to(ROOT)))
    git("add", *paths)
    git("commit", "-m", f"Release v{v}")
    git("tag", "-a", f"v{v}", "-m", f"MeowNotebook v{v}")
    print(f"已提交并打 tag v{v}。检查 CHANGELOG.md 无误后推送:\n  git push && git push origin v{v}")


if __name__ == "__main__":
    main()
