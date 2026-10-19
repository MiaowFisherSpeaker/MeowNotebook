# MeowNotebook 的 shell 命令。由 install.sh 加到 ~/.bash_aliases(或 ~/.zshrc)里 source。
#
#   cc [参数...]          当前目录以 --dangerously-skip-permissions 启动 cc(tmux 会话),
#                         同时确保 http://localhost:8765 网页在运行;本终端照常使用
#   ccr                   启动时直接进入会话选择列表(= cc -r),继续某个历史会话;
#                         比先 cc 再 /resume 好:不会多留一个空白会话
#   ccd [参数...]         同上,但只在后台启动,去网页上用
#   ccweb                 只启动网页
#   ccls                  列出 cc 启动的、仍在运行的会话
#   cca [名字]            重新进入某个会话(不给名字则进入最近的一个)
#
# ⚠ 提醒
#   1. cc 会在交互式 shell 里遮住 C 编译器 /usr/bin/cc(make 等构建不受影响);
#      需要编译器时用 `command cc` 或 gcc。不想遮住的话,把下面的 cc 改成别的名字。
#   2. bypass 模式下 cc 执行命令、改文件都不会再询问,只在你信任的目录里用。
#   3. 网页终端等于一个免密的 bypass shell:只监听 127.0.0.1,远程服务器请用 VS Code 端口转发
#      或 `ssh -L 8765:localhost:8765 …` 访问,不要把 8765 暴露到公网。

CCNB_HOME="${CCNB_HOME:-$HOME/projects/MeowNotebook}"

cc()    { python3 "$CCNB_HOME/ccnb.py" launch "$@"; }
ccr()   { python3 "$CCNB_HOME/ccnb.py" launch --resume "$@"; }
ccd()   { python3 "$CCNB_HOME/ccnb.py" launch --detach "$@"; }
ccweb() { python3 "$CCNB_HOME/ccnb.py" ensure-server; }
ccls()  { tmux -L ccnb ls -F '#{session_name}	#{session_path}	#{?session_attached,已连接,后台}' 2>/dev/null || echo "没有运行中的 cc 会话"; }
cca()   {
  local name="${1:-$(tmux -L ccnb ls -F '#{session_created} #{session_name}' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2)}"
  [ -n "$name" ] || { echo "没有运行中的 cc 会话"; return 1; }
  TMUX= tmux -L ccnb -f "$CCNB_HOME/tmux.conf" attach -t "=$name"
}
