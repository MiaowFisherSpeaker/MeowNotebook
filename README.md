# MeowNotebook

把 cc(命令行 AI 编程助手)的对话变成本地网站:问答**渲染好公式**,可以整理成笔记、按主题总结、收藏、搜索;
网页底部就是那个 cc 的真实终端,可以接着对话。个人工具,与任何厂商无关,所有数据只在本机。

- [一、快速上手](#一快速上手)
- [二、QQ 机器人(可选)](#二qq-机器人可选)
- [三、详细配置与自定义](#三详细配置与自定义)

---

## 一、快速上手

### 安装

```bash
git clone https://github.com/MiaowFisherSpeaker/MeowNotebook.git ~/projects/MeowNotebook
cd ~/projects/MeowNotebook && ./install.sh     # 卸载: ./install.sh --uninstall
```

依赖:Python ≥ 3.8、tmux、cc 命令行。装好后**新开一个终端**,下面的命令就能用了。

### 常用命令

| 命令 | 作用 |
|---|---|
| `cc` | 在**当前目录**开一个新的 cc 会话。网站会自动启动,不用另外开 |
| `ccr` | 在**当前目录**打开已有会话继续:列出这个目录的历史会话,选一个 |
| `ccls` | 列出正在运行的 cc 会话 |
| `cca [名字]` | 回到某个会话(不给名字就回到最近的一个) |
| `ccweb` | 只启动网站,不开 cc |

网站地址 **http://localhost:8765**。在远程服务器上用的话,用 VS Code 端口转发或 `ssh -L 8765:localhost:8765 你的服务器` 访问。

关网页、断 SSH、离开终端,cc 都继续在后台运行:`ccls` 看还有哪些,`cca` 回去。

### 笔记

- **原始问答**:所有 cc 会话自动出现在侧栏(按目录分组),每轮"问 → 答",公式渲染好,过程文字和工具调用折叠起来。
- **整理版**:在 cc 里执行 `/qa-notes`,把这次对话整理成结构化笔记(公式统一成 LaTeX),网页上"整理版"标签查看。
  整理历史会话用 `/qa-notes <会话ID>`,网页上"复制 /qa-notes 命令"可以直接拿到这条命令。
- **总结**:侧栏"✦ 总结",写一句要求(比如"只要论文""我开发 MeowNotebook 的进展",留空 = 全部),
  选**某一天**或**一段时间**,cc 会读这期间的问答写一份总结。历史总结都留着。用到的提示词见[这里](#总结的提示词)。
- **收藏**:每个问题右上角 ☆,侧栏"★ 收藏"汇总;收藏存的是快照,原始记录没了也还在。
- **复制为 Markdown**:正文里选中一段,右键 → 复制为 Markdown(公式、表格、代码块都还原成源码)。
- **搜索**:侧栏搜索框,左边切换范围:标题 / 提问 / 全文。

### 在网页上接着对话

- 会话页底部就是那个 cc 的终端,输入框、斜杠命令、Esc 打断……和在终端里完全一样。
- 没在运行的会话,点底部"▶ 在此继续对话"。
- 侧栏项目名右边的 **new**:在这个目录开一个新 cc。

### ⚠ 必看

1. **`cc` 以 bypass 模式运行**(`--dangerously-skip-permissions`):执行命令、改文件都不再询问,只在信任的目录用。
2. **网站 = 免密的 bypass 终端**。它只监听本机;**不要把 8765 端口暴露到公网**。
3. **`cc` 遮住了 C 编译器** `/usr/bin/cc`(只影响交互式 shell,`make` 不受影响)。要用编译器就 `command cc` 或 `gcc`。
4. 继续历史会话请用 `ccr`。先 `cc` 再在里面 `/resume` 会多留下一个空白会话(网站会隐藏它,cc 自己的列表里看得到)。

---

## 二、QQ 机器人(可选)

在 QQ 私聊里直接和 cc 对话。MeowNotebook 的服务器自己连 QQ 开放平台(官方机器人),不需要别的桥接程序;
没配置时完全不启用。

### 配置

1. 在 [q.qq.com](https://q.qq.com) 创建一个机器人(这一步要你自己在网页上做)。
2. 在 MeowNotebook 目录里开 cc:

   ```bash
   cd ~/projects/MeowNotebook && cc
   ```

   然后跟它说:

   ```text
   帮我配置 QQ 机器人,不要在会话中明文显示 AppSecret
   ```

   它会带你做完剩下的步骤。AppSecret 要在你自己的终端里运行 `python3 ccnb.py qq setup` 输入(不显示),
   不要直接发在对话里:对话会被记录,也会显示在网站上。

想自己动手,见第三部分的 [QQ 机器人手动配置](#qq-机器人手动配置)。

### 使用

直接发消息就是提问(第一次自动开新会话,目录是 `work_dir`)。命令:

| 命令 | 作用 |
|---|---|
| `/new [目录]` | 开一个新会话 |
| `/where` | 当前会话和网页链接 |
| `/stop` | 打断正在运行的回答 |
| `/help` | 说明 |

- QQ 里的对话就是普通的 cc 会话:侧栏标 "QQ"(可以按来源筛选),也能在网页上接着聊。
  会话正开在网页终端里时 QQ 不会去抢,会回复提示;QQ 正在回答时,网页也会等它答完。
- QQ 不渲染 Markdown 和公式,机器人会让 cc 用 Unicode 写公式;回答太长时分几条发,其余到网页看。
- **QQ 的限制**:机器人只能回复你发来的消息(60 分钟内,每条最多回 5 次),不能主动找你。
  所以网页上的"发送到 QQ"(整理版、总结)只在你最近给机器人发过消息的 60 分钟内能用。

### 连不上 / 收不到消息

- 侧栏 **● QQ** 是红的:指针移上去看错误。以前能用、现在连不上,多半是 QQ 开放平台改了规则,
  看 [QQ 机器人文档](https://bot.q.qq.com/wiki)的公告,或者 cc-connect 的 QQ 适配
  [最近的改动](https://github.com/chenhg5/cc-connect/commits/main/platform/qqbot)找线索。
- 连上了但收不到消息:先停掉网站,运行 `python3 ccnb.py qq probe`,它只连接并打印 QQ 发来的所有事件。
- 还是不行,或者发现 bug,欢迎到 [Issues](https://github.com/MiaowFisherSpeaker/MeowNotebook/issues) 提 issue。
  附上 ● QQ 悬停显示的错误和 `data/server.log` 里 `qqbot:` 开头的几行;**不要贴 AppSecret**。

---

## 三、详细配置与自定义

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `CCNB_PORT` | `8765` | 网站端口 |
| `CCNB_HOST` | `127.0.0.1` | 监听地址(不要改成公网地址) |
| `CC_BIN` | `claude` | cc 命令行的可执行文件名 |
| `CC_HOME` | `~/.<CC_BIN>` | cc 的数据目录(会话记录在其下 `projects/`) |
| `CCNB_ALLOWED_HOSTS` | — | 额外允许的主机名,逗号分隔(经反向代理访问时用) |
| `CCNB_MOUSE` | — | 设为 `off`:`cc` / `cca` 进入的终端不开鼠标上报(见下) |

**Windows 的 PowerShell / cmd 里鼠标乱码**:经 ssh 用 `cc` 时,移动鼠标可能在输入框里打出 `[<35;12;5M` 这类文字。
在服务器的 `~/.bashrc` 里 `export CCNB_MOUSE=off` 即可(滚动改用 tmux 的 `Ctrl-b [`);网页终端不受影响。

### QQ 机器人手动配置

也是 cc 帮你配置时照着做的步骤:

1. q.qq.com 上:
   - 记下机器人的 AppID / AppSecret;
   - 开发设置:把服务器的公网出口 IP(`curl ifconfig.me`)加入 IP 白名单;
   - 沙箱配置:把自己的 QQ 加为私聊测试成员,**扫码**打开和机器人的私聊。
2. 在**自己的终端**里(不要在 cc 里用 `!`)运行 `python3 ccnb.py qq setup`:输入 AppID、AppSecret(不显示)和
   QQ 里新会话的工作目录。它会先向 QQ 验证,通过后才写入 `data/qqbot.json`(权限 600,不进仓库)。
3. 重启网站:结束旧的 `ccnb.py serve` 进程,再 `ccweb`。侧栏顶部出现 **● QQ**,绿色 = 已连接。
4. 在 QQ 里给机器人发一条消息,网页侧栏会出现"QQ 用户 … 发来:… [允许]",确认是你自己再点允许。
   **不要允许别人**:机器人以 bypass 模式运行 cc,能在这台机器上执行命令。

### QQ 机器人的全部选项(`data/qqbot.json`)

| 键 | 说明 |
|---|---|
| `app_id` / `app_secret` | QQ 开放平台的 AppID / AppSecret |
| `allow_from` | 允许使用的用户 openid 列表;空 = 谁都不响应(网页上一键允许) |
| `work_dir` | 新会话的工作目录 |
| `sandbox` | `true` = 连沙箱环境 |
| `permission_mode` | cc 的权限模式,默认 `bypassPermissions`;想让它只能读,改成 `default` |

### 总结的提示词

"✦ 总结"把选定日期里的问答(按会话分组)交给 `cc -p`(不带工具、不保存会话),用的是下面这段提示词。
想改就改 `ccnb.py` 里的 `SUMMARY_PROMPT`;`{span}` 是日期范围,`{topic}` 是你写的要求。

```text
标准输入里是我在 {span} 和 cc(命令行 AI 编程助手)的问答记录,按会话分组,每轮是"问"和"答"。
请按我的要求写一份总结。我的要求:{topic}

- 只总结和要求相关的会话与问题,无关的忽略;一条相关的都没有就直接说明。
- 用中文 Markdown:开头 3-5 条要点,然后按主题分节(## 标题),每节末尾用"来源:"列出会话标题。
- 公式用 LaTeX(行内 $...$,独立 $$...$$)。
- 忠实于记录,不编造记录里没有的内容;记录里后来被纠正的结论以纠正后的为准。
- 直接输出总结正文,不要寒暄,不要说"以下是总结"。
```

问答原文超过 40 万字时,会逐步缩短每轮回答再交给 cc。

### 整理版的写法

`/qa-notes` 的整理规则(改写成什么结构、公式怎么写、哪些轮次略去)都在 `skill/qa-notes/SKILL.md`,直接改它就行。

### 更多命令

```bash
ccd                 # 和 cc 一样,但只在后台启动,去网页上用
cc -c               # cc 后面的参数原样传给 cc(-c / --resume <ID> / --model …)
```

`ccnb.py` 是网站背后的脚本,一般用不到:

```bash
python3 ccnb.py serve              # 前台启动网站(ccweb = 后台启动)
python3 ccnb.py list               # 终端里列出会话(● 进行中,✎ 已整理)
python3 ccnb.py extract --current  # 导出当前会话问答(Markdown,/qa-notes 用它)
python3 ccnb.py link               # 当前会话的网页链接(--turn N 指定某一轮)
python3 ccnb.py pending            # 今天更新过、还没整理完的会话
python3 ccnb.py qq setup           # 配置 QQ 机器人(交互式,AppSecret 不显示)
python3 ccnb.py qq probe           # QQ 机器人排查
```

不想让 `cc` 遮住 C 编译器,就改 `shell/cc.bash` 里的函数名。

### 功能细节

**网页终端**

- `cc` 把会话放进专用的 tmux(`tmux -L ccnb`),网页底部接的是**同一个**会话,两边画面和输入完全同步;
  只转发终端字节,不额外消耗 token。两边同时连着时,窗口尺寸跟随最近操作的那一边。
- 拖动底部标题栏调高度,"放大"临时扩到 72% 屏高;"结束会话"关闭这个 cc(记录保留)。
- 网络差时敲的键不会丢:每次输入都等服务器确认,断线重连后补发,不会重复。等太久状态栏会提示,
  断开超过 30 秒的输入不再补发并提示重新输入;页面其余请求连续失败时右下角显示"网络不稳定"。
- 会话正被别的 cc 进程使用(别的终端,或者 QQ 正在回答)时,"在此继续对话"会禁用,避免两个进程同时写同一会话。
  依据是 cc 自己登记的运行中进程(`~/.claude/sessions/<pid>.json`);cc 太老没有这份登记时,看记录 60 秒内有没有被写过。
- 终端里 `/resume`、`/clear` 换了会话,页面自动跟过去。判断"终端此刻在哪个会话"的优先级:
  输入框上边框的会话标题 → cc 登记的进程当前会话 → `hook.py` 记在 `data/panes/` 的会话 → 启动参数。

**问题地图 · 定位**

- 问答左侧的横条是问题地图(最多 15 根,滚轮翻动),指针移上去浮出附近几问,点击跳转;整理版里是标题地图。
- 打开会话直接跳到最新一问;链接 `#/s/<会话ID>/<轮次>` 跳到指定轮。

**搜索**

- 标题:只在浏览器里过滤,不请求服务器;提问:所有会话里你问过的问题;全文:提问 + 回答 + 整理版。
- 多个词用空格分隔,同一轮里全部出现才算命中;点击结果跳到那一轮并高亮。第一次搜索要解析全部会话,稍慢。

**图片与文件**

- 你粘贴的图片显示在问题下方;cc 查看过的图片折叠在回答下方。Markdown 里引用的本地位图会直接显示(不放 SVG)。
- 侧栏"文件"标签:只读浏览目录,预览 Markdown(渲染公式)、代码、图片、PDF;对话里的文件路径可以点击打开并定位到行。
  只读,原始文件一律以纯文本 + CSP sandbox 返回,HTML/SVG 不会在本站执行。

**安全**

- 网站只监听 127.0.0.1;WebSocket / POST 要求同源,且只接受 localhost 主机名(防止别的网站或 DNS rebinding 连上本地终端)。
- 确需远程直连时,先在前面加带登录认证的反向代理,并用 `CCNB_ALLOWED_HOSTS` 放行它的主机名。

### 数据在哪

- 读取:cc 数据目录下的 `projects/*/*.jsonl`(cc 自己的会话记录,只读)。
- 写入(都在 `data/`,不进仓库):`notes/` 整理稿、`summaries/` 总结、`stars.json` 收藏、`panes/` 终端↔会话对应、
  `qqbot.json` / `qqbot-state.json` QQ 机器人配置和状态、`server.log` 日志。
- 卸载(`./install.sh --uninstall`)不会删除 `data/`。

### 项目结构

| 文件 | 内容 |
|---|---|
| `ccnb.py` | 会话解析、网站服务、命令行(只用 Python 标准库) |
| `term.py` | tmux 会话管理、网页终端的 WebSocket 桥接 |
| `qqbot.py` | QQ 机器人(可选) |
| `hook.py` | 挂在 `cc` 启动的进程上的 hook,记录终端当前所在会话 |
| `static/` | 前端(原生 JS;marked、DOMPurify、KaTeX、xterm.js、highlight.js、字体都在 `static/vendor/`,离线可用) |
| `skill/qa-notes/` | `/qa-notes` 的整理规则 |
| `shell/cc.bash` | `cc` / `ccr` / `cca` / `ccls` / `ccweb` / `ccd` 的定义 |
| `install.sh` | 安装 / 卸载:把上面的命令接入 shell,把 `/qa-notes` 链接到 cc 的 skills 目录 |
