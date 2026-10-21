# MeowNotebook

把 cc(命令行 AI 编程助手)的对话整理成本地网站,重点是**问答 + 公式渲染**(终端 TUI 不渲染公式)。
个人工具,与任何厂商无关;所有数据只在本机。

- **原始问答**:自动读取 cc 的本地会话记录(也就是 `/resume` 能看到的所有会话),按目录分组;每轮显示"问 → 答",过程文字和工具调用折叠起来。进行中的会话每 3 秒自动刷新。
- **整理版**:在任意 cc 会话里执行 `/qa-notes`(整理当前会话)或 `/qa-notes <会话ID>`(整理历史会话),把问答改写成结构化笔记,公式统一成 LaTeX,存到 `data/notes/<会话ID>.md`。网站上点"复制 /qa-notes 命令"可以直接拿到命令。

## 安装

```bash
git clone https://github.com/MiaowFisherSpeaker/MeowNotebook.git ~/projects/MeowNotebook
cd ~/projects/MeowNotebook && ./install.sh     # 卸载: ./install.sh --uninstall
```

依赖:Python ≥ 3.8、tmux、cc 命令行。安装脚本会:

1. 在 `~/.bash_aliases`(zsh 为 `~/.zshrc`)写入一个 `# >>> meow-notebook >>>` 块,source `shell/cc.bash`,提供
   `cc`(启动)、`ccr`(选择历史会话继续)、`ccd`(后台启动)、`ccweb`(只开网页)、`ccls`(列出会话)、`cca`(回到会话);
2. 把 `skill/qa-notes` 链接到 cc 数据目录下的 `skills/qa-notes`。

可重复执行;卸载不会删除 `data/` 里的整理稿和收藏。

## ⚠ 提醒

1. **`cc` 遮住了 C 编译器** `/usr/bin/cc`(仅交互式 shell,`make` 等构建不受影响)。需要编译器时用 `command cc` 或 `gcc`;
   不想遮住就改 `shell/cc.bash` 里的函数名。
2. **bypass 模式**:`cc` 以 `--dangerously-skip-permissions` 启动,执行命令、改文件都不再询问,只在信任的目录用。
3. **网页终端 = 免密的 bypass shell**。只监听 127.0.0.1;远程服务器用 VS Code 端口转发或
   `ssh -L 8765:localhost:8765 …` 访问,**不要把 8765 暴露到公网**。若确需远程直连,先在前面加带登录认证的反向代理。
4. **会话不随窗口结束**:关网页、断 SSH、在终端里 detach,cc 都继续运行;`ccls` 查看、`cca` 回去、网页上"结束会话"。
5. 第一次运行 `cc` 时会在终端里再提示一次以上要点。
6. 想继续历史会话时可以用 `ccr`(或 `cc -r`)直接进选择列表。先 `cc` 再在里面 `/resume`,cc 会在启动时的新会话里
   记下这条命令,留下一个没有问答的空白会话(网站会自动隐藏这类会话,但 cc 自己的 `/resume` 列表里能看到)。

## 图片

- 你在对话里粘贴的图片显示在问题下方;cc 用 Read 查看过的图片(截图、PDF 页等)折叠在回答下方"cc 查看的图片"里,点击看原图。
- Markdown 中引用的本地图片(`![](/abs/path.png)`、`~/…`、相对会话目录的路径)会从本地读取显示;只放行位图格式,不放 SVG。

## 在网页上对话

会话页 = 上方渲染好公式的问答 + **底部嵌入的真实 cc TUI**。底部就是那个 cc 进程的终端(tmux 会话),
所以输入框、斜杠命令菜单、`@文件` 联想、权限确认、`/model` 等选择菜单、Esc 打断、Shift+Tab 切模式、↑ 翻历史……
与在终端里**完全一致**;问答区每 3 秒同步。

- **运行中**的会话(侧栏绿色"运行中"标记):底部直接可输入。拖动底部标题栏调高度,"放大"临时扩到 72% 屏高。
- **未运行**的会话:底部"▶ 在此继续对话",以 `--resume <ID>`(bypass 模式)在后台启动。
  若该会话正开在别的、不是 `cc` 启动的终端里,按钮禁用,避免两个进程同时写同一会话。
- 新会话在终端里用 `cc` 启动(需要指定工作目录);第一次提问前侧栏显示"新会话(尚未提问)"。
- 终端里 `/resume` 切到别的会话、`/clear` 开新会话时,页面自动跟过去。判断"终端此刻在哪个会话"按以下优先级:
  1. 读取 cc 输入框上边框显示的会话标题,匹配同目录下标题相同、最近更新的会话(最可靠:
     cc 在 `/resume` 时可能把会话交给后台 worker 运行,hook 不一定触发);
  2. `cc` 启动时通过 `--settings` 挂的 hook(`hook.py`)记录在 `data/panes/` 的会话;
  3. 启动参数(`--session-id` / `--resume`)。
- 本地终端(`cc`)和网页同时连着时,窗口尺寸跟随最近操作的那一边,切回另一边会自动恢复。

## 问题地图 · 定位 · 收藏

- **问题地图**:问答左侧垂直居中的横条,最多 15 根(以当前问题为中心,滚轮翻动);平时一样长,
  指针移上去按问题长短展开,并浮出附近几问的文字,点击跳转。整理版和 Markdown 文件里则是标题地图。
- **定位**:打开会话直接跳到最新一问;链接 `#/s/<会话ID>/<轮次>` 跳到指定轮。
- **收藏**:每个问题右上角 ☆;侧栏"★ 收藏"汇总。收藏保存问答快照,原始记录被清理后仍在。

## 搜索

侧栏搜索框左边可以切换搜索范围(记住上次的选择):

- **标题**:只在浏览器里过滤标题 / 首问 / 目录 / 会话 ID,最轻,不请求服务器。
- **提问**:搜索所有会话里你问过的问题。
- **全文**:搜索提问 + 回答(含过程文字)+ 整理版。

多个词用空格分隔,同一轮里全部出现才算命中,不区分大小写。结果按会话分组,每处命中带上下文摘要;
点击直接跳到那一轮,正文里高亮命中词(折叠的过程文字会自动展开)。会话记录在服务端已有解析缓存,
第一次搜索稍慢(要解析全部会话),之后一般在几十毫秒内。

## 只读文件浏览

- 侧栏"文件"标签:浏览目录(默认当前会话目录),可显示隐藏文件。
- 预览:Markdown(公式渲染 + 标题地图)、代码/文本(语法高亮 + 行号)、图片、PDF(支持分段加载);其他文件可下载。
- 对话里出现的文件路径(如 `09_xxx.md:81`、`~/a/b.py`)可以点击,直接打开预览并定位到该行。
- 只读:没有编辑、删除、上传。原始文件除图片/PDF 外一律以纯文本 + CSP sandbox 返回,HTML/SVG 不会在本站执行。

## 数据都在本地

- 读取:cc 数据目录下的 `projects/*/*.jsonl`(cc 自己的会话记录,只读)
- 写入:`data/notes/`(整理稿)、`data/stars.json`(收藏)、`data/panes/`(终端↔会话对应)、`data/server.log`
- `data/` 已在 `.gitignore` 中,不会进仓库。

## 网页终端(`cc`)

```bash
cc            # 当前目录以 --dangerously-skip-permissions 启动 cc,本终端照常使用
ccr           # 启动时直接进入会话选择列表(= cc -r),继续历史会话
cc -c         # 其余参数原样传给 cc(-c / --resume <ID> / --model …)
ccd           # 只在后台启动,去网页上用
```

`cc` 把会话放进专用 tmux 会话(`tmux -L ccnb`),网页会话页底部接入**同一个**会话:
本地终端与浏览器画面、输入完全同步;关掉浏览器或断开 SSH,cc 继续运行。只转发终端字节,不额外消耗 token。

- 启动时指定 `--session-id`,以便把终端和问答记录精确对应。
- 安全:只监听 127.0.0.1;WebSocket / POST 要求同源,且只接受 localhost 主机名(防止你浏览的其他网站或 DNS rebinding 连上本地终端)。
  **不要把 8765 直接暴露到公网**——它等于一个免密的 bypass 模式 shell。

## 使用

```bash
python3 ccnb.py ensure-server      # 后台启动,默认 http://localhost:8765
python3 ccnb.py serve              # 前台启动
python3 ccnb.py list               # 终端里列出会话(● 进行中,✎ 已整理)
python3 ccnb.py extract --current  # 导出当前会话问答(Markdown)
```

## 配置(环境变量)

| 变量 | 默认 | 说明 |
|---|---|---|
| `CCNB_PORT` | `8765` | 网站端口 |
| `CCNB_HOST` | `127.0.0.1` | 监听地址(不要改成公网地址) |
| `CC_BIN` | 见 `term.py` | cc 命令行的可执行文件名 |
| `CC_HOME` | `~/.<CC_BIN>` | cc 的数据目录(会话记录在其下 `projects/`) |
| `CCNB_ALLOWED_HOSTS` | — | 额外允许的主机名,逗号分隔 |

## 结构

- `ccnb.py`:会话解析、HTTP 服务、CLI,只用 Python 标准库
- `term.py`:tmux 会话管理、WebSocket ↔ PTY 桥接
- `static/`:前端(原生 JS;marked、DOMPurify、KaTeX、xterm.js、highlight.js、JetBrains Mono 字体都在 `static/vendor/`,离线可用)
- `data/notes/`:整理稿
- `skill/qa-notes/`:`/qa-notes` skill(由 `install.sh` 链接)
- `shell/cc.bash`:`cc` / `ccr` / `ccd` / `ccweb` / `ccls` / `cca` 的定义(由 `install.sh` 接入 shell)
- `install.sh`:安装 / 卸载
- `hook.py`:挂在 `cc` 启动的进程上的 hook,记录终端当前所在会话
