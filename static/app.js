// MeowNotebook 前端:会话列表、问答渲染(Markdown + KaTeX)、进行中会话自动刷新
const $ = (s, el = document) => el.querySelector(s);
const state = {
  sessions: [], sid: null, data: null, version: null, jump: null, stars: [], starKeys: new Set(),
  tab: localGet('tab', 'note'), collapsed: new Set(JSON.parse(localGet('collapsed', '[]'))),
};

function localGet(k, d) { try { return localStorage.getItem('ccnb.' + k) ?? d; } catch { return d; } }
function localSet(k, v) { try { localStorage.setItem('ccnb.' + k, v); } catch {} }
function esc(s) { return (s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
function toast(msg) { const t = $('#toast'); t.textContent = msg; t.classList.add('show'); setTimeout(() => t.classList.remove('show'), 1400); }
function fmtTime(ts) {
  if (!ts) return '';
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts);
  if (isNaN(d)) return '';
  const p = n => String(n).padStart(2, '0');
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// ---------- Markdown + 公式 ----------
marked.setOptions({ gfm: true, breaks: true });

const MATH_RE = [
  [/\$\$([\s\S]+?)\$\$/g, true],
  [/\\\[([\s\S]+?)\\\]/g, true],
  [/\\\(([\s\S]+?)\\\)/g, false],
  [/(?<![\\$\w])\$(?!\s)([^$\n]+?)(?<!\s)\$(?![\w$])/g, false],
];

let mdCwd = '';  // 解析 Markdown 中相对路径图片用的目录(当前会话的 cwd)
function localImgs(html) {
  return html.replace(/<img([^>]*?) src="([^"]+)"/g, (m, pre, src) => {
    if (/^(https?:|data:|blob:|\/api\/)/.test(src)) return m;
    const path = src.replace(/&amp;/g, '&');
    return `<img${pre} loading="lazy" src="/api/file?path=${encodeURIComponent(path)}&cwd=${encodeURIComponent(mdCwd)}"`;
  });
}

function imgGrid(sid, ids) {
  return `<div class="imgs">${ids.map(k => `<a href="/api/image/${sid}/${k}" target="_blank" title="点击查看原图"><img loading="lazy" src="/api/image/${sid}/${k}" alt="图片 ${k + 1}"></a>`).join('')}</div>`;
}

const unesc = t => t.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');

// 代码块语法高亮(hljs 输出已转义,可安全插入)
function highlightCode(html) {
  if (!window.hljs) return html;
  return html.replace(/<pre><code(?: class="language-([\w+#-]+)")?>([\s\S]*?)<\/code><\/pre>/g, (m, lang, body) => {
    const code = unesc(body);
    if (code.length > 50000) return m;
    try {
      const r = lang && hljs.getLanguage(lang) ? hljs.highlight(code, { language: lang, ignoreIllegals: true })
        : code.length < 4000 ? hljs.highlightAuto(code) : null;
      return r ? `<pre><code class="hljs">${r.value}</code></pre>` : m;
    } catch { return m; }
  });
}

// 行内代码里像文件路径的(如 `00_索引.md:429`、`~/projects/x/a.py`)变成可点击,打开只读预览
const FILE_EXT = /\.(md|markdown|txt|py|js|mjs|ts|tsx|jsx|json|jsonl|sh|bash|css|html|yaml|yml|toml|ini|conf|tex|bib|csv|tsv|xlsx|ipynb|pdf|png|jpe?g|gif|webp|svg|c|h|cpp|go|rs|java|rb|lua|sql|log|cfg)$/i;
function pathHref(raw) {
  let t = raw.trim(), line = null;
  const lm = t.match(/^(.*?):(\d+)(?:-\d+)?$/);
  if (lm) { t = lm[1]; line = lm[2]; }
  if (!t || /\s/.test(t) || t.length > 300 || /^[a-z]+:\/\//i.test(t)) return null;
  const hasSlash = t.includes('/');
  if (!(FILE_EXT.test(t) || (hasSlash && /^(~|\.{1,2})?\//.test(t) && !t.endsWith('/')))) return null;
  if (!hasSlash && !FILE_EXT.test(t)) return null;
  const abs = /^[\/~]/.test(t) ? t : (mdCwd ? mdCwd.replace(/\/$/, '') + '/' : '') + t.replace(/^\.\//, '');
  return '#/f/' + encodeURIComponent(abs) + (line ? '@' + line : '');
}
function linkPaths(html) {
  return html.replace(/(?<!<pre>)<code>([^<]{2,300})<\/code>/g, (m, body) => {
    const href = pathHref(unesc(body));
    return href ? `<a class="fpath" href="${href}" title="打开只读预览"><code>${body}</code></a>` : m;
  });
}

function renderMD(src) {
  if (!src) return '';
  const code = [], math = [];
  // 1. 先把代码保护起来,避免其中的 $ 被当成公式
  let s = src.replace(/(^|\n)(```|~~~)[^\n]*\n[\s\S]*?\n\2(?=\n|$)/g, m => `\x01C${code.push(m) - 1}\x01`)
             .replace(/`[^`\n]+`/g, m => `\x01C${code.push(m) - 1}\x01`);
  // 2. 抽出公式
  for (const [re, display] of MATH_RE) {
    s = s.replace(re, (_, tex) => `MATHPH${math.push({ tex, display }) - 1}XQ`);
  }
  // 2.5 加粗:CommonMark 规定结尾 ** 前是标点、后面紧跟文字时不算加粗,
  //     中文里"**原因：**后文"非常常见,所以先自己转成 <strong>
  s = s.replace(/\*\*(?=\S)([^*\n]+?)(?<=\S)\*\*/g, '<strong>$1</strong>');
  // 3. 还原代码,交给 marked
  s = s.replace(/\x01C(\d+)\x01/g, (_, i) => code[i]);
  let html = highlightCode(linkPaths(localImgs(DOMPurify.sanitize(marked.parse(s)))));
  // 4. 把占位符换成 KaTeX
  html = html.replace(/MATHPH(\d+)XQ/g, (_, i) => {
    const { tex, display } = math[i];
    try { return katex.renderToString(tex.trim(), { displayMode: display, throwOnError: false, strict: 'ignore' }); }
    catch { return `<span class="math-err">${esc(tex)}</span>`; }
  });
  return html;
}

// ---------- 会话列表 ----------
async function loadSessions() {
  const r = await fetch('/api/sessions').then(r => r.json()).catch(() => null);
  if (!r) return;
  state.sessions = r.sessions;
  onTerms(r.terms);
  renderSessions();
}

function renderSessions() {
  const kw = $('#search').value.trim().toLowerCase();
  if (kw && searchMode() !== 'title') return renderSearch();
  const groups = new Map();
  for (const s of state.sessions) {
    if (kw && !`${s.title} ${s.first_q} ${s.cwd} ${s.id}`.toLowerCase().includes(kw)) continue;
    if (!groups.has(s.cwd)) groups.set(s.cwd, []);
    groups.get(s.cwd).push(s);
  }
  const home = '/home/';
  const html = [...groups].map(([cwd, list]) => {
    const name = cwd.startsWith(home) ? '~/' + cwd.split('/').slice(3).join('/') : cwd;
    const cut = name.lastIndexOf('/') + 1;
    const col = state.collapsed.has(cwd) && !kw ? ' collapsed' : '';
    const live = list.some(x => x.running) ? '<span class="dot live" title="有运行中的会话"></span>' : '';
    return `<div class="proj${col}" data-cwd="${esc(cwd)}" title="${esc(cwd)}"><span class="caret"></span>` +
      `<span class="pn"><span class="pp">${esc(name.slice(0, cut))}</span>${esc(name.slice(cut) || name)}</span>${live}<span class="cnt">${list.length}</span></div><div class="proj-list">` +
      list.map(s => `<a class="sess${s.id === state.sid ? ' on' : ''}" href="#/s/${s.id}">
        <div class="t">${esc(s.title)}</div>
        <div class="m">${s.running ? '<span class="badge run">运行中</span>' : ''}${s.active ? '<span class="dot live" title="记录正在更新"></span>' : ''}${fmtTime(s.mtime)} · ${s.questions} 问
        ${s.noted ? '<span class="badge">整理版</span>' : ''}</div></a>`).join('') + '</div>';
  }).join('');
  $('#sessions').innerHTML = html || '<div class="hint">没有匹配的会话</div>';
}

$('#sessions').addEventListener('click', e => {
  const p = e.target.closest('.proj');
  if (!p) return;
  const cwd = p.dataset.cwd;
  state.collapsed.has(cwd) ? state.collapsed.delete(cwd) : state.collapsed.add(cwd);
  localSet('collapsed', JSON.stringify([...state.collapsed]));
  renderSessions();
});
// ---------- 搜索:标题(本地过滤,最轻)/ 提问 / 全文(服务器搜索会话正文)----------
const PH = { title: '搜索标题 / 首问 / 目录…', q: '搜索所有提问,空格分隔多个词…', all: '搜索提问 + 回答 + 整理版…' };
const sr = { kw: '', mode: '', data: null, timer: null, seq: 0 };
const searchMode = () => $('#search-mode').value;
const searchWords = () => searchMode() === 'title' ? [] : $('#search').value.trim().toLowerCase().split(/\s+/).filter(Boolean);

function markSnip(text, words) {
  let h = esc(text);
  for (const w of words) h = h.replace(new RegExp(esc(w).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'), m => `\x02${m}\x03`);
  return h.replace(/\x02/g, '<mark>').replace(/\x03/g, '</mark>');
}

function renderSearch() {
  const kw = $('#search').value.trim(), mode = searchMode();
  if (sr.kw !== kw || sr.mode !== mode) {  // 关键词变了才请求;定时刷新列表时沿用结果
    sr.kw = kw; sr.mode = mode; sr.data = null;
    const seq = ++sr.seq;
    fetch(`/api/search?scope=${mode}&q=${encodeURIComponent(kw)}`).then(r => r.json()).then(r => {
      if (seq === sr.seq) { sr.data = r; renderSessions(); }
    }).catch(() => {});
  }
  if (!sr.data) { $('#sessions').innerHTML = '<div class="sr-sum">搜索中…</div>'; return; }
  const words = searchWords(), d = sr.data;
  $('#sessions').innerHTML = !d.results?.length ? '<div class="hint">没有匹配的问答</div>'
    : `<div class="sr-sum">${d.sessions} 个会话 · ${d.hits} 处命中${d.sessions > d.results.length ? `(显示前 ${d.results.length} 个会话)` : ''}</div>` +
      d.results.map(s => `<a class="sess${s.id === state.sid ? ' on' : ''}" href="#/s/${s.id}" title="${esc(s.cwd)}">
        <div class="t">${esc(s.title)}</div><div class="m">${fmtTime(s.mtime)} · ${s.count} 处命中</div></a>` +
        s.hits.map(h => `<a class="sr-hit" href="#/s/${s.id}${h.n ? '/' + h.n : ''}"><b>${h.n ? '#' + h.n + ' ' : ''}${h.where}</b>${markSnip(h.snip, words)}</a>`).join('') +
        (s.count > s.hits.length ? `<div class="sr-more">还有 ${s.count - s.hits.length} 处,打开会话后可用浏览器查找</div>` : '')).join('');
}

// 在正文里高亮搜索词,并展开包含命中的"过程文字"
function markArticle() {
  const words = searchWords();
  if (!words.length) return;
  const re = new RegExp(words.map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'gi');
  const walk = document.createTreeWalker($('#article'), NodeFilter.SHOW_TEXT, {
    acceptNode: n => n.parentElement.closest('.katex, script, style, mark') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
  });
  const nodes = [];
  while (walk.nextNode()) if (re.test(walk.currentNode.data)) nodes.push(walk.currentNode);
  for (const n of nodes) {
    const frag = document.createDocumentFragment();
    let last = 0;
    n.data.replace(re, (m, i) => {
      frag.append(n.data.slice(last, i), Object.assign(document.createElement('mark'), { textContent: m }));
      last = i + m.length;
    });
    frag.append(n.data.slice(last));
    n.replaceWith(frag);
  }
  $('#article').querySelectorAll('details.proc').forEach(d => { if (d.querySelector('mark')) d.open = true; });
}

$('#search').addEventListener('input', () => {
  clearTimeout(sr.timer);
  if (searchMode() === 'title') return renderSessions();
  sr.timer = setTimeout(renderSessions, 300);
});
$('#search-mode').addEventListener('change', () => {
  localSet('search-mode', searchMode());
  $('#search').placeholder = PH[searchMode()];
  renderSessions();
  if (state.data) renderSession();
});
$('#search-mode').value = localGet('search-mode', 'title');
$('#search').placeholder = PH[searchMode()];

// ---------- 单个会话 ----------
async function loadSession(force = false) {
  if (!state.sid) return;
  const sid = state.sid;
  const v = force ? '' : (state.version || '');
  const r = await fetch(`/api/session/${sid}?v=${encodeURIComponent(v)}`).then(r => r.json()).catch(() => null);
  if (!r || sid !== state.sid || r.unchanged) return;
  // 这个会话已在新会话里继续(从 agent 视图重新进入时 cc 会换新 id)→ 跳到最新的那个
  if (r.moved) { location.replace('#/s/' + r.moved + (state.jump ? '/' + state.jump : '')); return; }
  if (r.error) { $('#article').innerHTML = `<div class="hint">${esc(r.error)}</div>`; return; }
  const atBottom = $('#main').scrollTop + $('#main').clientHeight > $('#main').scrollHeight - 80;
  const first = !state.data || state.data.meta.id !== r.meta.id;
  state.data = r; state.version = r.version;
  if (first && (r.active || state.jump)) state.tab = 'raw';  // 进行中的会话、或指定了某一轮 → 原始问答
  renderSession();
  if (first) jumpToTurn();
  else if (atBottom && r.active && state.tab === 'raw') $('#main').scrollTop = $('#main').scrollHeight;
}

function renderSession() {
  const { meta, turns, note, active } = state.data;
  mdCwd = meta.cwd || '';
  $('#empty').hidden = true; $('#view').hidden = false;
  $('#view').classList.remove('stars', 'file'); $('#star-link').classList.remove('on');
  fs.lastCwd = meta.cwd;
  $('#title').textContent = meta.title;
  document.title = meta.title + ' · MeowNotebook';
  $('#sub').innerHTML = [
    active ? '<span><span class="dot live"></span> 记录更新中 · 自动刷新</span>' : '',
    `<span>${esc(meta.cwd || '')}</span>`, `<span>${fmtTime(meta.started)} → ${fmtTime(meta.updated)}</span>`,
    `<span>${meta.questions} 问</span>`, `<span><code>${meta.id}</code></span>`,
  ].join('');
  const noteBtn = $('#tabs [data-tab=note]');
  noteBtn.disabled = !note;
  noteBtn.textContent = note ? `整理版${note.meta.covered_turns && +note.meta.covered_turns < turns.length ? `(覆盖到第 ${note.meta.covered_turns} 轮,共 ${turns.length})` : ''}` : '整理版(未整理)';
  const tab = note ? state.tab : 'raw';
  document.querySelectorAll('#tabs [data-tab]').forEach(b => b.classList.toggle('on', b.dataset.tab === tab));
  $('#show-process').parentElement.hidden = $('#show-misc').parentElement.hidden = tab !== 'raw';
  if (state.data.pending) {
    $('#article').innerHTML = '<div class="hint">新会话,还没有提问。<br>在下方的输入框里开始对话,问答会实时显示在这里。</div>';
    $('#toc').innerHTML = '';
  } else tab === 'note' ? renderNote(note) : renderRaw(turns);
  markArticle();
  syncDock();
}

// 打开会话时定位:指定的轮次,否则最新一问
function jumpToTurn() {
  if (!state.data) return;
  const want = state.jump;
  state.jump = null;
  if ((state.tab === 'note' && state.data.note) && !want) return;
  const qs = state.data.turns.filter(t => t.kind === 'question');
  const n = want ?? qs[qs.length - 1]?.n;
  const el = n && document.getElementById('t' + n);
  const mk = want && el?.querySelector('mark');  // 从搜索结果跳来:定位到这一轮里第一个命中处
  if (el) requestAnimationFrame(() => (mk || el).scrollIntoView({ block: mk ? 'center' : 'start' }));
}

function renderNote(note) {
  const m = note.meta;
  $('#article').innerHTML = `<div class="note-meta">整理于 ${esc(m.updated || fmtTime(note.mtime))}${m.covered_turns ? ` · 覆盖第 1–${esc(m.covered_turns)} 轮` : ''}</div>
    <div class="md">${renderMD(note.body)}</div>`;
  const hs = [...$('#article').querySelectorAll('.md h2, .md h3')];
  hs.forEach((h, i) => h.id = 'h' + i);
  renderMap(hs.map(h => ({ id: h.id, text: h.textContent, len: h.textContent.length * 3, lv: +h.tagName[1] })));
}

function rawHTML(turns, active, showProc, showMisc, sid) {
  const label = { question: '问', command: '命令', shell: 'Shell', summary: '上下文压缩摘要' };
  const items = turns.filter(t => showMisc || t.kind === 'question' || t.kind === 'summary');
  const last = turns[turns.length - 1];
  return items.map(t => {
    const main = t.texts.length ? t.texts[t.texts.length - 1] : '';
    const proc = t.texts.slice(0, -1);
    const q = t.kind === 'summary'
      ? `<details><summary>上下文压缩摘要(点击展开)</summary><div class="md">${renderMD(t.q)}</div></details>`
      : `<div class="md">${t.kind === 'question' ? renderMD(t.q) : `<code>${esc(t.q)}</code>`}</div>`;
    const pending = active && t === last && !t.texts.length;
    return `<section class="turn" id="t${t.n}">
      <div class="q ${t.kind}"><div class="lab"><span>#${t.n} ${label[t.kind] || t.kind}</span><span>${fmtTime(t.ts)}</span><span class="sp"></span>${
        t.kind === 'question' && sid ? starBtn(sid, t.ts) : ''}</div>${q}${sid && t.q_images?.length ? imgGrid(sid, t.q_images) : ''}</div>
      ${t.texts.length || t.notes.length || t.images?.length || pending ? `<div class="a">
        <div class="lab"><span>答</span>${t.tools ? `<span>${t.tools} 次工具调用</span>` : ''}</div>
        ${proc.length ? `<details class="proc"${showProc ? ' open' : ''}><summary>过程文字 ${proc.length} 段</summary>${proc.map(p => `<div class="md">${renderMD(p)}</div>`).join('')}</details>` : ''}
        <div class="md">${renderMD(main)}</div>
        ${sid && t.images?.length ? `<details class="proc imgsec"><summary>cc 查看的图片 ${t.images.length} 张</summary>${imgGrid(sid, t.images)}</details>` : ''}
        ${t.notes.map(n => `<div class="md">${renderMD(n)}</div>`).join('')}
        ${pending ? '<div class="pending">回答生成中…</div>' : ''}
      </div>` : ''}
    </section>`;
  }).join('');
}

function renderRaw(turns) {
  const showMisc = $('#show-misc').checked;
  $('#article').innerHTML = rawHTML(turns, state.data.active, $('#show-process').checked, showMisc, state.data.meta.id);
  renderMap(turns.filter(t => t.kind === 'question' && document.getElementById('t' + t.n))
    .map(t => ({ id: 't' + t.n, n: t.n, text: t.q.replace(/\s+/g, ' ').slice(0, 240), len: t.q.length })));
}

// ---------- 问题地图(左侧横条)----------
// 垂直居中,最多显示 MAP_MAX 根横条(以当前问题为中心的窗口,滚轮翻动);
// 平时横条一样长,指针移上去才按问题长短展开,并浮出附近几问的文字
const MAP_MAX = 15;
const qmap = { items: [], els: [], start: 0, cur: 0, hover: false, hideT: null };
function renderMap(items) {
  qmap.items = items;
  qmap.els = items.map(() => null);
  qmap.start = Math.max(0, items.length - MAP_MAX);
  qmap.cur = items.length - 1;
  drawMap();
  layoutMap();
  requestAnimationFrame(spy);
}
function drawMap() {
  const n = qmap.items.length, toc = $('#toc');
  if (!n) { toc.innerHTML = ''; return; }
  const lo = qmap.start, hi = Math.min(n, lo + MAP_MAX), maxLen = Math.log(800);
  const bars = qmap.items.slice(lo, hi).map((it, k) => {
    const i = lo + k, w = 22 + 78 * Math.min(1, Math.log(Math.max(it.len, 3)) / maxLen);
    return `<a class="qi${it.lv === 3 ? ' lv3' : ''}${i === qmap.cur ? ' on' : ''}" data-i="${i}" style="--w:${w.toFixed(0)}%"><span class="bar"></span></a>`;
  }).join('');
  toc.innerHTML = `<div class="more" data-step="-${MAP_MAX}"${lo ? '' : ' hidden'}>↑ ${lo}</div>${bars}` +
    `<div class="more" data-step="${MAP_MAX}"${hi < n ? '' : ' hidden'}>↓ ${n - hi}</div>`;
}
function shiftMap(d) {
  const s = Math.max(0, Math.min(qmap.items.length - MAP_MAX, qmap.start + d));
  if (s !== qmap.start) { qmap.start = s; drawMap(); }
}
function mapTarget(i) {
  return qmap.els[i] ||= document.getElementById(qmap.items[i].id);
}
function layoutMap() {
  const toc = $('#toc');
  const top = ($('#top')?.offsetHeight || 80) + 8;
  $('#main').style.setProperty('--top-h', (top - 8) + 'px');
  const dockH = $('#view').classList.contains('file') || $('#view').classList.contains('stars') ? 0 : dock.offsetHeight;
  toc.style.setProperty('--toc-top', top + 'px');
  toc.style.setProperty('--toc-h', Math.max(160, window.innerHeight - top - dockH - 8) + 'px');
}
function spy() {
  if (!qmap.items.length) return;
  const line = ($('#top')?.getBoundingClientRect().bottom || 0) + 60;
  let cur = 0;
  for (let i = 0; i < qmap.items.length; i++) {
    const el = mapTarget(i);
    if (el && el.getBoundingClientRect().top <= line) cur = i;
  }
  const changed = cur !== qmap.cur;
  qmap.cur = cur;
  if (!qmap.hover) {  // 指针不在地图上时,窗口跟着当前问题走
    const s = Math.max(0, Math.min(qmap.items.length - MAP_MAX, cur - Math.floor(MAP_MAX / 2)));
    if (s !== qmap.start) { qmap.start = s; drawMap(); return; }
  }
  if (changed) $('#toc').querySelectorAll('.qi').forEach(b => b.classList.toggle('on', +b.dataset.i === cur));
}
function showPop(i, y) {
  clearTimeout(qmap.hideT);
  const pop = $('#qpop');
  const lo = Math.max(0, i - 3), hi = Math.min(qmap.items.length, i + 4);
  pop.innerHTML = qmap.items.slice(lo, hi).map((it, k) => {
    const j = lo + k;
    return `<a data-i="${j}" class="${j === i ? 'hot' : ''}${j === qmap.cur ? ' on' : ''}"><span class="n">${it.n ?? ''}</span>${esc(it.text)}</a>`;
  }).join('');
  pop.hidden = false;
  const r = $('#toc').getBoundingClientRect();
  pop.style.left = (r.right + 8) + 'px';
  pop.style.top = Math.max(8, Math.min(window.innerHeight - pop.offsetHeight - 8, y - pop.offsetHeight / 2)) + 'px';
  $('#toc').querySelectorAll('.qi').forEach(b => b.classList.toggle('hot', +b.dataset.i === i));
}
function hidePop() {
  qmap.hideT = setTimeout(() => {
    $('#qpop').hidden = true; qmap.hover = false;
    $('#toc').querySelectorAll('.qi').forEach(b => b.classList.remove('hot'));
    spy();
  }, 200);
}
function goMap(i) {
  mapTarget(i)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}
$('#toc').addEventListener('mousemove', e => {
  qmap.hover = true;
  const b = e.target.closest('.qi');
  if (b) showPop(+b.dataset.i, e.clientY);
});
$('#toc').addEventListener('mouseleave', hidePop);
$('#toc').addEventListener('wheel', e => {  // 滚轮翻动地图窗口,不滚动页面
  if (qmap.items.length <= MAP_MAX) return;
  e.preventDefault();
  qmap.hover = true;
  shiftMap(e.deltaY > 0 ? 2 : -2);
  const b = document.elementFromPoint(e.clientX, e.clientY)?.closest('.qi');
  if (b) showPop(+b.dataset.i, e.clientY); else $('#qpop').hidden = true;
}, { passive: false });
$('#qpop').addEventListener('mouseenter', () => clearTimeout(qmap.hideT));
$('#qpop').addEventListener('mouseleave', hidePop);
$('#toc').addEventListener('click', e => {
  const m = e.target.closest('.more');
  if (m) { qmap.hover = true; shiftMap(+m.dataset.step); return; }
  const b = e.target.closest('.qi');
  if (b) goMap(+b.dataset.i);
});
$('#qpop').addEventListener('click', e => { const a = e.target.closest('a'); if (a) { goMap(+a.dataset.i); $('#qpop').hidden = true; } });
let spyQueued = false;
$('#main').addEventListener('scroll', () => {
  if (spyQueued) return;
  spyQueued = true;
  requestAnimationFrame(() => { spyQueued = false; spy(); });
});
window.addEventListener('resize', () => { layoutMap(); spy(); });

// ---------- 底部输入区(真实的 cc TUI)----------
// 会话页底部嵌入运行这个会话的 tmux 终端;输入框、斜杠菜单、权限确认、Esc 打断等与终端完全一致。
const tv = { name: null, term: null, fit: null, ws: null, retry: null, ro: null, terms: [], bySid: new Map() };
const dock = $('#dock');

function onTerms(terms) {
  if (!terms) return;
  tv.terms = terms;
  tv.bySid = new Map();
  // 终端当前所在的会话:有记录后用 sid(/clear 后会变),还没提问时用启动时指定的 launch_sid
  for (const t of terms) { const k = t.sid || t.launch_sid; if (k) tv.bySid.set(k, t); }
  // 底部终端里 /clear 开了新会话 → 页面跟过去
  if (tv.name && state.sid) {
    const t = terms.find(t => t.name === tv.name);
    if (t?.sid && t.sid !== state.sid) location.replace('#/s/' + t.sid);
  }
  syncDock();
}

async function loadTerms() {
  const r = await fetch('/api/terms').then(r => r.json()).catch(() => null);
  if (r) onTerms(r.terms);
}

function syncDock() {
  requestAnimationFrame(layoutMap);
  if (!state.data || $('#view').classList.contains('stars') || $('#view').classList.contains('file')) return detachTerm();
  const t = tv.bySid.get(state.data.meta.id);
  if (t) {
    dock.classList.add('on');
    $('#dock-status').innerHTML = `<span class="badge run">运行中</span> ${esc(t.name)} · ${t.clients} 个连接 · 点击终端后直接输入`;
    if (tv.name !== t.name) attachTerm(t.name);
  } else {
    detachTerm();
    dock.classList.remove('on');
    const busyElsewhere = state.data.active && !state.data.pending;
    $('#dock-go').disabled = busyElsewhere;
    $('#dock-status').textContent = '未运行';
    $('#dock-tip').innerHTML = busyElsewhere
      ? '这个会话正在别的终端里运行(不是用 cc 启动的)。为避免两个进程同时写同一会话,请在原终端继续,或退出它后再从这里继续。'
      : '将以 <code>--resume</code> 在后台启动(bypass 模式),底部出现与终端完全相同的输入框。';
  }
}

function fitTerm() {
  if (!tv.term || $('#termbox').offsetWidth === 0) return;
  try { tv.fit.fit(); } catch {}
  if (tv.ws?.readyState === 1) tv.ws.send('1' + JSON.stringify({ cols: tv.term.cols, rows: tv.term.rows }));
}

function detachTerm() {
  clearTimeout(tv.retry);
  if (tv.ws) { tv.ws.onclose = null; tv.ws.close(); }
  tv.ro?.disconnect();
  tv.term?.dispose();
  $('#termbox').querySelectorAll('.xterm').forEach(x => x.remove());
  Object.assign(tv, { name: null, term: null, ws: null, ro: null });
}

// 先等内置等宽字体加载完再建终端,否则 xterm 会按后备字体测量字宽,导致字间距错乱
const monoReady = Promise.race([document.fonts.load('14px "CCNB Mono"'), new Promise(r => setTimeout(r, 1500))]).catch(() => {});

async function attachTerm(name) {
  detachTerm();
  tv.name = name;
  await monoReady;
  if (tv.name !== name || tv.term) return;
  tv.term = new Terminal({
    fontFamily: '"CCNB Mono", Menlo, Consolas, "DejaVu Sans Mono", "Noto Sans Mono CJK SC", "Sarasa Mono SC", monospace',
    fontSize: 14, cursorBlink: true, allowProposedApi: true, scrollback: 1000, theme: { background: '#1e1e1e' },
  });
  tv.fit = new FitAddon.FitAddon();
  tv.term.loadAddon(tv.fit);
  tv.term.open($('#termbox'));
  tv.term.onData(d => { if (tv.ws?.readyState === 1) tv.ws.send('0' + d); });
  tv.ro = new ResizeObserver(() => fitTerm());
  tv.ro.observe($('#termbox'));
  connectTerm();
}

function connectTerm() {
  const name = tv.name;
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/term/${name}`);
  ws.binaryType = 'arraybuffer';
  tv.ws = ws;
  ws.onopen = () => { $('#term-overlay').hidden = true; fitTerm(); };
  ws.onmessage = e => tv.term.write(new Uint8Array(e.data));
  ws.onclose = async () => {
    if (tv.name !== name) return;
    await loadTerms();
    if (tv.name !== name) return;  // 会话已结束,syncDock 已切回"继续"按钮
    const ov = $('#term-overlay');
    ov.hidden = false;
    ov.textContent = '连接断开,正在重连…(点击立即重连)';
    tv.retry = setTimeout(connectTerm, 2000);
  };
}

// 高度:拖动标题栏调整,记住;"放大"在常用高度与 72vh 之间切换
function setDockH(px) {
  px = Math.max(120, Math.min(window.innerHeight - 160, px));
  dock.style.setProperty('--dock-h', px + 'px');
  localSet('dockH', String(px));
  requestAnimationFrame(layoutMap);
}
setDockH(+localGet('dockH', '300'));
$('#dock-bar').addEventListener('mousedown', e => {
  if (e.target.closest('button')) return;
  e.preventDefault();
  dock.classList.remove('max'); $('#dock-max').textContent = '放大';
  const move = ev => setDockH(window.innerHeight - ev.clientY - $('#dock-bar').offsetHeight);
  const up = () => { removeEventListener('mousemove', move); removeEventListener('mouseup', up); fitTerm(); };
  addEventListener('mousemove', move); addEventListener('mouseup', up);
});
$('#dock-max').addEventListener('click', () => {
  const max = dock.classList.toggle('max');
  $('#dock-max').textContent = max ? '还原' : '放大';
  setTimeout(() => { fitTerm(); layoutMap(); tv.term?.focus(); }, 0);
});
$('#termbox').addEventListener('click', () => tv.term?.focus());
$('#term-overlay').addEventListener('click', () => { clearTimeout(tv.retry); if (tv.name) connectTerm(); });

$('#dock-go').addEventListener('click', async () => {
  if (!state.data) return;
  const b = $('#dock-go');
  b.disabled = true; b.textContent = '启动中…';
  const r = await fetch(`/api/sessions/${state.data.meta.id}/continue`, { method: 'POST' })
    .then(r => r.json()).catch(() => ({ error: '网络错误' }));
  b.disabled = false; b.textContent = '▶ 在此继续对话';
  if (r.error) return alert(r.error);
  await loadTerms();
  setTimeout(() => tv.term?.focus(), 300);
});
$('#dock-kill').addEventListener('click', async () => {
  if (!tv.name || !confirm(`结束 ${tv.name}?其中的 cc 会被关闭(对话记录保留,可随时"在此继续")。`)) return;
  await fetch(`/api/terms/${tv.name}/kill`, { method: 'POST' });
  detachTerm();
  await loadSessions();
  if (state.data?.pending) location.hash = '';
});

// ---------- 只读文件浏览 ----------
const fs = { dir: localGet('fsDir', ''), file: null };
const fmtSize = n => n == null ? '' : n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB' : (n / 1048576).toFixed(1) + ' MB';
const dirname = p => p.replace(/\/[^/]*$/, '') || '/';
const fileIcon = (e) => e.dir ? '▸' : /\.(png|jpe?g|gif|webp|bmp|avif|svg)$/i.test(e.name) ? '◩' : /\.pdf$/i.test(e.name) ? '▤'
  : /\.(md|markdown|txt)$/i.test(e.name) ? '≡' : '·';

function setSide(which) {
  localSet('side-tab', which);
  document.querySelectorAll('.side-tabs button').forEach(b => b.classList.toggle('on', b.dataset.side === which));
  $('#side-sessions').hidden = which !== 'sessions';
  $('#side-files').hidden = which !== 'files';
  // 优先显示当前上下文:正在预览的文件所在目录 > 当前会话目录 > 上次浏览的目录
  if (which === 'files') {
    const want = !$('#view').hidden && $('#view').classList.contains('file') && fs.file ? dirname(fs.file)
      : state.data?.meta.cwd || fs.dir || '~';
    if (want !== fs.dir || !$('#fs-list').childElementCount) loadDir(want);
  }
}
document.querySelectorAll('.side-tabs button').forEach(b => b.addEventListener('click', () => setSide(b.dataset.side)));

async function loadDir(path) {
  const r = await fetch(`/api/fs/list?path=${encodeURIComponent(path)}&hidden=${$('#fs-hidden').checked ? 1 : 0}`)
    .then(r => r.json()).catch(() => ({ error: '网络错误' }));
  if (r.error) { toast(r.error); return; }
  fs.dir = r.path; localSet('fsDir', r.path);
  const parts = r.path.split('/').filter(Boolean);
  $('#fs-path').innerHTML = '<a data-p="/">/</a>' + parts.map((x, i) => `<a data-p="/${esc(parts.slice(0, i + 1).join('/'))}">${esc(x)}</a>`).join('/');
  $('#fs-up').disabled = !r.parent;
  $('#fs-up').dataset.p = r.parent || '';
  $('#fs-list').innerHTML = r.entries.map(e => {
    const full = r.path.replace(/\/$/, '') + '/' + e.name;
    return e.dir
      ? `<a class="fe" data-p="${esc(full)}"><span class="ic">${fileIcon(e)}</span><span class="nm">${esc(e.name)}</span></a>`
      : `<a class="fe${full === fs.file ? ' on' : ''}" href="#/f/${encodeURIComponent(full)}"><span class="ic">${fileIcon(e)}</span><span class="nm">${esc(e.name)}</span><span class="sz">${fmtSize(e.size)}</span></a>`;
  }).join('') + (r.truncated ? '<div class="hint">(只显示前 3000 项)</div>' : '') || '<div class="hint">空目录</div>';
}
$('#side-files').addEventListener('click', e => {
  const a = e.target.closest('[data-p]');
  if (a && a.dataset.p) { e.preventDefault(); loadDir(a.dataset.p); }
});
$('#fs-home').addEventListener('click', () => loadDir(state.data?.meta.cwd || fs.lastCwd || '~'));
$('#fs-hidden').addEventListener('change', () => loadDir(fs.dir || '~'));

async function openFile(path, line) {
  state.sid = null; state.data = null; state.version = null;
  detachTerm();
  const r = await fetch(`/api/fs/read?path=${encodeURIComponent(path)}`).then(r => r.json()).catch(() => ({ error: '网络错误' }));
  $('#empty').hidden = true; $('#view').hidden = false;
  $('#view').classList.remove('stars'); $('#view').classList.add('file'); $('#star-link').classList.remove('on');
  renderSessions();
  if (r.error) {
    $('#title').textContent = path.split('/').pop(); $('#sub').innerHTML = `<span>${esc(path)}</span>`;
    $('#article').innerHTML = `<div class="hint">${esc(r.error)}</div>`; renderMap([]); return;
  }
  if (r.kind === 'dir') { setSide('files'); loadDir(r.path); history.back(); return; }
  fs.file = r.path;
  const raw = `/api/fs/raw?path=${encodeURIComponent(r.path)}`;
  $('#title').textContent = r.name;
  document.title = r.name + ' · MeowNotebook';
  $('#sub').innerHTML = `<span>${esc(r.path)}</span><span>${fmtSize(r.size)}</span><span>${fmtTime(r.mtime)}</span><span>只读</span>`;
  $('#f-raw').href = raw; $('#f-dl').href = raw + '&dl=1';
  const art = $('#article');
  mdCwd = dirname(r.path);
  if (r.kind === 'markdown' && !line) {  // 带行号的链接指向源码行,显示源码
    art.innerHTML = `<div class="md">${renderMD(r.text)}</div>`;
    const hs = [...art.querySelectorAll('.md h1, .md h2, .md h3')];
    hs.forEach((h, i) => h.id = 'h' + i);
    renderMap(hs.map(h => ({ id: h.id, text: h.textContent, len: h.textContent.length * 3, lv: h.tagName === 'H3' ? 3 : 2 })));
  } else if (r.kind === 'text' || r.kind === 'markdown') {
    const lines = r.text.split('\n');
    let body = esc(r.text);
    try {
      if (r.text.length < 400000) body = r.lang && hljs.getLanguage(r.lang)
        ? hljs.highlight(r.text, { language: r.lang, ignoreIllegals: true }).value : body;
    } catch {}
    art.innerHTML = `<div class="code-wrap"><pre class="gutter">${lines.map((_, i) => i + 1).join('\n')}</pre><pre class="src"><code class="hljs">${body}</code></pre></div>`;
    renderMap([]);
    if (line) {
      const wrap = art.querySelector('.code-wrap'), lh = parseFloat(getComputedStyle(wrap).lineHeight) || 20;
      const mark = document.createElement('div');
      mark.className = 'mark'; mark.style.top = (10 + (line - 1) * lh) + 'px';
      wrap.appendChild(mark);
      requestAnimationFrame(() => mark.scrollIntoView({ block: 'center' }));
    }
  } else if (r.kind === 'image') {
    art.innerHTML = `<img class="fview-img" src="${raw}" alt="${esc(r.name)}">`; renderMap([]);
  } else if (r.kind === 'pdf') {
    art.innerHTML = `<iframe class="fview-pdf" src="${raw}"></iframe>`; renderMap([]);
  } else {
    art.innerHTML = `<div class="hint">${r.kind === 'large' ? '文件超过 2 MB,不在页面里预览。' : '二进制文件,无法预览。'}<br>可以用右上角"下载"。</div>`; renderMap([]);
  }
  if (!line) $('#main').scrollTop = 0;
  if (!$('#side-files').hidden && dirname(r.path) !== fs.dir) loadDir(dirname(r.path));
  else $('#fs-list').querySelectorAll('.fe').forEach(a => a.classList.toggle('on', a.getAttribute('href') === '#/f/' + encodeURIComponent(r.path)));
}

// ---------- 收藏 ----------
const starKey = (sid, ts) => sid + '|' + ts;
function starBtn(sid, ts) {
  const on = state.starKeys.has(starKey(sid, ts));
  return `<button class="star${on ? ' on' : ''}" data-sid="${sid}" data-ts="${esc(ts)}" title="${on ? '取消收藏' : '收藏这条问答'}">${on ? '★' : '☆'}</button>`;
}
function setStars(list) {
  state.stars = list;
  state.starKeys = new Set(list.map(x => starKey(x.sid, x.ts)));
  $('#star-count').textContent = list.length ? `(${list.length})` : '';
}
async function loadStars() {
  const r = await fetch('/api/stars').then(r => r.json()).catch(() => null);
  if (r) setStars(r.stars);
}
document.addEventListener('click', async e => {
  const b = e.target.closest('.star');
  if (!b) return;
  e.preventDefault();
  const on = !b.classList.contains('on');
  const r = await fetch('/api/stars', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sid: b.dataset.sid, ts: b.dataset.ts, on }) }).then(r => r.json()).catch(() => null);
  if (!r || r.error) return toast(r?.error || '操作失败');
  setStars(r.stars);
  document.querySelectorAll(`.star[data-sid="${b.dataset.sid}"][data-ts="${CSS.escape(b.dataset.ts)}"]`).forEach(x => x.outerHTML = starBtn(x.dataset.sid, x.dataset.ts));
  toast(on ? '已收藏' : '已取消收藏');
  if (location.hash === '#/stars' && !on) renderStars();
});

function renderStars() {
  $('#empty').hidden = true; $('#view').hidden = false;
  $('#view').classList.remove('file'); $('#view').classList.add('stars'); detachTerm(); $('#star-link').classList.add('on');
  $('#title').textContent = '★ 收藏';
  document.title = '收藏 · MeowNotebook';
  $('#sub').innerHTML = `<span>${state.stars.length} 条</span><span>存放在本地 data/stars.json(含问答快照)</span>`;
  $('#article').innerHTML = state.stars.map((x, i) => (mdCwd = x.cwd || '', '') + `<section class="starcard turn" id="star${i}">
      <div class="src"><a href="#/s/${x.sid}/${x.n}">${esc(x.title)} · 第 ${x.n} 轮</a><span>${fmtTime(x.ts)}</span><span class="sp"></span>${starBtn(x.sid, x.ts)}</div>
      <div class="q question"><div class="md">${renderMD(x.q)}</div></div>
      <div class="a"><div class="md">${renderMD(x.a)}</div></div>
    </section>`).join('') || '<div class="hint">还没有收藏。在问答页每个问题右上角点 ☆ 即可收藏。</div>';
  renderMap(state.stars.map((x, i) => ({ id: 'star' + i, n: i + 1, text: x.q.replace(/\s+/g, ' ').slice(0, 240), len: x.q.length })));
}

// ---------- 交互 ----------
document.querySelectorAll('#tabs [data-tab]').forEach(b => b.addEventListener('click', () => {
  state.tab = b.dataset.tab; localSet('tab', state.tab); renderSession();
}));
['#show-process', '#show-misc'].forEach(id => $(id).addEventListener('change', renderSession));

document.querySelectorAll('[data-copy]').forEach(b => b.addEventListener('click', async () => {
  const m = state.data?.meta; if (!m) return;
  const text = b.dataset.copy === 'resume' ? `cd ${m.cwd} && cc --resume ${m.id}` : `/qa-notes ${m.id}`;
  try { await navigator.clipboard.writeText(text); toast('已复制: ' + text); }
  catch { prompt('复制下面的命令', text); }
}));
$('#toggle-side').addEventListener('click', () => { document.body.classList.add('side-hidden'); localSet('side', '0'); setTimeout(fitTerm, 0); });
document.querySelectorAll('.show-side').forEach(b => b.addEventListener('click', () => { document.body.classList.remove('side-hidden'); localSet('side', '1'); setTimeout(fitTerm, 0); }));
if (localGet('side', '1') === '0') document.body.classList.add('side-hidden');

function route() {
  const t = location.hash.match(/^#\/t\/([\w-]+)/);  // 旧链接:终端名 → 对应会话
  if (t) {
    const term = tv.terms.find(x => x.name === t[1]);
    location.replace(term ? '#/s/' + (term.sid || term.launch_sid) : '#');
    return;
  }
  const f = location.hash.match(/^#\/f\/([^@]*)(?:@(\d+))?$/);
  if (f) {
    let path;
    try { path = decodeURIComponent(f[1]); } catch { location.replace('#'); return; }
    openFile(path, f[2] ? +f[2] : null); return;
  }
  if (location.hash === '#/stars') {
    state.sid = null; state.data = null; renderSessions(); loadStars().then(renderStars); return;
  }
  const m = location.hash.match(/^#\/s\/([\w-]+)(?:\/(\d+))?/);
  const sid = m ? m[1] : null;
  if (m?.[2]) state.jump = +m[2];
  if (sid && sid === state.sid && state.data && state.jump) { state.tab = 'raw'; renderSession(); jumpToTurn(); return; }
  if (sid === state.sid && (sid || !$('#empty').hidden) && !$('#view').classList.contains('file')) return;
  state.sid = sid; state.data = null; state.version = null;
  if (!sid) { $('#view').hidden = true; $('#empty').hidden = false; detachTerm(); }
  $('#main').scrollTop = 0;
  renderSessions();
  loadSession(true);
}
window.addEventListener('hashchange', route);

// 进行中的会话每 3 秒检查一次;列表每 15 秒刷新
// 记录在更新(或底部终端在运行)时每 3 秒刷新问答;终端状态每 3 秒、会话列表每 15 秒刷新
setInterval(() => { if (!document.hidden && (state.data?.active || tv.name)) loadSession(); }, 3000);
setInterval(() => { if (!document.hidden) loadTerms(); }, 3000);
setInterval(() => { if (!document.hidden) { loadSessions(); if (state.data && !state.data.active) loadSession(); } }, 15000);
setSide(localGet('side-tab', 'sessions'));
Promise.all([loadSessions(), loadStars()]).then(route);
