---
name: qa-notes
description: 把 cc 对话里用户的提问和回答整理成带公式渲染的笔记,发布到本地 MeowNotebook 网站(http://localhost:8765)。当用户说"整理到网站""整理这次对话""整理笔记""qa-notes",或给出一个会话 ID 要求整理历史对话时使用。参数:可选的会话 ID;不给则整理当前会话。
---

# qa-notes:整理对话到 MeowNotebook

项目位置:`~/projects/MeowNotebook`(网站 + 解析脚本 `ccnb.py`)。网站自动显示所有会话的**原始问答**;本 skill 负责生成**整理版**,存为 `~/projects/MeowNotebook/data/notes/<session-id>.md`,网站上以"整理版"标签显示。

## 步骤

1. **导出待整理的问答。** 有会话 ID 参数就用 `--session <ID>`,否则用 `--current`(按当前工作目录找最新的会话,即本会话):

   ```bash
   python3 ~/projects/MeowNotebook/ccnb.py extract --current --new-only > /tmp/qa-notes-src.md
   ```

   `--new-only` 只导出已有整理稿尚未覆盖的轮次(增量整理);用户要求"重新整理/全部重做"时去掉它。输出开头的注释给出 `note_file`(整理稿路径)和 `total_turns`。用 Read 读这个文件;很长就分段读。

2. **写整理稿。** 已有整理稿就先读它,把新内容**追加/合并**进去;没有就新建。格式:

   ```markdown
   ---
   session: <session-id>
   title: <简短标题>
   updated: <YYYY-MM-DD HH:MM>
   covered_turns: <total_turns>
   ---

   ## <把问题改写成一个清晰的标题>

   > <用户原问题,原文照录;很长时可节选>

   <整理后的回答>
   ```

   整理原则:
   - **以"回答用户的提问"为主线。** 每个实质性问题一节;同一主题的连续追问合并成一节,用 `###` 分小节。寒暄、纯操作指令(如"推送上去""重启")、只是执行任务的轮次可以略去,或在文末"其他操作"里一句话带过。
   - **公式用 LaTeX 写,这是重点。** 行内 `$...$`,独立公式 `$$...$$`(网站用 KaTeX 渲染)。终端里的回答为了 TUI 用了 Unicode 公式(如 `σ²/K^(H−t)`、`δₜ = rₜ + γV(hₜ₊₁) − V(hₜ)`),整理时一律改写成 LaTeX(`$\sigma^2/K^{H-t}$`、`$\delta_t = r_t + \gamma V(h_{t+1}) - V(h_t)$`)。推导可以写得比终端里更完整。
   - **忠实。** 不改变回答的结论;回答里后来被纠正的错误,以纠正后的为准,并可注明"(此前回答有误,已更正)"。不编造原对话里没有的事实。
   - 去掉过程性文字("让我看一下…""我先搜索…")、工具调用细节和文件路径堆砌;保留对理解有用的表格、代码和链接。
   - 语言跟随原对话(中文对话用中文;术语首次出现可附英文)。遵守用户记忆中的固定术语(例如 HPS 相关的 径向/切向)。

   用 Write 写入 `note_file`。`covered_turns` 必须更新为本次的 `total_turns`。

3. **确保网站在运行并给出链接:**

   ```bash
   python3 ~/projects/MeowNotebook/ccnb.py ensure-server --session <session-id>
   ```

   回复用户时只给一句话 + 链接(以及整理了哪几个问题),不要把整理稿全文贴回终端。
