#!/usr/bin/env bash
# MeowNotebook 安装脚本(可重复执行)。
#   ./install.sh            安装:shell 命令(cc/ccd/ccweb/ccls/cca)+ /qa-notes skill
#   ./install.sh --uninstall 卸载(不删除 data/ 里的整理稿和收藏)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BEGIN="# >>> meow-notebook >>>"
END="# <<< meow-notebook <<<"
OLD_BEGIN="# >>> cc-notebook >>>"   # 改名前的块,安装时一并清掉
OLD_END="# <<< cc-notebook <<<"
# cc 命令行的可执行文件名与数据目录(与 term.py 中的 CC_BIN / CC_HOME 一致)
CC_BIN="${CC_BIN:-claude}"
CC_HOME="${CC_HOME:-$HOME/.$CC_BIN}"
SKILL_LINK="$CC_HOME/skills/qa-notes"

case "${SHELL##*/}" in
  zsh) RC="$HOME/.zshrc" ;;
  *)   RC="$HOME/.bash_aliases" ;;   # Ubuntu 默认的 ~/.bashrc 会 source 它
esac

strip_block() {  # 删除 rc 文件里旧的块(含改名前的块)
  [ -f "$RC" ] || return 0
  python3 - "$RC" "$BEGIN" "$END" "$OLD_BEGIN" "$OLD_END" <<'EOF'
import re, sys
p, b, e, ob, oe = sys.argv[1:]
s = open(p).read()
for x, y in ((b, e), (ob, oe)):
    s = re.sub(re.escape(x) + r".*?" + re.escape(y) + r"\n?", "", s, flags=re.S)
open(p, "w").write(s)
EOF
}

if [ "${1:-}" = "--uninstall" ]; then
  strip_block
  [ -L "$SKILL_LINK" ] && rm "$SKILL_LINK"
  echo "已卸载。data/ 里的整理稿与收藏保留;新开终端后 cc 恢复为 C 编译器。"
  exit 0
fi

# 1. 依赖
missing=()
for c in python3 tmux "$CC_BIN"; do command -v "$c" >/dev/null || missing+=("$c"); done
if [ ${#missing[@]} -gt 0 ]; then
  echo "缺少依赖: ${missing[*]}(tmux 可用 apt install tmux 安装)"; exit 1
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || { echo "需要 Python ≥ 3.8"; exit 1; }

# 2. shell 命令:在 rc 文件里写一个可替换的块
strip_block
if [ -f "$RC" ] && grep -qE '^\s*cc\(\)' "$RC"; then
  echo "注意:$RC 里已有别的 cc() 定义,它可能覆盖本项目的 cc,请手动检查。"
fi
{
  echo "$BEGIN"
  echo "export CCNB_HOME=\"$HERE\""
  echo "[ -f \"\$CCNB_HOME/shell/cc.bash\" ] && . \"\$CCNB_HOME/shell/cc.bash\""
  echo "$END"
} >> "$RC"

# 3. /qa-notes skill
mkdir -p "$(dirname "$SKILL_LINK")"
if [ -e "$SKILL_LINK" ] && [ ! -L "$SKILL_LINK" ]; then
  echo "注意:$SKILL_LINK 已存在且不是链接,跳过 skill 安装。"
else
  ln -sfn "$HERE/skill/qa-notes" "$SKILL_LINK"
fi
mkdir -p "$HERE/data/notes"

cat <<EOF

✅ 已安装。新开一个终端(或执行 source $RC)后可用:
   cc / ccr / ccd / ccweb / ccls / cca     (说明见 $HERE/shell/cc.bash)
   /qa-notes                         (在任意 cc 会话里整理问答)

⚠ 提醒
  1. cc 会在交互式 shell 里遮住 C 编译器 /usr/bin/cc;需要时用 command cc 或 gcc。
  2. cc 以 --dangerously-skip-permissions 启动:执行命令、改文件不再询问,只在信任的目录用。
  3. 网页终端 = 免密的 bypass shell。只监听 127.0.0.1;远程服务器用 VS Code 端口转发
     或 ssh -L 8765:localhost:8765 访问,不要把 8765 暴露到公网。
  4. 数据都在本地:读取 $CC_HOME/projects,写入 $HERE/data(已 gitignore)。
EOF
