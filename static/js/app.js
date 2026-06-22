/* Novel Rewriter v7.0.0 - Main Application */
const API = '';
let curBook = null, curCh = null, currentBookData = null;
let allBooks = [];
let lastRewriteData = null;
let fontSize = 13;
let extractedNames = {};
let syncScrollEnabled = true;
let bookRewriteResult = null;
let viewMode = 'both';
let isStreaming = false;
let currentChTitle = null;

// ===== 初始化 =====
async function init() {
  addRule();
  await loadBooks();
  await loadTemplates();
  loadTheme();
  loadFontSize();
  loadDraft();
  updateCnt();
  loadApiKey();
  setupSyncScroll();
  fetchVersion();
  registerSW();
  updateEditorButtons();
}

// ===== PWA =====
function registerSW() {
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/static/sw.js').catch(()=>{});
  }
}

// ===== 对比模式切换 =====
function setViewMode(mode) {
  viewMode = mode;
  document.querySelectorAll('.vm').forEach(b => b.classList.remove('on'));
  document.getElementById('vmBoth').classList.toggle('on', mode==='both');
  document.getElementById('vmOrig').classList.toggle('on', mode==='orig');
  document.getElementById('vmResult').classList.toggle('on', mode==='result');
  const cmpOrig = document.getElementById('cmpOrig');
  const cmpResult = document.getElementById('cmpResult');
  if (mode === 'both') {
    cmpOrig.style.display = 'flex';
    cmpResult.style.display = 'flex';
  } else if (mode === 'orig') {
    cmpOrig.style.display = 'flex';
    cmpResult.style.display = 'none';
  } else {
    cmpOrig.style.display = 'none';
    cmpResult.style.display = 'flex';
  }
}

// ===== 主题 =====
function loadTheme() {
  const t = localStorage.getItem('nr_theme') || 'dark';
  document.body.classList.toggle('light', t === 'light');
  document.getElementById('themeBtn').textContent = t === 'light' ? '☀️' : '🌙';
}
function toggleTheme() {
  const isLight = document.body.classList.toggle('light');
  localStorage.setItem('nr_theme', isLight ? 'light' : 'dark');
  document.getElementById('themeBtn').textContent = isLight ? '☀️' : '🌙';
}

// ===== 字体大小 =====
function loadFontSize() {
  fontSize = parseInt(localStorage.getItem('nr_fontsize') || '13');
  applyFontSize();
}
function changeFontSize(delta) {
  fontSize = Math.max(10, Math.min(24, fontSize + delta));
  localStorage.setItem('nr_fontsize', fontSize);
  applyFontSize();
}
function applyFontSize() {
  document.getElementById('origText').style.fontSize = fontSize + 'px';
  document.getElementById('resultText').style.fontSize = fontSize + 'px';
  document.getElementById('fontSizeVal').textContent = fontSize + 'px';
}

// ===== 本地暂存 =====
function saveDraft() {
  localStorage.setItem('nr_draft', document.getElementById('origText').value);
}
function loadDraft() {
  const d = localStorage.getItem('nr_draft');
  if (d) document.getElementById('origText').value = d;
}

// ===== 书库 =====
async function loadBooks() {
  try {
    const r = await fetch(`${API}/api/books`);
    const d = await r.json();
    allBooks = d.books || [];
    renderBookList();
  } catch(e) { console.error(e); }
}

function renderBookList() {
  const el = document.getElementById('bookList');
  const q = (document.getElementById('bookSearch')?.value || '').toLowerCase();
  const scope = document.getElementById('searchScope')?.value || 'title';
  const filtered = q ? allBooks.filter(b => b.title.toLowerCase().includes(q) || (b.author||'').toLowerCase().includes(q)) : allBooks;
  if (!filtered.length) { el.innerHTML = '<div class="empty"><span class="e">📚</span>暂无书籍</div>'; return; }

  let html = '';
  filtered.forEach(b => {
    const isExpanded = b.id === curBook && currentBookData;
    const on = b.id === curBook ? ' on' : '';
    const arrow = isExpanded ? '▼' : '▶';
    html += `<div class="bk${on}" onclick="selectBook('${escJs(b.id)}')">` +
      `<span class="bi" style="font-size:8px">${arrow}</span>` +
      `<span class="bi">📖</span>` +
      `<div style="flex:1;min-width:0"><div class="bn">${esc(b.title)}</div><div class="bm">${b.author?esc(b.author)+' · ':''}${b.chapter_count}章</div></div>` +
      `<span class="bx" onclick="event.stopPropagation();confirmDelete('book','${escJs(b.id)}','「${escJs(b.title)}」')">×</span>` +
    `</div>`;
    if (isExpanded) {
      const chs = currentBookData.chapters || [];
      chs.forEach((ch,i) => {
        const chOn = ch.id === curCh ? ' on' : '';
        const chEditing = ch.id === curCh && currentChTitle ? ' editing' : '';
        html += `<div class="ch${chOn}${chEditing}" draggable="true" data-id="${esc(ch.id)}"
          ondragstart="dragStart(event,${i})"
          ondragover="dragOver(event)"
          ondragleave="dragLeave(event)"
          ondrop="dropChapter(event,${i})"
          ondragend="dragEnd(event)"
          onclick="loadChapterById('${escJs(b.id)}','${escJs(ch.id)}')">
          <span class="ch-t">${esc(ch.title)}</span>
          <span class="ch-x" onclick="event.stopPropagation();confirmDelete('chapter','${escJs(ch.id)}','「${escJs(ch.title)}」')">×</span>
        </div>`;
      });
      html += `<div class="ch-add" onclick="event.stopPropagation();showModal('addChModal')">+ 添加章节</div>`;
    }
  });
  el.innerHTML = html;
}

// ===== 内容搜索 =====
let searchTimer = null;
function handleSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(async () => {
    const q = document.getElementById('bookSearch').value.trim();
    const scope = document.getElementById('searchScope').value;
    if (!q) { await loadBooks(); return; }
    if (scope === 'title') {
      renderBookList();
    } else {
      try {
        const r = await fetch(`${API}/api/books/search?q=${encodeURIComponent(q)}&scope=content`);
        const d = await r.json();
        renderSearchResults(d.results || [], q);
      } catch(e) { console.error(e); }
    }
  }, 400);
}

function renderSearchResults(results, q) {
  const el = document.getElementById('bookList');
  if (!results.length) { el.innerHTML = '<div class="empty"><span class="e">🔍</span>未找到匹配内容</div>'; return; }
  let html = '<div style="padding:4px 8px;font-size:9px;color:var(--wn);margin-bottom:4px;cursor:pointer" onclick="loadBooks();handleSearch()">📖 内容搜索结果 · 点击返回书库</div>';
  results.forEach(b => {
    html += `<div class="bk" onclick="curBook=null;currentBookData=null;selectBook('${escJs(b.id)}')">
      <span class="bi">📖</span>
      <div style="flex:1;min-width:0"><div class="bn">${esc(b.title)}</div><div class="bm">${b.author?esc(b.author)+' · ':''}${b.chapter_count||0}章</div></div>
    </div>`;
    if (b.matched_chapters && b.matched_chapters.length) {
      b.matched_chapters.slice(0, 3).forEach(ch => {
        html += `<div class="ch" onclick="event.stopPropagation();curBook=null;currentBookData=null;selectBookAndChapter('${escJs(b.id)}','${escJs(ch.id)}')" style="background:var(--sf2)">
          <span class="ch-t">${esc(ch.title)}</span>
          <span style="font-size:9px;color:var(--wn);margin-left:4px">命中</span>
        </div>
        <div style="padding:2px 8px 2px 36px;font-size:9px;color:var(--tx3);line-height:1.4;margin-bottom:3px">${highlightSnippet(ch.snippet, q)}</div>`;
      });
    }
  });
  el.innerHTML = html;
}

function highlightSnippet(snippet, q) {
  if (!q || !snippet) return esc(snippet);
  const escaped = esc(snippet);
  const qEsc = esc(q);
  const regex = new RegExp('(' + qEsc.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
  return escaped.replace(regex, '<span class="search-hl">$1</span>');
}

// ===== 章节拖拽排序（修复版）=====
let dragIdx = null;
function dragStart(e, idx) {
  dragIdx = idx;
  e.target.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move';
}
function dragOver(e) {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  e.currentTarget.classList.add('drag-over');
}
function dragLeave(e) {
  e.currentTarget.classList.remove('drag-over');
}
function dropChapter(e, targetIdx) {
  e.preventDefault();
  e.currentTarget.classList.remove('drag-over');
  if (dragIdx === null || dragIdx === targetIdx) return;

  const chEls = Array.from(document.querySelectorAll('.ch[draggable=true]'));
  const chIds = chEls.map(el => el.dataset.id).filter(Boolean);

  if (chIds.length < 2) return;

  // 移动元素
  const movedId = chIds[dragIdx];
  chIds.splice(dragIdx, 1);
  const adjustedTarget = targetIdx > dragIdx ? targetIdx - 1 : targetIdx;
  chIds.splice(adjustedTarget, 0, movedId);

  reorderChapters(curBook, chIds);
}
function dragEnd(e) {
  e.currentTarget.classList.remove('dragging','drag-over');
  document.querySelectorAll('.ch').forEach(el => el.classList.remove('drag-over'));
  dragIdx = null;
}

async function reorderChapters(bookId, chapterIds) {
  try {
    await fetch(`${API}/api/books/${bookId}/chapters/reorder`, {
      method:'PUT', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({chapter_ids:chapterIds})
    });
    toast('章节顺序已更新','ok');
    const r = await fetch(`${API}/api/books/${bookId}`);
    currentBookData = await r.json();
    renderBookList();
  } catch(e) { toast('排序失败','err'); }
}

async function selectBook(id) {
  if (curBook === id) {
    curBook = null; curCh = null; currentBookData = null; currentChTitle = null;
    renderBookList();
    updateEditorButtons();
    updateChapterNav();
    return;
  }
  curBook = id; curCh = null;
  try {
    const r = await fetch(`${API}/api/books/${id}`);
    currentBookData = await r.json();
    renderBookList();
    updateEditorButtons();
    updateChapterNav();
  } catch(e) { toast('加载失败','err'); }
}

function loadChapterById(bookId, chId) {
  if (!currentBookData || currentBookData.id !== bookId) return;
  const ch = (currentBookData.chapters || []).find(c => c.id === chId);
  if (!ch) return;
  curCh = chId;
  currentChTitle = ch.title;
  document.getElementById('origText').value = ch.content;
  document.getElementById('resultText').innerHTML = '等待翻改...';
  document.getElementById('repInfo').textContent = '结果';
  updateCnt(); saveDraft();
  renderBookList();
  updateEditorButtons();
  updateChapterNav();
  toast(`已加载「${ch.title}」`,'ok');
}

async function selectBookAndChapter(bookId, chId) {
  await selectBook(bookId);
  // 等待 DOM 更新后加载章节
  setTimeout(() => loadChapterById(bookId, chId), 100);
}

// ===== 编辑器按钮更新 =====
function updateEditorButtons() {
  const saveBtn = document.getElementById('saveChBtn');
  const applyBtn = document.getElementById('applyBtn');
  const origTitle = document.getElementById('origTitle');

  if (curCh && currentChTitle) {
    saveBtn.style.display = 'block';
    origTitle.textContent = '原文 · ' + currentChTitle;
    applyBtn.style.display = 'block';
  } else {
    saveBtn.style.display = 'none';
    origTitle.textContent = '原文';
    applyBtn.style.display = 'none';
  }
}

// ===== 保存章节内容 =====
async function saveChapterContent() {
  if (!curBook || !curCh) { toast('请先选择章节','wn'); return; }
  const content = document.getElementById('origText').value;
  showLd('保存中...');
  try {
    const r = await fetch(`${API}/api/books/${curBook}/chapters/${curCh}`, {
      method:'PUT',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ content })
    });
    if (!r.ok) throw new Error('保存失败');
    toast('章节已保存','ok');
    const r2 = await fetch(`${API}/api/books/${curBook}`);
    currentBookData = await r2.json();
    renderBookList();
  } catch(e) { toast('保存失败: '+e.message,'err'); }
  finally { hideLd(); }
}

// ===== 应用翻改结果到章节 =====
async function applyResultToChapter() {
  if (!curBook || !curCh) { toast('请先选择章节','wn'); return; }
  const resultEl = document.getElementById('resultText');
  const resultText = resultEl.textContent;
  if (!resultText || resultText === '等待翻改...' || resultText === '无翻改结果') {
    toast('没有可应用的结果','wn'); return;
  }
  // 提取纯文本（去除高亮标记）
  const tempDiv = document.createElement('div');
  tempDiv.innerHTML = resultEl.innerHTML;
  const cleanText = tempDiv.textContent || tempDiv.innerText || resultText;

  document.getElementById('origText').value = cleanText;
  updateCnt();
  await saveChapterContent();
  toast('翻改结果已应用到章节','ok');
}

// ===== 删除 =====
let deleteTarget = null;
function confirmDelete(type, id, name) {
  deleteTarget = { type, id };
  document.getElementById('confirmText').textContent = `确定要删除${name}吗？此操作不可撤销。`;
  document.getElementById('confirmBtn').onclick = executeDelete;
  showModal('confirmModal');
}
async function executeDelete() {
  if (!deleteTarget) return;
  const { type, id } = deleteTarget;
  hideModal('confirmModal');
  try {
    if (type === 'book') {
      await fetch(`${API}/api/books/${id}`, {method:'DELETE'});
      if (curBook === id) { curBook = null; curCh = null; currentBookData = null; currentChTitle = null; }
      toast('已删除','ok');
      await loadBooks();
      updateEditorButtons();
    } else if (type === 'chapter' && curBook) {
      await fetch(`${API}/api/books/${curBook}/chapters/${id}`, {method:'DELETE'});
      if (curCh === id) { curCh = null; currentChTitle = null; }
      toast('已删除','ok');
      const r = await fetch(`${API}/api/books/${curBook}`);
      currentBookData = await r.json();
      await loadBooks();
      curBook = currentBookData.id;
      renderBookList();
      updateEditorButtons();
    } else if (type === 'template') {
      await fetch(`${API}/api/rules/${id}`, {method:'DELETE'});
      toast('已删除','ok');
      await loadTemplates();
    }
  } catch(e) { toast('删除失败','err'); }
  deleteTarget = null;
}

function togglePanel(id) { document.getElementById(id).classList.toggle('hide'); }

// ===== 替换规则 =====
function addRule(o='', r='') {
  const c = document.getElementById('rulesBox');
  const d = document.createElement('div');
  d.className = 'rc';
  d.innerHTML = `<input type="text" placeholder="原名" value="${esc(o)}" class="oi"><span class="ar">→</span><input type="text" placeholder="新名" value="${esc(r)}" class="ri" oninput="updateRuleStat()"><button class="dx" onclick="this.closest('.rc').remove();updateRuleStat()">×</button>`;
  c.appendChild(d);
  updateRuleStat();
}
function clearRules() {
  document.getElementById('rulesBox').innerHTML = '';
  addRule();
  updateRuleStat();
  toast('规则已清空','ok');
}
function getRules() {
  const rules = [];
  document.querySelectorAll('.rc').forEach(c => {
    const o = c.querySelector('.oi').value.trim();
    const r = c.querySelector('.ri').value.trim();
    if (o && r) rules.push({original:o, replacement:r});
  });
  return rules;
}
function updateRuleStat() { document.getElementById('sRules').textContent = getRules().length; }

function toggleBatch() { document.getElementById('batchArea').classList.toggle('show'); }

function parseBatch() {
  const txt = document.getElementById('batchInput').value.trim();
  if (!txt) return;
  const lines = txt.split('\n').filter(l => l.trim());
  let count = 0;
  lines.forEach(line => {
    const parts = line.split(/[→=]/);
    if (parts.length >= 2) {
      const o = parts[0].trim(), r = parts.slice(1).join('→').trim();
      if (o && r) { addRule(o, r); count++; }
    }
  });
  document.getElementById('batchInput').value = '';
  toast(`已添加 ${count} 条规则`,'ok');
}

// ===== 智能提取 =====
async function extractNames() {
  const text = document.getElementById('origText').value.trim();
  if (!text) { toast('请先输入原文','wn'); return; }
  showLd('提取中...');
  extractedNames = {};
  try {
    const r = await fetch(`${API}/api/extract`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text}) });
    if (!r.ok) throw new Error('提取失败');
    const d = await r.json();
    showExtractResults(d.names);
  } catch(e) { toast('提取失败','err'); }
  finally { hideLd(); }
}

function showExtractResults(names) {
  const el = document.getElementById('extResults');
  el.innerHTML = '';
  const cats = [
    {key:'person', label:'👤 人物', cls:'tag-p'},
    {key:'location', label:'🗺️ 地名', cls:'tag-l'},
    {key:'organization', label:'🏛️ 组织', cls:'tag-o'},
    {key:'item', label:'⚔️ 物品', cls:'tag-i'}
  ];
  let hasAny = false;
  cats.forEach(cat => {
    const items = names[cat.key] || [];
    if (!items.length) return;
    hasAny = true;
    const sec = document.createElement('div');
    sec.style.marginBottom = '5px';
    sec.innerHTML = `<span class="tag ${cat.cls}">${cat.label}</span> `;
    items.forEach(name => {
      const chip = document.createElement('span');
      chip.className = 'nc';
      chip.textContent = name;
      chip.dataset.name = name;
      chip.onclick = () => toggleExtractedName(chip, name);
      sec.appendChild(chip);
    });
    el.appendChild(sec);
  });
  if (!hasAny) { el.innerHTML = '<div style="font-size:10px;color:var(--tx3)">未提取到名称</div>'; }
  document.getElementById('extPanel').classList.add('show');
}

function toggleExtractedName(chip, name) {
  const isSelected = chip.classList.toggle('sel');
  if (isSelected) {
    addRule(name, '');
    extractedNames[name] = true;
    const cards = document.querySelectorAll('.rc');
    const last = cards[cards.length - 1];
    if (last) last.querySelector('.ri').focus();
  } else if (extractedNames[name]) {
    document.querySelectorAll('.rc').forEach(c => {
      const oi = c.querySelector('.oi');
      if (oi && oi.value === name) c.remove();
    });
    delete extractedNames[name];
    updateRuleStat();
  }
}

// ===== 模板 =====
async function loadTemplates() {
  try {
    const r = await fetch(`${API}/api/rules`);
    const d = await r.json();
    renderTemplates(d.rules || []);
  } catch(e) {}
}
function renderTemplates(tps) {
  const el = document.getElementById('tpList');
  if (!tps.length) { el.innerHTML = '<div style="font-size:9px;color:var(--tx3)">暂无模板</div>'; return; }
  el.innerHTML = '';
  tps.forEach(t => {
    const d = document.createElement('div');
    d.className = 'tp-item';
    d.innerHTML = `<span>${esc(t.name)} (${t.rules.length})</span><span class="bx" style="opacity:1;font-size:11px" onclick="event.stopPropagation();confirmDelete('template','${escJs(t.id)}','「${escJs(t.name)}」模板')">×</span>`;
    d.onclick = () => { t.rules.forEach(r => addRule(r.original, r.replacement)); toast(`已加载 ${t.rules.length} 条规则`,'ok'); };
    el.appendChild(d);
  });
}
async function saveTemplate() {
  const name = document.getElementById('tpName').value.trim();
  if (!name) { toast('请输入模板名','wn'); return; }
  const rules = getRules();
  if (!rules.length) { toast('没有可保存的规则','wn'); return; }
  try {
    await fetch(`${API}/api/rules`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name,rules}) });
    hideModal('saveTpModal');
    document.getElementById('tpName').value = '';
    toast('模板已保存','ok');
    await loadTemplates();
  } catch(e) { toast('保存失败','err'); }
}

// ===== AI 选项 =====
function toggleAi() {
  const on = document.getElementById('useAi').checked;
  document.getElementById('aiSep').style.display = on?'':'none';
  document.getElementById('aiPrvLbl').style.display = on?'':'none';
  document.getElementById('aiLvLbl').style.display = on?'':'none';
  document.getElementById('sAi').style.display = on?'':'none';
}

// ===== API Key =====
function loadApiKey() {
  const provider = document.getElementById('apiKeyProvider').value;
  const key = localStorage.getItem('nr_apikey_' + provider) || '';
  document.getElementById('apiKeyIn').value = key;
}
function saveApiKey() {
  const provider = document.getElementById('apiKeyProvider').value;
  const key = document.getElementById('apiKeyIn').value.trim();
  localStorage.setItem('nr_apikey_' + provider, key);
  hideModal('apiModal');
  toast('API Key 已保存','ok');
}

// ===== 文件导入 =====
function importFile() {
  document.getElementById('fileInput').click();
}
function handleFile(event) {
  const file = event.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = (e) => {
    document.getElementById('origText').value = e.target.result;
    updateCnt();
    saveDraft();
    toast(`已导入 ${file.name}`,'ok');
  };
  reader.readAsText(file, 'UTF-8');
  event.target.value = '';
}

// ===== 下载 =====
function downloadResult(format) {
  const resultEl = document.getElementById('resultText');
  const t = resultEl.textContent;
  if (!t || t==='等待翻改...') { toast('没有可下载的内容','wn'); return; }
  let content, fileName, mimeType;
  const useJson = format === 'json';

  if (bookRewriteResult && bookRewriteResult.chapters && bookRewriteResult.chapters.length > 0) {
    if (useJson) {
      const exportData = {
        book_title: bookRewriteResult.book_title,
        export_date: new Date().toISOString(),
        total_chapters: bookRewriteResult.total_chapters,
        total_replacements: bookRewriteResult.total_replacements,
        chapters: bookRewriteResult.chapters.map(ch => ({
          id: ch.id, title: ch.title, original: ch.original,
          rewritten: ch.rewritten, replace_count: ch.replace_count,
          replacements: ch.replacements
        }))
      };
      content = JSON.stringify(exportData, null, 2);
      fileName = `${bookRewriteResult.book_title}_翻改结果_${new Date().toISOString().slice(0,10)}.json`;
      mimeType = 'application/json';
    } else {
      content = `《${bookRewriteResult.book_title}》翻改结果\n${'='.repeat(40)}\n\n`;
      bookRewriteResult.chapters.forEach(ch => {
        content += `${ch.title}\n${'-'.repeat(30)}\n${ch.rewritten}\n\n`;
      });
      fileName = `${bookRewriteResult.book_title}_翻改结果_${new Date().toISOString().slice(0,10)}.txt`;
      mimeType = 'text/plain;charset=utf-8';
    }
  } else {
    if (useJson) {
      const exportData = {
        chapter: currentChTitle || '未命名',
        export_date: new Date().toISOString(),
        original: document.getElementById('origText').value,
        rewritten: t,
        replacements: lastRewriteData ? lastRewriteData.replacements : []
      };
      content = JSON.stringify(exportData, null, 2);
      fileName = `翻改结果_${currentChTitle||'未命名'}_${new Date().toISOString().slice(0,10)}.json`;
      mimeType = 'application/json';
    } else {
      content = t;
      const chName = currentChTitle ? `_${currentChTitle}` : '';
      fileName = `翻改结果${chName}_${new Date().toISOString().slice(0,10)}.txt`;
      mimeType = 'text/plain;charset=utf-8';
    }
  }
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([content],{type:mimeType}));
  a.download = fileName;
  a.click();
  toast(useJson ? '已下载JSON' : '已下载','ok');
}

// ===== 导出 EPUB =====
async function downloadResultEpub() {
  if (!currentBookData || !currentBookData.id) {
    toast('请先打开一本书','wn'); return;
  }
  try {
    toast('正在生成 EPUB...','ok');
    const resp = await fetch(API + '/books/export?book_id=' + encodeURIComponent(currentBookData.id) + '&format=epub');
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      toast(err.detail || '导出失败','er');
      return;
    }
    const blob = await resp.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${currentBookData.title || 'book'}.epub`;
    a.click();
    toast('已导出 EPUB','ok');
  } catch(e) {
    toast('导出失败','er');
  }
}

// ===== 计数 =====
function updateCnt() {
  const o = document.getElementById('origText').value;
  const r = document.getElementById('resultText').textContent;
  document.getElementById('cntO').textContent = o.length;
  document.getElementById('cntR').textContent = (r==='等待翻改...') ? 0 : r.length;
}

// ===== 翻改（非流式）=====
async function doRewrite() {
  const text = document.getElementById('origText').value.trim();
  if (!text) { toast('请先输入原文','wn'); return; }
  // 付费墙：免费10次翻改，之后29元/月
  if (!Paywall.tryUse('rewrite', { price: '29', desc: '无限次数翻改 · AI句式改写 · 名称替换 · 结果下载', freeLimit: 10, contactWx: 'a5050e' })) return;
  const rules = getRules();
  if (!rules.length) { toast('请添加替换规则','wn'); return; }
  const useAi = document.getElementById('useAi').checked;
  const intensity = document.getElementById('aiLv').value;
  const provider = document.getElementById('aiProvider').value;
  const apiKey = localStorage.getItem('nr_apikey_' + provider) || '';
  if (useAi && !apiKey) { showModal('apiModal'); return; }

  const btn = document.getElementById('rewriteBtn');
  btn.disabled = true;
  showLd(useAi ? 'AI改写中（可能需要30-60秒）...' : '翻改中...');
  try {
    const r = await fetch(`${API}/api/rewrite`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ text, rules, use_ai:useAi, ai_intensity:intensity, api_key:useAi?apiKey:null, ai_provider:provider })
    });
    if (!r.ok) { const e = await r.json(); throw new Error(e.detail||'翻改失败'); }
    const d = await r.json();
    lastRewriteData = d;
    renderResult(d);
    let total = 0;
    d.replacements.forEach(rp => { if (rp.original!=='⚠️') total += rp.count; });
    document.getElementById('sReps').textContent = total;
    document.getElementById('repInfo').textContent = `${total}处替换`;
    updateCnt();
    toast(`翻改完成！${total}处替换`,'ok');
  } catch(e) { toast('翻改失败: '+e.message,'err'); }
  finally { btn.disabled = false; hideLd(); }
}

function renderResult(data) {
  const el = document.getElementById('resultText');
  let html = esc(data.rewritten);
  if (data.replacements) {
    // 按替换词(新名)长度降序排列，避免短词先匹配截断长词
    const sorted = data.replacements.filter(rp => rp.original !== '⚠️').sort((a,b) => b.replacement.length - a.replacement.length);
    const highlighted = new Set();
    sorted.forEach(rp => {
      const rEsc = esc(rp.replacement);
      if (!highlighted.has(rEsc)) {
        html = html.split(rEsc).join(`<span class="diff-new">${rEsc}</span>`);
        highlighted.add(rEsc);
      }
    });
  }
  el.innerHTML = html;
  applyFontSize();
}

// ===== 整本翻改（SSE流式）=====
async function rewriteBookStream() {
  if (!curBook || !currentBookData) { toast('请先选择一本书','wn'); return; }
  const rules = getRules();
  if (!rules.length) { toast('请添加替换规则','wn'); return; }
  const useAi = document.getElementById('useAi').checked;
  const intensity = document.getElementById('aiLv').value;
  const provider = document.getElementById('aiProvider').value;
  const apiKey = useAi ? (localStorage.getItem('nr_apikey_' + provider) || '') : '';
  if (useAi && !apiKey) { showModal('apiModal'); return; }

  const chapters = currentBookData.chapters || [];
  if (!chapters.length) { toast('该书没有章节','wn'); return; }

  const btn = document.getElementById('rewriteBookBtn');
  btn.disabled = true;
  const progEl = document.getElementById('bookRewriteProgress');
  progEl.style.display = 'block';
  bookRewriteResult = { book_id: curBook, book_title: currentBookData.title, total_chapters: 0, total_replacements: 0, chapters: [] };

  const resultEl = document.getElementById('resultText');
  resultEl.innerHTML = '<div style="color:var(--tx3);font-size:11px;">📡 流式翻改中，请稍候...</div>';

  for (let i = 0; i < chapters.length; i++) {
    const ch = chapters[i];
    const pct = Math.round(((i + 1) / chapters.length) * 100);
    document.getElementById('brProgress').style.width = pct + '%';
    document.getElementById('brProgressText').textContent = `第${i+1}/${chapters.length}章 ${pct}%`;
    document.getElementById('brStatus').textContent = `正在翻改: ${ch.title}`;

    try {
      if (!useAi) {
        const r = await fetch(`${API}/api/books/${curBook}/rewrite`, {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({ rules, use_ai: false, api_key: null, ai_provider: provider, chapter_ids: [ch.id] })
        });
        if (r.ok) {
          const d = await r.json();
          if (d.chapters && d.chapters[0]) {
            bookRewriteResult.chapters.push(d.chapters[0]);
            bookRewriteResult.total_replacements += d.chapters[0].replace_count;
          }
        }
      } else {
        await streamRewriteChapter(ch, rules, intensity, apiKey, provider);
      }
    } catch(e) { /* skip */ }
  }

  bookRewriteResult.total_chapters = bookRewriteResult.chapters.length;
  showBookRewriteResult(bookRewriteResult);
  btn.disabled = false;
  progEl.style.display = 'none';
  toast(`整本翻改完成！${bookRewriteResult.total_replacements}处替换`,'ok');
}

// SSE流式翻改单章
let streamRenderTimer = null;

function streamRewriteChapter(ch, rules, intensity, apiKey, provider) {
  return new Promise(async (resolve) => {
    streamRenderTimer = null;
    try {
      const body = JSON.stringify({
        text: ch.content, rules, use_ai: true, ai_intensity: intensity,
        api_key: apiKey, ai_provider: provider
      });
      const r = await fetch(`${API}/api/rewrite/stream`, {
        method:'POST', headers:{'Content-Type':'application/json'}, body
      });
      if (!r.ok) { resolve(); return; }
      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let fullText = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const dataStr = line.slice(6);
          if (dataStr === '[DONE]') continue;
          try {
            const data = JSON.parse(dataStr);
            if (data.type === 'chunk') {
              fullText += data.content;
              if (!streamRenderTimer) {
                streamRenderTimer = setTimeout(() => {
                  const el = document.getElementById('resultText');
                  const statusDiv = el.querySelector('div');
                  const contentDiv = statusDiv ? statusDiv.nextElementSibling : null;
                  if (contentDiv) {
                    contentDiv.textContent = fullText;
                  } else {
                    el.innerHTML = `<div style="font-size:11px;color:var(--pm)">正在翻改：${esc(ch.title)}</div><div style="margin-top:8px">${esc(fullText)}</div>`;
                  }
                  streamRenderTimer = null;
                }, 100);
              }
            } else if (data.type === 'done') {
              let rewritten = data.rewritten || fullText;
              let repDetails = data.replacements || [];
              let total = 0;
              repDetails.forEach(rp => { if (rp.original !== '⚠️') total += rp.count; });
              bookRewriteResult.chapters.push({
                id: ch.id, title: ch.title,
                original: ch.content, rewritten, replacements: repDetails, replace_count: total
              });
              bookRewriteResult.total_replacements += total;
            } else if (data.type === 'error') {
              console.error('SSE error:', data.msg);
            }
          } catch(e) {}
        }
      }
    } catch(e) { console.error(e); }
    if (streamRenderTimer) { clearTimeout(streamRenderTimer); streamRenderTimer = null; }
    resolve();
  });
}

function showBookRewriteResult(data) {
  const el = document.getElementById('resultText');
  let html = '';
  (data.chapters || []).forEach(ch => {
    html += `<div style="margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid var(--bd)">
      <div style="font-size:11px;color:var(--pm);font-weight:bold;margin-bottom:4px">${esc(ch.title)} <span style="color:var(--tx3);font-weight:normal">(${ch.replace_count}处替换)</span></div>`;
    let chHtml = esc(ch.rewritten);
    if (ch.replacements) {
      const sorted = ch.replacements.filter(rp => rp.original !== '⚠️').sort((a,b) => b.replacement.length - a.replacement.length);
      const highlighted = new Set();
      sorted.forEach(rp => {
        const rEsc = esc(rp.replacement);
        if (!highlighted.has(rEsc)) {
          chHtml = chHtml.split(rEsc).join(`<span class="diff-new">${rEsc}</span>`);
          highlighted.add(rEsc);
        }
      });
    }
    html += `<div style="font-size:13px;line-height:1.9;white-space:pre-wrap;word-break:break-all">${chHtml}</div></div>`;
  });
  if (!html) html = '<div style="color:var(--tx3)">无翻改结果</div>';
  el.innerHTML = html;
  document.getElementById('repInfo').textContent = `${data.total_replacements}处替换 · ${data.total_chapters}章`;
  document.getElementById('cntR').textContent = el.textContent.length;
}

// ===== 同步滚动 =====
function setupSyncScroll() {
  const origS = document.getElementById('origScroll');
  const resultS = document.getElementById('resultScroll');
  if (!origS || !resultS) return;
  let syncing = false;
  origS.addEventListener('scroll', () => {
    if (syncing || !syncScrollEnabled) return;
    syncing = true;
    const ratio = origS.scrollTop / (origS.scrollHeight - origS.clientHeight || 1);
    resultS.scrollTop = ratio * (resultS.scrollHeight - resultS.clientHeight);
    requestAnimationFrame(() => syncing = false);
  });
  resultS.addEventListener('scroll', () => {
    if (syncing || !syncScrollEnabled) return;
    syncing = true;
    const ratio = resultS.scrollTop / (resultS.scrollHeight - resultS.clientHeight || 1);
    origS.scrollTop = ratio * (origS.scrollHeight - origS.clientHeight);
    requestAnimationFrame(() => syncing = false);
  });
}
function toggleSyncScroll() {
  syncScrollEnabled = document.getElementById('syncScrollCb').checked;
}

// ===== 工具函数 =====
function showModal(id) { document.getElementById(id).classList.add('show'); }
function hideModal(id) { document.getElementById(id).classList.remove('show'); }
function showLd(t) { document.getElementById('ldText').textContent=t; document.getElementById('ldBg').classList.add('show'); }
function hideLd() { document.getElementById('ldBg').classList.remove('show'); }
function toast(msg,type='ok') { const t=document.getElementById('toast'); t.textContent=msg; t.className=`toast toast-${type} show`; setTimeout(()=>t.classList.remove('show'),2500); }
function esc(s) { if (!s) return ''; return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
function escJs(s) { if (!s) return ''; return String(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/"/g,'\\"').replace(/\n/g,'\\n').replace(/\r/g,'\\r'); }

// ===== 版本号 =====
async function fetchVersion() {
  try {
    const r = await fetch(`${API}/api/health`);
    const d = await r.json();
    document.getElementById('verBadge').textContent = d.version || '7.0';
  } catch(e) { document.getElementById('verBadge').textContent = '7.0'; }
}

// ===== 复制功能 =====
function copyText(id) {
  const el = document.getElementById(id);
  const text = el.value || el.textContent;
  navigator.clipboard.writeText(text).then(() => toast('已复制','ok')).catch(() => {
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta);
    ta.select(); document.execCommand('copy');
    document.body.removeChild(ta);
    toast('已复制','ok');
  });
}
function copyResult() {
  const text = document.getElementById('resultText').textContent;
  if (!text || text === '等待翻改...') { toast('没有可复制的内容','wn'); return; }
  navigator.clipboard.writeText(text).then(() => toast('已复制','ok')).catch(() => {
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta);
    ta.select(); document.execCommand('copy');
    document.body.removeChild(ta);
    toast('已复制','ok');
  });
}

// ===== 创建书籍 =====
async function createBook() {
  const title = document.getElementById('newBookTitle').value.trim();
  if (!title) { toast('请输入书名','wn'); return; }
  const author = document.getElementById('newBookAuthor').value.trim();
  const content = document.getElementById('newBookContent').value.trim();
  const chapters = content ? [{ title:'第一章', content }] : [];
  try {
    const r = await fetch(`${API}/api/books`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({title,author,chapters}) });
    const d = await r.json();
    hideModal('addBookModal');
    document.getElementById('newBookTitle').value='';
    document.getElementById('newBookAuthor').value='';
    document.getElementById('newBookContent').value='';
    toast(`「${title}」已创建`,'ok');
    await loadBooks();
    await selectBook(d.id);
  } catch(e) { toast('创建失败','err'); }
}

// ===== 添加章节 =====
async function addChapter() {
  if (!curBook) { toast('请先选择一本书','wn'); return; }
  const title = document.getElementById('newChTitle').value.trim() || `第${Date.now()%10000}章`;
  const content = document.getElementById('newChContent').value.trim();
  if (!content) { toast('请输入内容','wn'); return; }
  try {
    await fetch(`${API}/api/books/${curBook}/chapters`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({title,content}) });
    hideModal('addChModal');
    document.getElementById('newChTitle').value='';
    document.getElementById('newChContent').value='';
    toast(`「${title}」已添加`,'ok');
    const r = await fetch(`${API}/api/books/${curBook}`);
    currentBookData = await r.json();
    await loadBooks();
    curBook = currentBookData.id;
    renderBookList();
  } catch(e) { toast('添加失败','err'); }
}

// ===== 键盘快捷键 =====
document.addEventListener('keydown', e => {
  const tag = e.target.tagName;
  const isInput = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';
  // Ctrl+Enter → 翻改
  if (e.ctrlKey && e.key === 'Enter') { e.preventDefault(); doRewrite(); return; }
  // Escape → 关闭模态框
  if (e.key === 'Escape') { document.querySelectorAll('.modal-bg.show').forEach(m => m.classList.remove('show')); return; }
  // Ctrl+S → 智能保存
  if (e.ctrlKey && e.key === 's') {
    e.preventDefault();
    if (curCh) { saveChapterContent(); } else { saveDraft(); toast('草稿已保存','ok'); }
    return;
  }
  // / → 聚焦搜索
  if (e.key === '/' && !isInput) {
    e.preventDefault();
    const s = document.getElementById('bookSearch');
    if (s) { s.focus(); s.select(); }
    return;
  }
  // Ctrl+N → 新建书籍
  if (e.ctrlKey && e.key === 'n') { e.preventDefault(); showModal('addBookModal'); return; }
  // Ctrl+[ / Ctrl+] → 上一章/下一章
  if (e.ctrlKey && (e.key === '[' || e.key === ']')) {
    e.preventDefault();
    navigateChapter(e.key === ']' ? 1 : -1);
    return;
  }
  // Ctrl+→ / Ctrl+← → 下一章/上一章
  if (e.ctrlKey && e.key === 'ArrowRight') { e.preventDefault(); navigateChapter(1); return; }
  if (e.ctrlKey && e.key === 'ArrowLeft') { e.preventDefault(); navigateChapter(-1); return; }
});

// ===== 章节导航 =====
function navigateChapter(direction) {
  if (!curBook || !currentBookData || !currentBookData.chapters) return;
  const chapters = currentBookData.chapters;
  const curIdx = chapters.findIndex(c => c.id === curCh);
  if (curIdx === -1) return;
  const newIdx = curIdx + direction;
  if (newIdx < 0 || newIdx >= chapters.length) {
    toast(direction > 0 ? '已是最后一章' : '已是第一章', 'wn');
    return;
  }
  loadChapterById(curBook, chapters[newIdx].id);
  const chEl = document.querySelector('.ch.on');
  if (chEl) chEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function updateChapterNav() {
  const el = document.getElementById('chNav');
  if (!el) return;
  if (!(curCh && currentBookData && (currentBookData.chapters || []).length > 1)) {
    el.style.display = 'none'; return;
  }
  el.style.display = 'inline-flex';
  const chapters = currentBookData.chapters || [];
  const curIdx = chapters.findIndex(c => c.id === curCh);
  el.innerHTML = `<button class="ch-nav-btn" onclick="navigateChapter(-1)" ${curIdx <= 0 ? 'disabled' : ''} title="上一章 Ctrl+[">◀</button>` +
    `<span style="padding:0 4px;min-width:28px;text-align:center;color:var(--tx2)">${curIdx + 1}/${chapters.length}</span>` +
    `<button class="ch-nav-btn" onclick="navigateChapter(1)" ${curIdx >= chapters.length - 1 ? 'disabled' : ''} title="下一章 Ctrl+]">▶</button>`;
}
init();

// ============ Event Delegation ============
document.addEventListener("click", function(e) {
  var el = e.target.closest("[data-action]");
  if (!el) return;
  var action = el.dataset.action;
  var args = el.dataset.args || "";
  switch(action) {
    case "addChapter": addChapter(); break;
    case "addRule": addRule(); break;
    case "applyResultToChapter": applyResultToChapter(); break;
    case "changeFontSize": changeFontSize(parseInt(args)); break;
    case "clearRules": clearRules(); break;
    case "copyResult": copyResult(); break;
    case "copyText": copyText(args); break;
    case "createBook": createBook(); break;
    case "doRewrite": doRewrite(); break;
    case "downloadResult": downloadResult('txt'); break;
    case "downloadResultJson": downloadResult('json'); break;
    case "downloadResultEpub": downloadResultEpub(); break;
    case "extractNames": extractNames(); break;
    case "hideModal": hideModal(args); break;
    case "importFile": importFile(); break;
    case "parseBatch": parseBatch(); break;
    case "rewriteBookStream": rewriteBookStream(); break;
    case "saveApiKey": saveApiKey(); break;
    case "saveChapterContent": saveChapterContent(); break;
    case "saveTemplate": saveTemplate(); break;
    case "setViewMode": setViewMode(args); break;
    case "showModal": showModal(args); break;
    case "toggleBatch": toggleBatch(); break;
    case "togglePanel": togglePanel(args); break;
    case "toggleTheme": toggleTheme(); break;
  }
});
