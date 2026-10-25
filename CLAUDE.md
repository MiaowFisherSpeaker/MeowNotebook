# MeowNotebook

用户说"帮我配置 QQ 机器人"(README 建议的说法是"帮我配置 QQ 机器人,不要在会话中明文显示 AppSecret")时,按 README.md 第三部分"QQ 机器人手动配置"的步骤带用户做:

- **绝不让用户把 AppSecret 发在对话里**(会话记录会显示在网站上)。请用户在自己的终端里运行
  `python3 ccnb.py qq setup`(交互式输入,不回显;不能用 `!` 在 cc 里运行),它会先验证再写入 `data/qqbot.json`。
- 不要读取或打印 `data/qqbot.json` 里的 `app_secret`。
- 写好后重启网站(结束旧的 `ccnb.py serve` 进程,再 `python3 ccnb.py ensure-server`),
  确认 `curl -s localhost:8765/api/sessions` 返回的 `qq.connected` 为 true;再请用户在 QQ 里发一条消息,到网页侧栏点"允许"。
- 收不到消息时用 `python3 ccnb.py qq probe` 排查(先停掉网站,免得两边同时连)。
