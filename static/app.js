// MeowNotebook 前端:会话列表、问答渲染(Markdown + KaTeX)、进行中会话自动刷新
const $ = (s, el = document) => el.querySelector(s);
const state = {
  sessions: [], sid: null, data: null, version: null, jump: null, stars: [], starKeys: new Set(),
  tab: localGet('tab', 'note'), collapsed: new Set(JSON.parse(localGet('collapsed', '[]'))),
};

function localGet(k, d) { try { return localStorage.getItem('ccnb.' + k) ?? d; } catch { return d; } }
function localSet(k, v) { try { localStorage.setItem('ccnb.' + k, v); } catch {} }
function esc(s) { return (s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
function toast(msg, ms = 1400) {
  const t = $('#toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toast.timer); toast.timer = setTimeout(() => t.classList.remove('show'), ms);
}
// 请求带超时:网络差时请求可能挂很久,定时刷新会一个个叠起来;连续失败时右下角提示网络不稳定
const net = { fails: 0 };
async function api(url, opts = {}, ms = 15000) {
  const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), ms);
  try {
    const r = await (await fetch(url, { ...opts, signal: ctl.signal })).json();
    netState(true);
    return r;
  } catch (e) {
    if (e.name !== 'SyntaxError') netState(false);
    throw e;
  } finally { clearTimeout(timer); }
}
function netState(ok) {
  net.fails = ok ? 0 : net.fails + 1;
  $('#net').hidden = net.fails < 2;
}
const post = (url, body) => api(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body || {}) }, 60000);

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
      return r ? `<pre><code class="hljs${lang ? ' language-' + lang : ''}">${r.value}</code></pre>` : m;
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

// ---------- 复制为 Markdown ----------
// 正文里选中后右键:复制为 Markdown(表格、公式 $…$、代码块、列表、加粗等都还原成源码,
// 粘到 Markdown 编辑器里能直接渲染)/ 纯文本 / 富文本。Ctrl+C 保持浏览器默认行为。
const MD_SKIP = 'button, summary, script, style, .lab, .pending, .katex-mathml';
const mdBlock = s => s.trim() ? `\n\n${s.trim()}\n\n` : '';
const mdKids = (el, ctx) => [...el.childNodes].map(n => toMD(n, ctx)).join('');
const mdTex = k => k.querySelector('annotation[encoding="application/x-tex"]')?.textContent.trim() ?? k.textContent;
function mdWrap(inner, w) {
  const m = inner.match(/^(\s*)([\s\S]*?)(\s*)$/);
  return m[2] ? m[1] + w + m[2] + w + m[3] : inner;
}
function mdTable(t) {
  const trs = [...t.querySelectorAll('tr')];
  const rows = trs.map(tr => [...tr.children].map(c =>
    mdKids(c, { cell: true }).trim().replace(/\s*\n\s*/g, '<br>').replace(/(?<!\\)\|/g, '\\|')));
  if (!rows.length) return '';
  const w = Math.max(...rows.map(r => r.length));
  const align = [...Array(w)].map((_, i) => {
    const c = trs[0].children[i], a = c && (c.style.textAlign || c.getAttribute('align'));
    return a === 'center' ? ':---:' : a === 'right' ? '---:' : a === 'left' ? ':---' : '---';
  });
  const line = r => '| ' + [...Array(w)].map((_, i) => r[i] ?? '').join(' | ') + ' |';
  return mdBlock([line(rows[0]), '| ' + align.join(' | ') + ' |', ...rows.slice(1).map(line)].join('\n'));
}
function mdList(el, ctx) {
  let i = +(el.getAttribute('start') || 1);
  return mdBlock([...el.children].filter(li => li.tagName === 'LI').map(li => {
    const mark = el.tagName === 'OL' ? `${i++}. ` : '- ';
    return mark + mdKids(li, ctx).trim().replace(/\n{2,}/g, '\n').replace(/\n/g, '\n' + ' '.repeat(mark.length));
  }).join('\n'));
}
function toMD(n, ctx = {}) {
  if (n.nodeType === 3) return /^\s*\n\s*$/.test(n.nodeValue) ? '' : n.nodeValue.replace(/\s*\n\s*/g, ' ');
  if (n.nodeType === 11) return mdKids(n, ctx);
  if (n.nodeType !== 1 || n.matches(MD_SKIP)) return '';
  if (n.classList.contains('katex-display')) {
    const k = n.querySelector('.katex');
    return !k ? '' : ctx.cell ? `$$${mdTex(k)}$$` : mdBlock(`$$\n${mdTex(k)}\n$$`);
  }
  if (n.classList.contains('katex')) return `$${mdTex(n)}$`;
  const tag = n.tagName.toLowerCase(), kids = () => mdKids(n, ctx);
  switch (tag) {
    case 'h1': case 'h2': case 'h3': case 'h4': case 'h5': case 'h6':
      return mdBlock('#'.repeat(+tag[1]) + ' ' + kids().trim());
    case 'p': case 'div': case 'section': case 'article': case 'details': return mdBlock(kids());
    case 'pre': {
      const code = n.textContent.replace(/\n$/, '');
      const lang = n.querySelector('code')?.className.match(/language-([\w+#-]+)/)?.[1] || '';
      let f = '```'; while (code.includes(f)) f += '`';
      return mdBlock(`${f}${lang}\n${code}\n${f}`);
    }
    case 'code': { const t = n.textContent; return t.includes('`') ? '`` ' + t + ' ``' : '`' + t + '`'; }
    case 'strong': case 'b': return mdWrap(kids(), '**');
    case 'em': case 'i': return mdWrap(kids(), '*');
    case 'del': case 's': return mdWrap(kids(), '~~');
    case 'a': {
      const href = n.getAttribute('href') || '';
      return !href || href.startsWith('#') || n.querySelector('img') ? kids() : `[${kids()}](${href})`;
    }
    case 'img': {
      let src = n.getAttribute('src') || '';
      src = src.startsWith('/api/file?') ? new URLSearchParams(src.split('?')[1]).get('path') : new URL(src, location.href).href;
      return `![${n.getAttribute('alt') || ''}](${src})`;
    }
    case 'input': return n.type === 'checkbox' ? (n.checked ? '[x] ' : '[ ] ') : '';
    case 'br': return '\n';
    case 'hr': return mdBlock('---');
    case 'blockquote': return mdBlock(kids().trim().split('\n').map(l => l ? '> ' + l : '>').join('\n'));
    case 'ul': case 'ol': return mdList(n, ctx);
    case 'table': return mdTable(n);
    default: return kids();
  }
}
// 当前选区克隆成 DOM(公式扩成整个、跨格选表格取整行并补表头);不在正文里返回 null
function selectionBox() {
  const sel = getSelection();
  if (!sel.rangeCount || sel.isCollapsed) return null;
  const elOf = n => n.nodeType === 1 ? n : n.parentElement;
  const range = sel.getRangeAt(0).cloneRange();
  const anc = elOf(range.commonAncestorContainer);
  if (!$('#article').contains(anc)) return null;
  const kx = n => { const k = elOf(n)?.closest('.katex'); return k?.closest('.katex-display') || k; };
  const ks = kx(range.startContainer), ke = kx(range.endContainer);
  if (ks) range.setStartBefore(ks);
  if (ke) range.setEndAfter(ke);
  const box = document.createElement('div');
  const tbl = anc.closest('table');
  if (tbl && !anc.closest('td, th')) {
    const t = document.createElement('table');
    [...tbl.rows].filter((r, i) => i === 0 || range.intersectsNode(r)).forEach(r => t.appendChild(r.cloneNode(true)));
    box.appendChild(t);
  } else box.appendChild(range.cloneContents());
  return box;
}

// 右键菜单选好格式后借 execCommand('copy') 写剪贴板:非 https 访问时 navigator.clipboard 不可用,这条路都能用
let copyAs = null;
document.addEventListener('copy', e => {
  if (!copyAs) return;
  for (const [type, data] of Object.entries(copyAs)) e.clipboardData.setData(type, data);
  copyAs = null; e.preventDefault();
});
function copyData(data, label) {
  copyAs = data;
  const ok = document.execCommand('copy');
  copyAs = null;
  toast(ok ? '已复制为 ' + label : '复制失败');
}

const cmenu = document.createElement('div');
cmenu.id = 'cmenu'; cmenu.hidden = true;
cmenu.innerHTML = '<button data-as="md">复制为 Markdown</button><button data-as="text">复制为纯文本</button><button data-as="html">复制为富文本</button>';
document.body.appendChild(cmenu);
cmenu.addEventListener('mousedown', e => e.preventDefault());  // 点菜单时保住选区
cmenu.addEventListener('click', e => {
  const as = e.target.dataset?.as; if (!as) return;
  cmenu.hidden = true;
  const box = selectionBox(); if (!box) return;
  if (as === 'md') copyData({ 'text/plain': toMD(box).replace(/\n{3,}/g, '\n\n').trim() }, 'Markdown');
  else if (as === 'text') copyData({ 'text/plain': getSelection().toString() }, '纯文本');
  else copyData({ 'text/html': box.innerHTML, 'text/plain': getSelection().toString() }, '富文本');
});
document.addEventListener('contextmenu', e => {
  cmenu.hidden = true;
  if (e.shiftKey || !$('#article').contains(e.target) || !selectionBox()) return;  // Shift+右键 = 浏览器原生菜单
  e.preventDefault();
  cmenu.hidden = false;
  const { innerWidth: W, innerHeight: H } = window, r = cmenu.getBoundingClientRect();
  cmenu.style.left = Math.min(e.clientX, W - r.width - 4) + 'px';
  cmenu.style.top = Math.min(e.clientY, H - r.height - 4) + 'px';
});
document.addEventListener('mousedown', e => { if (!cmenu.contains(e.target)) cmenu.hidden = true; });
document.addEventListener('keydown', e => { if (e.key === 'Escape') cmenu.hidden = true; });
$('#main').addEventListener('scroll', () => { cmenu.hidden = true; });

// ---------- 会话列表 ----------
async function loadSessions() {
  const r = await api('/api/sessions').catch(() => null);
  if (!r) return;
  state.sessions = r.sessions;
  renderQQ(r.qq || {});
  onTerms(r.terms);
  renderSessions();
  if (state.data) renderSendNote();
}

function renderSessions() {
  const kw = $('#search').value.trim().toLowerCase();
  if (kw && searchMode() !== 'title') return renderSearch();
  renderSrcFilter();
  const groups = new Map();
  for (const s of state.sessions) {
    if (state.src !== 'all' && (s.via?.platform || 'local') !== state.src) continue;
    if (kw && !`${s.title} ${s.first_q} ${s.cwd} ${s.id} ${s.via?.label || ''}`.toLowerCase().includes(kw)) continue;
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
      `<span class="pn"><span class="pp">${esc(name.slice(0, cut))}</span>${esc(name.slice(cut) || name)}</span>` +
      (cwd.startsWith('/') ? '<button class="proj-new" title="cc in this project">new</button>' : '') +
      `${live}<span class="cnt">${list.length}</span></div><div class="proj-list">` +
      list.map(s => `<a class="sess${s.id === state.sid ? ' on' : ''}" href="#/s/${s.id}">
        <div class="t">${esc(s.title)}</div>
        <div class="m">${s.running ? '<span class="badge run">运行中</span>' : ''}${s.via ? `<span class="badge via" title="在 ${esc(s.via.label)} 里开的会话">${esc(s.via.label)}</span>` : ''}${s.active ? '<span class="dot live" title="记录正在更新"></span>' : ''}${fmtTime(s.mtime)} · ${s.questions} 问
        ${s.noted ? '<span class="badge">整理版</span>' : ''}</div></a>`).join('') + '</div>';
  }).join('');
  $('#sessions').innerHTML = html || '<div class="hint">没有匹配的会话</div>';
}

// 来源筛选:有在 QQ 里开的会话时才显示
state.src = localGet('src', 'all');
function renderSrcFilter() {
  const plats = new Map(state.sessions.filter(s => s.via).map(s => [s.via.platform, s.via.label]));
  const box = $('#src-filter');
  box.hidden = !plats.size;
  if (!plats.size) { state.src = 'all'; return; }
  if (state.src !== 'all' && state.src !== 'local' && !plats.has(state.src)) state.src = 'all';
  box.innerHTML = [['all', '全部'], ['local', '本机'], ...plats].map(([k, l]) =>
    `<button data-src="${esc(k)}" class="${k === state.src ? 'on' : ''}">${esc(l)}</button>`).join('');
}
$('#src-filter').addEventListener('click', e => {
  const b = e.target.closest('[data-src]'); if (!b) return;
  state.src = b.dataset.src; localSet('src', state.src); renderSessions();
});

$('#sessions').addEventListener('click', async e => {
  const nb = e.target.closest('.proj-new');
  if (nb) {  // 在这个项目目录新开一个 cc,打开它的会话页,底部就是它的输入框
    e.stopPropagation();
    nb.disabled = true;
    const r = await post('/api/terms/new', { cwd: nb.closest('.proj').dataset.cwd }).catch(() => ({ error: '网络错误' }));
    nb.disabled = false;
    if (r.error) return toast(r.error);
    await loadTerms();
    if (r.sid) location.hash = '#/s/' + r.sid;
    loadSessions();
    return;
  }
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
function markArticle(root = $('#article')) {
  const words = searchWords();
  if (!words.length) return;
  const re = new RegExp(words.map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'gi');
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
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
  root.querySelectorAll('details.proc').forEach(d => { if (d.querySelector('mark')) d.open = true; });
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
// 网络差时请求会重叠、乱序返回:晚到的旧回复若合并到新数据上,刚出现的轮次会消失、增量对不上。
// 所以每个请求编号,只应用比已应用的更新的回复;增量回复的基础版本不是手里的版本就重新要。
const sess = { seq: 0, applied: 0, inflight: 0 };
async function loadSession(force = false, poll = false) {
  if (!state.sid || (poll && sess.inflight)) return;  // 定时刷新:上一个请求还没回来就不叠加
  const sid = state.sid, seq = ++sess.seq;
  const v = force ? '' : (state.version || '');
  sess.inflight++;
  const r = await api(`/api/session/${sid}?v=${encodeURIComponent(v)}`).catch(() => null);
  sess.inflight--;
  if (!r || sid !== state.sid || seq < sess.applied) return;
  sess.applied = seq;
  if (state.data && !r.error && JSON.stringify(state.data.owner ?? null) !== JSON.stringify(r.owner ?? null)) {
    state.data.owner = r.owner; syncDock();
  }
  if (r.unchanged) return;
  if (r.delta && v !== (state.version || '')) return loadSession();
  // 这个会话已在新会话里继续(从 agent 视图重新进入时 cc 会换新 id)→ 跳到最新的那个
  if (r.moved) { location.replace('#/s/' + r.moved + (state.jump ? '/' + state.jump : '')); return; }
  if (r.error) { $('#article').innerHTML = `<div class="hint">${esc(r.error)}</div>`; return; }
  const atBottom = $('#main').scrollTop + $('#main').clientHeight > $('#main').scrollHeight - 80;
  const first = !state.data || state.data.meta.id !== r.meta.id;
  if (r.delta && !first && !state.data.pending) {
    // 增量:服务器只回了变化 / 新增的轮次(按编号 n,从 1 开始连续),合并后只重画这些轮
    const old = state.data.turns, turns = old.slice(0, r.count);
    for (const t of r.turns) turns[t.n - 1] = t;
    const redraw = new Set(r.turns.map(t => t.n));
    if (old.length) redraw.add(old[old.length - 1].n);  // 原来的最后一轮:"回答生成中…"可能要撤掉
    state.data = { ...state.data, meta: r.meta, active: r.active, turns };
    state.version = r.version;
    patchSession(redraw, old.length);
  } else {
    if (r.delta) return loadSession(true);  // 手里没有可合并的基础数据,要完整的
    state.data = r; state.version = r.version;
    if (first && (r.active || state.jump)) state.tab = 'raw';  // 进行中的会话、或指定了某一轮 → 原始问答
    renderSession();
  }
  if (first) jumpToTurn();
  else if (atBottom && r.active && state.tab === 'raw') $('#main').scrollTop = $('#main').scrollHeight;
}

function renderHead() {
  const { meta, turns, note, active } = state.data;
  mdCwd = meta.cwd || '';
  $('#empty').hidden = true; $('#view').hidden = false;
  $('#view').classList.remove('stars', 'file', 'summary'); $('#star-link').classList.remove('on'); $('#sum-link').classList.remove('on');
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
  renderSendNote();
  noteBtn.textContent = note ? `整理版${note.meta.covered_turns && +note.meta.covered_turns < turns.length ? `(覆盖到第 ${note.meta.covered_turns} 轮,共 ${turns.length})` : ''}` : '整理版(未整理)';
}

function renderSession() {
  const { turns, note } = state.data;
  renderHead();
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

// 增量更新:只重画 redraw 里的轮次,其余 DOM 原样保留(长会话不必每 3 秒整页重排 Markdown / 公式)
function patchSession(redraw, oldCount) {
  const { turns, note, active, meta } = state.data;
  renderHead();
  if ((note && state.tab === 'note') || state.data.pending) return;  // 整理版页面不受原始记录变化影响
  if (turns.length < oldCount || !$('#article').querySelector('.turn')) return renderSession();
  const showProc = $('#show-process').checked, showMisc = $('#show-misc').checked;
  const last = turns[turns.length - 1];
  for (const n of [...redraw].sort((a, b) => a - b)) {
    const t = turns[n - 1];
    const old = document.getElementById('t' + n);
    const html = t ? rawHTML([t], active, showProc, showMisc, meta.id, last) : '';
    if (!html) { old?.remove(); continue; }
    const tpl = document.createElement('template');
    tpl.innerHTML = html.trim();
    const el = tpl.content.firstElementChild;
    if (old) {
      // 保留用户展开过的"过程文字 / 图片"
      const opened = [...old.querySelectorAll('details')].map(d => d.open);
      el.querySelectorAll('details').forEach((d, i) => { if (opened[i]) d.open = true; });
      old.replaceWith(el);
    } else {
      // 插到编号比它小的最后一个已显示轮次后面(新轮次一般就是追加到末尾)
      const prev = [...$('#article').querySelectorAll('.turn')].reverse().find(x => +x.id.slice(1) < n);
      prev ? prev.after(el) : $('#article').prepend(el);
    }
    markArticle(el);
  }
  renderMap(turns.filter(t => t.kind === 'question' && document.getElementById('t' + t.n))
    .map(t => ({ id: 't' + t.n, n: t.n, text: t.q.replace(/\s+/g, ' ').slice(0, 240), len: t.q.length })));
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

function rawHTML(turns, active, showProc, showMisc, sid, last = turns[turns.length - 1]) {
  const label = { question: '问', command: '命令', shell: 'Shell', summary: '上下文压缩摘要' };
  const items = turns.filter(t => showMisc || t.kind === 'question' || t.kind === 'summary');
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
const tv = { name: null, term: null, fit: null, ws: null, retry: null, ro: null, terms: [], bySid: new Map(),
  cid: null, seq: 0, unacked: [] };
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
  const r = await api('/api/terms').catch(() => null);
  if (r) onTerms(r.terms);
}

function syncDock() {
  requestAnimationFrame(layoutMap);
  if (!state.data || $('#view').classList.contains('stars') || $('#view').classList.contains('file')) return detachTerm();
  const t = tv.bySid.get(state.data.meta.id);
  if (t) {
    dock.classList.add('on');
    $('#dock-status').innerHTML = `<span class="badge run">运行中</span> ${esc(t.name)} · ${t.clients} 个连接 · 点击终端后直接输入<span class="warn" id="input-wait"></span>`;
    inputStatus();
    if (tv.name !== t.name) attachTerm(t.name);
  } else {
    detachTerm();
    dock.classList.remove('on');
    // 服务器按 cc 登记的运行中进程判断(正在回答 QQ 消息的也认得出),不再看记录最近有没有写
    const owner = !state.data.pending && state.data.owner;
    $('#dock-go').disabled = !!owner;
    $('#dock-status').textContent = owner ? `在${owner.label}里运行中` : '未运行';
    if (owner) $('#dock-tip').textContent = owner.message;
    else $('#dock-tip').innerHTML = '将以 <code>--resume</code> 在后台启动(bypass 模式),底部出现与终端完全相同的输入框。';
  }
}

function fitTerm() {
  if (!tv.term || $('#termbox').offsetWidth === 0) return;
  try { tv.fit.fit(); } catch {}
  if (tv.ws?.readyState === 1) tv.ws.send('1' + JSON.stringify({ cols: tv.term.cols, rows: tv.term.rows }));
}

function detachTerm() {
  clearTimeout(tv.retry);
  if (tv.ws) retireWS(tv.ws);
  tv.ro?.disconnect();
  tv.term?.dispose();
  $('#termbox').querySelectorAll('.xterm').forEach(x => x.remove());
  Object.assign(tv, { name: null, term: null, ws: null, ro: null, unacked: [] });
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
  Object.assign(tv, { cid: crypto.randomUUID?.() || String(Math.random()).slice(2) + Date.now(), seq: 0, unacked: [] });
  tv.term.onData(sendInput);
  tv.ro = new ResizeObserver(() => fitTerm());
  tv.ro.observe($('#termbox'));
  connectTerm();
}

// 键盘输入带序号发送,服务器确认("a<序号>")后才算送达。网络差时连接可能悄悄断掉,
// 浏览器发送缓冲里的按键会跟着丢;没确认的输入一直留着,重连后补发(服务器按序号去重,不会重复输入)。
function sendInput(d) {
  const m = { seq: ++tv.seq, d, t: Date.now() };
  tv.unacked.push(m);
  const ws = tv.ws;
  if (ws?.readyState === 1 && ws.mode === 'ack') ws.send('2' + m.seq + ':' + d);
  else if (ws?.readyState === 1 && ws.mode === 'legacy') { ws.send('0' + d); tv.unacked.pop(); }
}

function inputStatus() {
  const el = $('#input-wait'); if (!el) return;
  const n = tv.unacked.reduce((a, m) => a + m.d.length, 0);
  const late = tv.unacked.length && Date.now() - tv.unacked[0].t > 1500;
  el.textContent = late ? ` · 网络慢,${n} 个字符等待送达…` : '';
}

function retireWS(ws) {
  ws.onclose = ws.onmessage = ws.onopen = null;
  try { ws.close(); } catch {}
}

function connectTerm() {
  const name = tv.name;
  if (tv.ws) retireWS(tv.ws);
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/term/${name}?cid=${tv.cid}`);
  ws.binaryType = 'arraybuffer';
  ws.mode = null;  // 'ack':服务器支持带确认的输入;'legacy':旧版服务器(还没重启),退回直接发送
  tv.ws = ws;
  ws.onopen = () => {
    $('#term-overlay').hidden = true; fitTerm();
    setTimeout(() => {
      if (ws.mode || tv.ws !== ws || ws.readyState !== 1) return;
      ws.mode = 'legacy';
      for (const m of tv.unacked.splice(0)) ws.send('0' + m.d);
    }, 5000);
  };
  ws.onmessage = e => {
    if (typeof e.data === 'string') {  // "a<序号>":这之前的输入都已写进终端
      const n = +e.data.slice(1);
      tv.unacked = tv.unacked.filter(m => m.seq > n);
      if (!ws.mode) {  // 刚连上:服务器告诉我们它收到哪了,补发其余的
        ws.mode = 'ack';
        for (const m of tv.unacked) ws.send('2' + m.seq + ':' + m.d);
      }
      inputStatus();
      return;
    }
    tv.term.write(new Uint8Array(e.data));
  };
  ws.onclose = async () => {
    if (tv.name !== name || tv.ws !== ws) return;
    await loadTerms();
    if (tv.name !== name) return;  // 会话已结束,syncDock 已切回"继续"按钮
    const ov = $('#term-overlay');
    ov.hidden = false;
    ov.textContent = '连接断开,正在重连…(点击立即重连)' + (tv.unacked.length ? ',已输入的内容会在重连后补发' : '');
    tv.retry = setTimeout(connectTerm, 2000);
  };
}

// 输入迟迟没有确认:连接多半已经死了(浏览器要很久才发现),换一条;断开太久的输入不再补发,明确告诉用户
setInterval(() => {
  if (!tv.unacked.length) return;
  const age = Date.now() - tv.unacked[0].t;
  if (age > 30000) {
    const n = tv.unacked.reduce((a, m) => a + m.d.length, 0);
    tv.unacked = [];
    toast(`网络中断太久,${n} 个字符没有送达,已丢弃,请重新输入`);
  } else if (age > 6000 && tv.ws?.readyState === 1 && tv.name && !tv.stale) {
    tv.stale = true;
    clearTimeout(tv.retry);
    connectTerm();
    setTimeout(() => { tv.stale = false; }, 5000);
  }
  inputStatus();
}, 1000);

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
  const r = await post(`/api/sessions/${state.data.meta.id}/continue`).catch(() => ({ error: '网络错误' }));
  b.disabled = false; b.textContent = '▶ 在此继续对话';
  if (r.error) return alert(r.error);
  await loadTerms();
  setTimeout(() => tv.term?.focus(), 300);
});
$('#dock-kill').addEventListener('click', async () => {
  if (!tv.name || !confirm(`结束 ${tv.name}?其中的 cc 会被关闭(对话记录保留,可随时"在此继续")。`)) return;
  await post(`/api/terms/${tv.name}/kill`).catch(() => null);
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
  $('#view').classList.remove('stars', 'summary'); $('#view').classList.add('file'); $('#star-link').classList.remove('on'); $('#sum-link').classList.remove('on');
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
  $('#view').classList.remove('file', 'summary'); $('#view').classList.add('stars'); detachTerm(); $('#star-link').classList.add('on');
  $('#sum-link').classList.remove('on');
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

// ---------- 总结:输入要求(如"只要论文""开发 MeowNotebook"),选某一天或一段时间,让 cc 总结这期间的问答 ----------
const sum = { list: [], cur: null, mode: localGet('sum-mode', 'day'), timer: null };
const isoDay = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const dayAgo = n => { const d = new Date(); d.setDate(d.getDate() - n); return isoDay(d); };

async function openSummary(id) {
  $('#empty').hidden = true; $('#view').hidden = false;
  $('#view').classList.remove('file'); $('#view').classList.add('stars', 'summary'); detachTerm();
  $('#star-link').classList.remove('on'); $('#sum-link').classList.add('on');
  $('#title').textContent = '✦ 总结';
  document.title = '总结 · MeowNotebook';
  $('#sub').innerHTML = '<span>按你的要求,让 cc 总结某一天或一段时间里的问答</span><span>存放在本地 data/summaries/</span>';
  if (!$('#sum-form')) renderSumForm();
  const [l, cur] = await Promise.all([api('/api/summaries').catch(() => null), id ? api('/api/summaries/' + id).catch(() => null) : null]);
  if (l) sum.list = l.summaries;
  sum.cur = cur && !cur.error ? cur : null;
  renderSumBody();
  clearTimeout(sum.timer);
  if (sum.list.some(x => x.status === 'running') && location.hash.startsWith('#/summary'))
    sum.timer = setTimeout(() => location.hash.startsWith('#/summary') && openSummary(id), 2500);
}

function renderSumForm() {
  $('#article').innerHTML = `<form id="sum-form" autocomplete="off">
      <input type="text" id="sum-topic" placeholder="要求,如:只要论文 / 我开发 MeowNotebook 的进展(留空 = 全部)">
      <span class="seg"><button type="button" data-mode="day">某一天</button><button type="button" data-mode="range">一段时间</button></span>
      <input type="date" id="sum-from"><span id="sum-to-wrap">至 <input type="date" id="sum-to"></span>
      <span class="presets"><button type="button" data-preset="0">今天</button><button type="button" data-preset="1">昨天</button><button type="button" data-preset="7">近 7 天</button><button type="button" data-preset="30">近 30 天</button></span>
      <button id="sum-go" type="submit">生成总结</button>
    </form><div id="sum-body"></div>`;
  $('#sum-topic').value = localGet('sum-topic', '');
  $('#sum-from').value = $('#sum-to').value = isoDay(new Date());
  $('#sum-from').max = $('#sum-to').max = isoDay(new Date());
  setSumMode(sum.mode);
  $('#sum-form').addEventListener('click', e => {
    const m = e.target.closest('[data-mode]'); if (m) return setSumMode(m.dataset.mode);
    const pr = e.target.closest('[data-preset]'); if (!pr) return;
    const n = +pr.dataset.preset;
    if (n <= 1) { setSumMode('day'); $('#sum-from').value = dayAgo(n); }
    else { setSumMode('range'); $('#sum-from').value = dayAgo(n - 1); $('#sum-to').value = dayAgo(0); }
  });
  $('#sum-form').addEventListener('submit', async e => {
    e.preventDefault();
    const from = $('#sum-from').value, to = sum.mode === 'day' ? from : $('#sum-to').value;
    if (!from || !to) return toast('请选择日期');
    const topic = $('#sum-topic').value.trim();
    localSet('sum-topic', topic);
    $('#sum-go').disabled = true;
    const r = await post('/api/summaries', { topic, from, to }).catch(() => ({ error: '网络错误' }));
    $('#sum-go').disabled = false;
    if (r.error) return toast(r.error, 4000);
    location.hash = '#/summary/' + r.id;
  });
}
function setSumMode(m) {
  sum.mode = m; localSet('sum-mode', m);
  $('#sum-form').querySelectorAll('[data-mode]').forEach(b => b.classList.toggle('on', b.dataset.mode === m));
  $('#sum-to-wrap').hidden = m === 'day';
}

const sumSpan = x => x.from === x.to ? x.from : `${x.from} 至 ${x.to}`;
function renderSumBody() {
  const st = x => x.status === 'running' ? '<span class="st">生成中…</span>'
    : x.status === 'error' ? '<span class="st err">失败</span>' : `<span class="st">${x.sessions} 个会话 · ${x.turns} 问</span>`;
  const list = sum.list.map(x => `<a class="sum-item${sum.cur?.id === x.id ? ' on' : ''}" href="#/summary/${x.id}">
      <span class="when">${esc(sumSpan(x))}</span><span class="topic">${esc(x.topic)}</span>${st(x)}</a>`).join('');
  let cur = '';
  const c = sum.cur;
  if (c) {
    const head = `<div class="sum-actions"><b>${esc(c.topic)}</b><span>${esc(sumSpan(c))} · ${c.sessions} 个会话 · ${c.turns} 问</span><span class="sp"></span>
      ${c.status === 'done' && state.qq?.enabled ? `<button data-sum-send="${c.id}" ${state.qq.can_send ? '' : 'disabled title="QQ 只允许在你给机器人发消息后 60 分钟内回复"'}>发送到 QQ</button>` : ''}
      <button class="danger" data-sum-del="${c.id}">删除</button></div>`;
    cur = head + (c.status === 'running' ? '<div class="hint">cc 正在阅读这段时间的问答并总结,通常要一两分钟…</div>'
      : c.status === 'error' ? `<div class="hint">生成失败:${esc(c.error || '')}</div>`
      : `<div class="md">${renderMD(c.body)}</div>`);
  }
  mdCwd = '';
  $('#sum-body').innerHTML = cur + (list ? `<div class="sum-actions"><span>历史总结</span></div>${list}` : '');
  markArticle($('#sum-body'));
  renderMap([]);
}
$('#article').addEventListener('click', async e => {
  const sd = e.target.closest('[data-sum-send]'), dl = e.target.closest('[data-sum-del]');
  if (sd) {
    sd.disabled = true;
    const r = await post(`/api/summaries/${sd.dataset.sumSend}/send`).catch(() => ({ error: '网络错误' }));
    sd.disabled = false;
    toast(r.error ? '发送失败:' + r.error : '已发送到' + r.to, r.error ? 5000 : 2000);
  } else if (dl && confirm('删除这份总结?')) {
    await post(`/api/summaries/${dl.dataset.sumDel}/delete`).catch(() => null);
    location.hash = '#/summary';
  }
});

// ---------- 交互 ----------
document.querySelectorAll('#tabs [data-tab]').forEach(b => b.addEventListener('click', () => {
  state.tab = b.dataset.tab; localSet('tab', state.tab); renderSession();
}));
['#show-process', '#show-misc'].forEach(id => $(id).addEventListener('change', renderSession));

// QQ 机器人(data/qqbot.json 配好才有):侧栏顶部的连接状态,以及第一次发消息的用户等你允许
function renderQQ(qq) {
  state.qq = qq;
  const st = $('#qq-status');
  st.hidden = !qq.enabled;
  if (qq.enabled) {
    st.innerHTML = `<span class="dot ${qq.connected ? 'live' : 'off'}"></span>QQ`;
    st.title = qq.connected ? `QQ 机器人已连接 · 允许 ${qq.allowed} 个用户`
      : 'QQ 机器人未连接' + (qq.error ? ':' + qq.error : '') + '\n一直连不上?欢迎到 github.com/MiaowFisherSpeaker/MeowNotebook/issues 提 issue';
  }
  const pd = $('#qq-pending'), p = qq.pending;
  pd.hidden = !p;
  if (p) pd.innerHTML = `QQ 用户 <code>${esc(p.openid.slice(0, 8))}…</code> 发来:「${esc(p.text)}」<br>
    是你自己就允许它使用机器人(允许后它能在这台机器上运行 cc)。<br><button data-qq-allow="${esc(p.openid)}">允许</button>`;
}
$('#qq-pending').addEventListener('click', async e => {
  const b = e.target.closest('[data-qq-allow]'); if (!b) return;
  const r = await post('/api/qq/allow', { openid: b.dataset.qqAllow }).catch(() => ({ error: '网络错误' }));
  if (r.error) return toast(r.error, 4000);
  renderQQ(r.qq); toast('已允许,再在 QQ 里发一条消息试试', 3000);
});

// 整理版发回 QQ:QQ 只允许在你给机器人发消息后 60 分钟内回复,超时按钮变灰
function renderSendNote() {
  const b = $('#send-note');
  b.hidden = !(state.qq?.enabled && state.data?.note);
  b.disabled = !state.qq?.can_send;
  b.title = state.qq?.can_send ? '把整理版正文作为文字发到 QQ' : 'QQ 只允许在你给机器人发消息后 60 分钟内回复:先在 QQ 里给机器人发一条消息';
}
$('#send-note').addEventListener('click', async () => {
  const b = $('#send-note'), sid = state.data?.meta.id; if (!sid) return;
  b.disabled = true;
  const r = await post(`/api/sessions/${sid}/send-note`).catch(() => ({ error: '网络错误' }));
  b.disabled = false;
  toast(r.error ? '发送失败:' + r.error : '整理版已发送到' + r.to, r.error ? 5000 : 2000);
});

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
  const sm = location.hash.match(/^#\/summary(?:\/([\w-]+))?$/);
  if (sm) { state.sid = null; state.data = null; renderSessions(); openSummary(sm[1] || null); return; }
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

// 服务器推送变化(SSE):记录、整理版或终端变了才取数据。推送连着时不再每 3 秒轮询;
// 断开时(网络差、代理不支持)EventSource 自己重连,其间退回轮询。会话列表 / 当前会话另有 15 秒的兜底刷新。
const live = { ok: false, es: null, last: 0, listT: 0, listTimer: null };
function refreshList() {  // 记录在写时每秒都有变化,列表最多 5 秒刷新一次
  if (live.listTimer) return;
  live.listTimer = setTimeout(() => { live.listTimer = null; live.listT = Date.now(); loadSessions(); },
    Math.max(0, 5000 - (Date.now() - live.listT)));
}
function onChanged(ids) {
  if (document.hidden) { live.missed = true; return; }
  const known = new Set(state.sessions.map(s => s.id));
  if (state.sid && (ids.includes(state.sid) || ids.some(id => !known.has(id)))) loadSession(false, true);
  refreshList();
}
function connectLive() {
  if (!window.EventSource) return;
  const es = live.es = new EventSource('/api/events');
  es.onmessage = e => {
    live.last = Date.now();
    let ev; try { ev = JSON.parse(e.data); } catch { return; }
    if (ev.hello) {  // 连上 / 重连:断开期间的变化不会补推,全量刷新一次
      live.ok = true; netState(true);
      loadTerms(); loadSessions(); loadSession(false, true);
      return;
    }
    if (ev.terms) onTerms(ev.terms);
    if (ev.changed) onChanged(ev.changed);
  };
  es.onerror = () => { live.ok = false; };
}
// 服务器至少每 15 秒发一次;40 秒没动静 = 连接卡死了(网络差时浏览器不一定察觉),先退回轮询并重连
setInterval(() => {
  if (!live.ok || Date.now() - live.last < 40000) return;
  live.ok = false; live.es.close(); connectLive();
}, 5000);
document.addEventListener('visibilitychange', () => {
  if (document.hidden || !live.missed) return;
  live.missed = false; loadSession(false, true); loadSessions();
});
setInterval(() => { if (!document.hidden && !live.ok && (state.data?.active || tv.name)) loadSession(false, true); }, 3000);
setInterval(() => { if (!document.hidden && !live.ok) loadTerms(); }, 3000);
setInterval(() => { if (!document.hidden) { loadSessions(); if (state.data && (live.ok || !state.data.active)) loadSession(false, true); } }, 15000);
connectLive();
setSide(localGet('side-tab', 'sessions'));
Promise.all([loadSessions(), loadStars()]).then(route);
