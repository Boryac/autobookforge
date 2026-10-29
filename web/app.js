/* AutoBookForge — Web UI 交互逻辑（原生 JS，零构建） */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = { file: null, jobId: null, result: null, timer: null, llm: null, llmEnabled: false };

const STAGE_STEP = {
  encoding: 0, ad_filter_llm: 0, cleaning: 0, splitting: 1, title_llm: 1,
  correcting: 2, proof_llm: 2, toc_llm: 2, cover: 4, fonts: 4, epub: 5, convert: 5,
};

/* ---------------- 工具 ---------------- */
function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

let toastTimer = null;
function toast(msg, ms) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('on'), ms || 2600);
}

function setSteps(activeIndex, doneUpTo) {
  $$('#steps li').forEach((li, i) => {
    li.classList.toggle('active', i === activeIndex);
    li.classList.toggle('done', doneUpTo != null && i <= doneUpTo && i !== activeIndex);
  });
}

function setStatus(text, live) {
  $('#statusText').textContent = text;
  $('#liveDot').classList.toggle('idle', !live);
  const sc = $('.side-scroll');
  if (sc) sc.classList.toggle('steps-pinned', !!live);
}

function setProgress(progress, stageLabel) {
  const pct = Math.round((progress || 0) * 100);
  $('#progressFill').style.width = pct + '%';
  $('#progressPct').textContent = pct + '%';
  if (stageLabel) $('#progressStage').textContent = stageLabel;
}

/* ---------------- 设置读取 ---------------- */
function segValue(sel) {
  const on = $(sel + ' button.on');
  return on ? on.dataset.value : null;
}

function getSettings() {
  const formats = $$('#chkFormats input:checked').map(i => i.value);
  const coverOn = segValue('#segCover') !== 'off';
  return {
    title: $('#inpTitle').value.trim(),
    author: $('#inpAuthor').value.trim(),
    style: segValue('#segStyle') || 'literary',
    correct_level: parseInt(segValue('#segCorrect') || '2', 10),
    cover: coverOn,
    cover_style: segValue('#segStyle') || 'auto',
    formats: formats.length ? formats : ['epub'],
    llm: !!state.llmEnabled && !!(state.llm && state.llm.configured),
    llm_budget: segValue('#segBudget') || (state.llm && state.llm.budget) || 'standard',
    font_embed: segValue('#segFontEmbed') || 'body',
    font_body: valOf('#selFontBody'),
    font_title: valOf('#selFontTitle'),
    font_format: valOf('#selFontFormat') || 'woff2',
    indent_mode: segValue('#segIndent') || 'space',
    page_ornament: segValue('#segOrnament') || 'chapter',
    title_page: !!($('#chkTitlePage') && $('#chkTitlePage').checked),
    vertical: !!($('#chkVertical') && $('#chkVertical').checked),
  };
}

/* ---------------- 文件选择 ---------------- */
function acceptFile(file) {
  if (!file) return;
  if (!/\.(txt|text)$/i.test(file.name)) { toast('仅支持 TXT 文件'); return; }
  state.file = file;
  $('#uploadHint').textContent = `已选择：${file.name}　${(file.size / 1024).toFixed(1)} KB`;
  setSteps(0, -1);
  setStatus('文件已就绪', false);
  toast('文件已就绪，可点击「开始处理」');
}

/* ---------------- 开始处理 ---------------- */
async function startConvert() {
  if (!state.file) { toast('请先选择 TXT 文件'); return; }
  const s = getSettings();
  const fd = new FormData();
  fd.append('file', state.file);
  fd.append('style', s.style);
  fd.append('correct_level', String(s.correct_level));
  fd.append('cover', String(s.cover));
  fd.append('cover_style', s.cover_style);
  fd.append('title', s.title);
  fd.append('author', s.author);
  fd.append('formats', s.formats.join(','));
  fd.append('llm', String(!!s.llm));
  fd.append('llm_budget', s.llm_budget);
  fd.append('font_embed', s.font_embed);
  fd.append('font_body', s.font_body);
  fd.append('font_title', s.font_title);
  fd.append('font_format', s.font_format);
  fd.append('indent_mode', s.indent_mode);
  fd.append('page_ornament', s.page_ornament);
  fd.append('title_page', String(s.title_page));
  fd.append('vertical', String(s.vertical));

  $('#btnStart').disabled = true;
  setProgress(0, '提交任务');
  setStatus('正在处理', true);
  try {
    const res = await fetch('/api/upload', { method: 'POST', body: fd });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || ('HTTP ' + res.status));
    }
    const data = await res.json();
    state.jobId = data.job_id;
    poll();
  } catch (e) {
    $('#btnStart').disabled = false;
    setStatus('提交失败', false);
    toast('提交失败：' + e.message);
  }
}

/* ---------------- 轮询进度 ---------------- */
async function poll() {
  if (!state.jobId) return;
  try {
    const res = await fetch('/api/jobs/' + state.jobId);
    const job = await res.json();
    const stepIdx = STAGE_STEP[job.stage] != null ? STAGE_STEP[job.stage] : 0;
    setSteps(stepIdx, stepIdx - 1);
    setProgress(job.progress, job.stage_label || '处理中');
    setStatus(job.message || '处理中', true);

    if (job.status === 'done') {
      state.result = job.result;
      setProgress(1, '处理完成');
      setSteps(5, 5);
      setStatus('处理完成', false);
      $('#btnStart').disabled = false;
      renderResult(job.result);
      toast('处理完成，共 ' + (job.result.chapter_count || 0) + ' 章');
      return;
    }
    if (job.status === 'error') {
      $('#btnStart').disabled = false;
      setStatus('处理失败', false);
      toast('处理失败：' + (job.error || '未知错误'), 4200);
      return;
    }
    state.timer = setTimeout(poll, 500);
  } catch (e) {
    state.timer = setTimeout(poll, 1200);
  }
}

/* ---------------- 结果渲染 ---------------- */
function renderResult(result) {
  $('#viewUpload').classList.add('hidden');
  $('#viewResult').classList.remove('hidden');

  $('#metaTitle').textContent = result.book_title || '未命名书籍';
  $('#metaAuthor').textContent = (result.author || '佚名') + '　·　' + (result.encoding || '');
  $('#metaChars').textContent = (result.total_chars || 0).toLocaleString() + ' 字';

  const rep = result.report || {};
  const bySource = (rep.summary && rep.summary.by_source) || {};
  $('#stChapters').textContent = result.chapter_count || 0;
  $('#stRule').textContent = bySource.rule || 0;
  $('#stNgram').textContent = bySource.ngram || 0;
  $('#stTime').textContent = (result.elapsed || 0).toFixed(2);

  renderMeta(rep.meta || {}, rep.split_quality || {}, rep.clean_stats || {});
  renderTypo(rep.typography || {});
  renderChapters(rep.chapter_index || []);
  renderDiff(rep.chapters || []);
  renderCover(result);
  renderFiles(result);
  const llmRep = rep.llm || {};
  const llmOn = !!(llmRep.active || (llmRep.tasks && Object.keys(llmRep.tasks).length));
  $('#tabLlm').hidden = !llmOn;
  renderLlmReport(llmRep);
  switchTab('chapters');
}

function renderMeta(meta, quality, clean) {
  const ml = $('#metaLine');
  const bits = [];
  if (meta.author) bits.push('作者 <b>' + escapeHtml(meta.author) + '</b>');
  if (meta.site) bits.push('来源水印 <b>' + escapeHtml(meta.site) + '</b>');
  if (meta.consumed_lines) bits.push('摘除元信息行 <b>' + meta.consumed_lines + '</b>');
  (meta.tags || []).forEach(t => bits.push('<span class="tag">' + escapeHtml(t) + '</span>'));
  ml.innerHTML = bits.join('　');
  ml.hidden = bits.length === 0;

  const sg = $('#structGrid');
  if (quality && quality.chapters) {
    $('#stVolumes').textContent = quality.volumes || 0;
    $('#stSections').textContent = quality.sections || 0;
    $('#stMedian').textContent = quality.median_len || 0;
    $('#stCoverage').textContent = Math.round((quality.coverage || 0) * 100) + '%';
    sg.hidden = false;
  } else {
    sg.hidden = true;
  }

  const cl = $('#cleanLine');
  if (clean && clean.lines_in) {
    cl.innerHTML =
      '水印整行删除 <b>' + (clean.ads_removed || 0) + '</b>　' +
      '段内水印剥离 <b>' + (clean.inline_ads_stripped || 0) + '</b>　' +
      '元信息行 <b>' + (clean.meta_removed || 0) + '</b>　' +
      '标题保护 <b>' + (clean.headings_protected || 0) + '</b>';
    cl.hidden = false;
  } else {
    cl.hidden = true;
  }
}

function renderChapters(list) {
  const ul = $('#chapterList');
  if (!list.length) {
    ul.innerHTML = '<li><span class="name">未识别到章节结构（整书作为单章处理）</span></li>';
    return;
  }
  ul.innerHTML = list.map((c, i) => {
    const lv = c.level === 1 ? '卷' : (c.level === 3 ? '节' : '章');
    return `
    <li class="lv-${c.level || 2}">
      <span class="idx">${String(i + 1).padStart(2, '0')}</span>
      <span class="name">${escapeHtml(c.title || ('第 ' + (i + 1) + ' 章'))}</span>
      <span class="badge">${lv}</span>
      <span class="len">${c.chars || 0} 字</span>
    </li>`;
  }).join('');
}

function segsToOriginal(segs) {
  return segs.map(s => s.changed
    ? `<del class="abf-del" title="${escapeHtml(s.note)}">${escapeHtml(s.text)}</del>`
    : escapeHtml(s.text)).join('');
}

function segsToCorrected(segs) {
  return segs.map(s => s.changed
    ? `<ins class="abf-ins" data-source="${escapeHtml(s.source)}" title="${escapeHtml(s.note)}">${escapeHtml(s.replacement)}</ins>`
    : escapeHtml(s.text)).join('');
}

function renderDiff(chapterReports) {
  const withFix = chapterReports.filter(c => c.segments && c.segments.length);
  if (!withFix.length) {
    $('#diffOriginal').innerHTML = '<span style="color:#a3a69b">本次未发现需要修正的内容。</span>';
    $('#diffCorrected').innerHTML = '<span style="color:#a3a69b">文本已通过规则层与统计层校验。</span>';
    $('#diffSummary').textContent = '未发现修正';
    return;
  }
  const origHtml = [];
  const corrHtml = [];
  withFix.forEach(c => {
    origHtml.push(`<div class="diff-chapter">${escapeHtml(c.title)}</div>`);
    corrHtml.push(`<div class="diff-chapter">${escapeHtml(c.title)}</div>`);
    origHtml.push(segsToOriginal(c.segments));
    corrHtml.push(segsToCorrected(c.segments));
  });
  $('#diffOriginal').innerHTML = origHtml.join('<br>');
  $('#diffCorrected').innerHTML = corrHtml.join('<br>');

  const total = withFix.reduce((a, c) => a + (c.count || 0), 0);
  $('#diffSummary').textContent = `共 ${withFix.length} 个章节存在修正，合计 ${total} 处`;
}

function renderCover(result) {
  const img = $('#coverImg');
  if (result.cover_path) {
    img.src = `/api/jobs/${state.jobId}/cover?t=${Date.now()}`;
    img.style.display = '';
  } else {
    img.removeAttribute('src');
    img.style.display = 'none';
    $('#panelCover').innerHTML = '<div class="cover-preview"><span style="color:#a3a69b;font-size:13px">未生成封面</span></div>';
  }
}

function renderFiles(result) {
  const ul = $('#fileList');
  const files = result.output_files || {};
  const keys = Object.keys(files);
  if (!keys.length) {
    ul.innerHTML = '<li><span class="fname">无产出文件</span></li>';
    return;
  }
  ul.innerHTML = keys.map(fmt => `
    <li>
      <span class="fmt">${fmt.toUpperCase()}</span>
      <span class="fname">${escapeHtml(files[fmt].split(/[\\/]/).pop())}</span>
      <a href="/api/jobs/${state.jobId}/download?fmt=${encodeURIComponent(fmt)}">下载</a>
    </li>`).join('');
}

/* ---------------- Tabs ---------------- */
function switchTab(name) {
  $$('.tab').forEach(t => t.classList.toggle('on', t.dataset.tab === name));
  ['chapters', 'diff', 'cover', 'files', 'llm'].forEach(k => {
    const p = $('#panel' + k.charAt(0).toUpperCase() + k.slice(1));
    if (p) p.classList.toggle('hidden', k !== name);
  });
}

/* ---------------- 预览纠错（无需上传文件） ---------------- */
const DEMO_TEXT =
  '这一章的内容做为铺垫，按耐不住的心情写在字里行间。\n' +
  '他迫不急待地想要出发，却又一如继往地犹豫。\n' +
  '他想要题高自己的武功境界，却始终不得其法。\n' +
  '张三丰站在山巅，俯瞰着脚下\n的云海。';

async function previewCorrect(text) {
  if (!text || !text.trim()) { toast('文本为空'); return; }
  try {
    const res = await fetch('/api/preview-correct', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, level: parseInt(segValue('#segCorrect') || '2', 10) }),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const report = await res.json();
    $('#viewUpload').classList.add('hidden');
    $('#viewResult').classList.remove('hidden');
    $('#metaTitle').textContent = '文本纠错预览';
    $('#metaAuthor').textContent = '规则层 + 统计层';
    const s = report.summary || {};
    $('#metaChars').textContent = (s.corrected_chars || 0) + ' 字';
    const bs = (s.by_source) || {};
    $('#stChapters').textContent = '—';
    $('#stRule').textContent = bs.rule || 0;
    $('#stNgram').textContent = bs.ngram || 0;
    $('#stTime').textContent = '0.00';
    renderChapters([]);
    state.jobId = null;
    $('#diffOriginal').innerHTML = segsToOriginal(report.segments || []);
    $('#diffCorrected').innerHTML = segsToCorrected(report.segments || []);
    $('#diffSummary').textContent = `共 ${s.total_corrections || 0} 处修正`;
    $('#panelFiles').innerHTML = '<ul class="file-list"><li><span class="fname">预览模式不产出文件</span></li></ul>';
    $('#tabLlm').hidden = true;
    $('#panelCover').innerHTML = '<div class="cover-preview"><span style="color:#a3a69b;font-size:13px">预览模式不生成封面</span></div>';
    switchTab('diff');
    setStatus('预览完成', false);
  } catch (e) {
    toast('预览失败：' + e.message);
  }
}

/* ---------------- 报告导出 ---------------- */
function downloadReport(kind) {
  if (!state.jobId) { toast('请先完成一次完整处理'); return; }
  const url = `/api/jobs/${state.jobId}/report?format=${kind}`;
  if (kind === 'md') {
    window.open(url, '_blank');
    return;
  }
  fetch(url).then(r => r.json()).then(data => {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'autobookforge_report.json';
    a.click();
    URL.revokeObjectURL(a.href);
  }).catch(() => toast('导出失败'));
}

/* ---------------- 事件绑定 ---------------- */
function bind() {
  const dz = $('#dropzone');
  const input = $('#fileInput');

  dz.addEventListener('click', () => input.click());
  dz.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') input.click(); });
  input.addEventListener('change', () => acceptFile(input.files[0]));

  ['dragenter', 'dragover'].forEach(ev => dz.addEventListener(ev, e => {
    e.preventDefault(); dz.classList.add('dragover');
  }));
  ['dragleave', 'drop'].forEach(ev => dz.addEventListener(ev, e => {
    e.preventDefault(); dz.classList.remove('dragover');
  }));
  dz.addEventListener('drop', e => {
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    acceptFile(f);
  });
  window.addEventListener('dragover', e => e.preventDefault());
  window.addEventListener('drop', e => e.preventDefault());

  $('#btnStart').addEventListener('click', startConvert);
  $('#btnReset').addEventListener('click', () => {
    state.file = null; state.jobId = null; state.result = null;
    $('#uploadHint').textContent = '尚未选择文件';
    $('#viewUpload').classList.remove('hidden');
    $('#viewResult').classList.add('hidden');
    input.value = '';
    setSteps(0, -1); setProgress(0, '就绪'); setStatus('等待任务', false);
  });

  $('#btnDemo').addEventListener('click', () => previewCorrect(DEMO_TEXT));
  $('#btnPaste').addEventListener('click', () => {
    const t = window.prompt('粘贴需要纠错的文本：');
    if (t) previewCorrect(t);
  });

  $('#btnReportJson').addEventListener('click', () => downloadReport('json'));
  $('#btnReportMd').addEventListener('click', () => downloadReport('md'));

  $$('.tab').forEach(t => t.addEventListener('click', () => switchTab(t.dataset.tab)));

  $$('.seg').forEach(seg => seg.addEventListener('click', e => {
    const btn = e.target.closest('button');
    if (!btn) return;
    $$('button', seg).forEach(b => b.classList.remove('on'));
    btn.classList.add('on');
  }));

  bindTypography();
  bindLlm();
  loadFonts();

  setSteps(0, -1);
  setStatus('等待任务', false);
}

/* ---------------- 精排与字体 ---------------- */
const INDENT_HINTS = {
  space: '全角空格：所有阅读器表现一致，但不随字号缩放',
  css: 'CSS 2em：随字号缩放（更正确），老 Kindle 可能丢失缩进',
  spacing: '段间距：零缩进 + 段间留白，西式排版',
};

const ORNAMENT_HINTS = {
  none: '不添加装饰符号',
  chapter: '每章开头一个装饰符号',
  segment: '按字数切出逻辑页，每页顶部一个纹样（与阅读器真实分页不重合）',
};

function valOf(sel) {
  const el = $(sel);
  return el ? el.value : '';
}

function bindTypography() {
  document.addEventListener('click', (e) => {
    const b = e.target.closest('#segIndent button, #segOrnament button');
    if (!b) return;
    const box = b.parentElement;
    if (!box) return;
    if (box.id === 'segIndent') {
      const h = $('#hintIndent');
      if (h) h.textContent = INDENT_HINTS[b.dataset.value] || '';
    } else if (box.id === 'segOrnament') {
      const h = $('#hintOrnament');
      if (h) h.textContent = ORNAMENT_HINTS[b.dataset.value] || '';
    }
  });
}

async function loadFonts() {
  try {
    const res = await fetch('/api/fonts');
    if (!res.ok) return;
    fillFontSelects(await res.json());
  } catch (e) { /* 离线打开时忽略 */ }
}

let FONT_LIST = [];
const FONT_ROLE_ORDER = ['正文', '章题', '引文'];

function fillFontSelects(data) {
  const list = (data && data.fonts) || [];
  FONT_LIST = list.filter(f => f.role !== 'unused');
  const groups = new Map();
  FONT_LIST.forEach((f) => {
    const k = f.role_label || '其他';
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(f);
  });
  const keys = [...groups.keys()].sort((a, b) => {
    const ia = FONT_ROLE_ORDER.indexOf(a), ib = FONT_ROLE_ORDER.indexOf(b);
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
  });
  let html = '<option value="">随风格自动选择</option>';
  keys.forEach((k) => {
    html += '<optgroup label="' + escapeHtml(k) + '">' +
      groups.get(k).map(f => '<option value="' + f.id + '">' + escapeHtml(f.label) + '</option>').join('') +
      '</optgroup>';
  });
  const pairs = [
    ['#selFontBody', (data && data.roles && data.roles.body) || ''],
    ['#selFontTitle', (data && data.roles && data.roles.title) || ''],
  ];
  pairs.forEach((p) => {
    const el = $(p[0]);
    if (!el) return;
    el.innerHTML = html;
    if (p[1] && FONT_LIST.some(f => f.id === p[1])) el.value = p[1];
    el.addEventListener('change', updateFontHint);
  });
  updateFontHint();
}

function updateFontHint() {
  const el = $('#hintFont');
  if (!el) return;
  const pick = (sel) => FONT_LIST.find(f => f.id === (($(sel) || {}).value || ''));
  const b = pick('#selFontBody');
  const t = pick('#selFontTitle');
  const bits = [];
  if (b) bits.push('正文 ' + b.label + ' ' + b.size_mb + 'MB');
  if (t) bits.push('章题 ' + t.label + ' ' + t.size_mb + 'MB');
  el.textContent = bits.length
    ? bits.join('　') + '　子集化后嵌入，按本书用字裁剪'
    : '随风格自动选择：由排版风格决定字体体系，按本书用字子集化后嵌入';
}

function renderTypo(typo) {
  const ml = $('#metaLine');
  if (!ml || !typo || !Object.keys(typo).length) return;
  const f = typo.fonts || {};
  const faces = f.faces || [];
  const bits = [
    '精排 <b>' + escapeHtml(typo.style || '-') + '</b>',
    '缩进 <b>' + escapeHtml(typo.indent_mode || '-') + '</b>',
    '纹样 <b>' + escapeHtml(typo.page_ornament || '-') + '</b>',
  ];
  if (faces.length) {
    bits.push('内嵌字体 <b>' + faces.map(x => escapeHtml(x.family)).join('、') + '</b>');
    bits.push('增量 <b>' + (f.total_out_mb || 0).toFixed(2) + ' MB</b>');
  } else {
    bits.push('内嵌字体 <b>无（系统回退）</b>');
  }
  if (typo.vertical) bits.push('<b>竖排</b>');
  const span = document.createElement('span');
  span.className = 'typo-line';
  span.innerHTML = bits.join('　');
  ml.appendChild(document.createElement('br'));
  ml.appendChild(span);
  ml.hidden = false;
}

/* ==================== LLM 增强 ==================== */
const LLM_TASK_ORDER = ['chapter_titles', 'blank_titles', 'cover_params', 'proofread', 'ad_filter', 'toc', 'spelling', 'grammar'];

function llmSetState(data) {
  state.llm = data || {};
  state.llmEnabled = !!(data && data.enabled);
  const configured = !!(data && data.configured);
  const el = $('#llmState');
  el.textContent = configured ? (data.active ? '已启用' : '已配置未启用') : '未配置';
  el.classList.toggle('on', !!(data && data.active));
  el.classList.toggle('err', !configured);
  const meta = $('#llmMeta');
  meta.textContent = configured
    ? `${data.model || '未指定模型'}　·　${data.base_url || ''}　·　密钥 ${data.api_key || '未设置'}`
    : '未填写 API 地址与密钥，LLM 增强会自动跳过';
  const sw = $('#llmToggle');
  sw.setAttribute('aria-checked', configured && data && data.enabled ? 'true' : 'false');
  if (data && data.budget) {
    $$('#segBudget button').forEach(b => b.classList.toggle('on', b.dataset.value === data.budget));
  }
}

async function loadLlmSettings() {
  try {
    const res = await fetch('/api/llm/settings');
    if (!res.ok) return;
    const data = await res.json();
    llmSetState(data);
    fillLlmModal(data);
  } catch (e) { /* 静默 */ }
}

function fillLlmModal(data) {
  if (!data) return;
  $('#inpBaseUrl').value = data.base_url || '';
  $('#inpApiKey').value = data.api_key || '';
  $('#inpModel').value = data.model || '';
  $('#inpTemp').value = data.temperature != null ? data.temperature : 0.3;
  $('#inpMaxTokens').value = data.max_tokens || 2048;
  $('#inpTimeout').value = data.timeout || 60;
  $('#inpRetries').value = data.retries != null ? data.retries : 2;
  $('#chkJsonMode').checked = data.json_mode !== false;
  $('#chkPrefix').checked = data.prefix_cache !== false;
  $('#chkLocalCache').checked = data.use_cache !== false;
  fillThinkingSelect(data);
  $('#llmSavedPath').textContent = data.saved_path ? ('配置写入：' + data.saved_path) : '';
  renderLlmTasks(data);
  renderLlmStrategies(data.strategies || []);
}

function fillThinkingSelect(data) {
  const sel = $('#selThinking');
  if (!sel) return;
  const modes = data.thinking_modes || [];
  state.thinkingModes = modes.map(m => m.id);
  sel.innerHTML = modes.map(m =>
    `<option value="${escapeHtml(m.id)}">${escapeHtml(m.name)}</option>`).join('');
  sel.value = data.thinking && modes.some(m => m.id === data.thinking) ? data.thinking : 'auto';
  const hint = $('#thinkingHint');
  if (hint) hint.textContent = data.thinking_hint || '';
}

function renderLlmTasks(data) {
  const labels = data.task_labels || {};
  const descs = data.task_descriptions || {};
  const tasks = data.tasks || {};
  const ids = data.task_ids || LLM_TASK_ORDER;
  $('#llmTaskGrid').innerHTML = ids.map(id => `
    <div class="task-item">
      <label class="check"><input type="checkbox" data-task="${escapeHtml(id)}" ${tasks[id] ? 'checked' : ''}><i></i><span class="t-name">${escapeHtml(labels[id] || id)}</span></label>
      <span class="t-desc">${escapeHtml(descs[id] || '')}</span>
    </div>`).join('');
}

function renderLlmStrategies(list) {
  $('#llmStrategyGrid').innerHTML = (list || []).map(s =>
    `<div class="strategy"><b>${escapeHtml(s.name)}</b><span>${escapeHtml(s.effect || '')}</span></div>`).join('');
}

function collectLlmPayload() {
  const tasks = {};
  $$('#llmTaskGrid input[data-task]').forEach(i => { tasks[i.dataset.task] = i.checked; });
  return {
    enabled: $('#llmToggle').getAttribute('aria-checked') === 'true',
    base_url: $('#inpBaseUrl').value.trim(),
    api_key: $('#inpApiKey').value.trim(),
    model: $('#inpModel').value.trim(),
    temperature: parseFloat($('#inpTemp').value || '0.3'),
    max_tokens: parseInt($('#inpMaxTokens').value || '2048', 10),
    timeout: parseInt($('#inpTimeout').value || '60', 10),
    retries: parseInt($('#inpRetries').value || '2', 10),
    concurrency: (state.llm && state.llm.concurrency) || 4,
    budget: segValue('#segBudget') || 'standard',
    json_mode: $('#chkJsonMode').checked,
    prefix_cache: $('#chkPrefix').checked,
    use_cache: $('#chkLocalCache').checked,
    thinking: ($('#selThinking') && $('#selThinking').value) || 'auto',
    tasks,
  };
}

async function saveLlmSettings() {
  const btn = $('#btnSaveLlm');
  btn.disabled = true;
  try {
    const res = await fetch('/api/llm/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(collectLlmPayload()),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    llmSetState(data);
    fillLlmModal(data);
    toast('设置已保存');
  } catch (e) {
    toast('保存失败：' + e.message);
  } finally {
    btn.disabled = false;
  }
}

function llmTestPayload() {
  return {
    base_url: $('#inpBaseUrl').value.trim(),
    api_key: $('#inpApiKey').value.trim(),
    model: $('#inpModel').value.trim(),
    timeout: parseInt($('#inpTimeout').value || '60', 10),
    thinking: ($('#selThinking') && $('#selThinking').value) || 'auto',
  };
}

async function testLlm() {
  const box = $('#llmTestResult');
  box.hidden = false;
  box.className = 'test-result';
  box.textContent = '正在测试连通性…';
  try {
    const res = await fetch('/api/llm/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(llmTestPayload()),
    });
    const data = await res.json();
    box.className = 'test-result ' + (data.ok ? 'ok' : 'bad');
    box.textContent = (data.ok ? '连通正常' : '连接失败') + '　' + (data.detail || '') + '　' + (data.latency_ms || 0) + ' ms';
  } catch (e) {
    box.className = 'test-result bad';
    box.textContent = '测试失败：' + e.message;
  }
}

async function fetchModels() {
  const box = $('#llmTestResult');
  box.hidden = false;
  box.className = 'test-result';
  box.textContent = '正在拉取模型列表…';
  try {
    const res = await fetch('/api/llm/models', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(llmTestPayload()),
    });
    const data = await res.json();
    const models = data.models || [];
    $('#modelOptions').innerHTML = models.map(m => `<option value="${escapeHtml(m)}"></option>`).join('');
    box.className = 'test-result ' + (data.ok ? 'ok' : 'bad');
    box.textContent = data.ok ? `共 ${models.length} 个模型，可在「模型名称」输入框下拉选择` : ('拉取失败：' + (data.detail || ''));
  } catch (e) {
    box.className = 'test-result bad';
    box.textContent = '拉取失败：' + e.message;
  }
}

function openLlmModal() { $('#llmModal').hidden = false; }
function closeLlmModal() { $('#llmModal').hidden = true; }

function llmDeepLink() {
  try {
    if (new URLSearchParams(location.search).get('settings') === '1') openLlmModal();
  } catch (e) { /* 忽略 */ }
}

const COST_LABELS = {
  batch_merge: '批量合并', rules_first: '规则前置', digest_input: '摘要输入',
  patch_output: '修改点输出', prefix_cache: '前缀缓存',
  schema_retry: '结构化输出（一次通过率）', local_cache: '本地缓存',
};

function renderLlmReport(rep) {
  const tasks = rep.tasks || {};
  const usage = rep.usage || {};
  const cost = rep.cost || {};
  const active = !!rep.active;

  $('#llmReportMeta').textContent = active
    ? `${rep.model || ''}　预算档位 ${rep.budget_label || ''}　调用 ${usage.calls || 0} 次`
    : (rep.enabled ? '未启用：缺少 API 地址 / 密钥 / 模型名' : '本次运行未启用');
  const tokens = (usage.prompt_tokens || 0) + (usage.completion_tokens || 0);
  $('#llmReportTokens').textContent = tokens.toLocaleString() + ' token';

  const labelMap = (state.llm && state.llm.task_labels) || {};
  const rows = Object.keys(tasks).map(id => {
    const t = tasks[id] || {};
    const status = t.skipped
      ? `<span class="skipped">${escapeHtml(t.skipped)}</span>`
      : `调用 ${t.calls || 0} 次　送检 ${t.items_sent || 0} / ${t.items_total || 0}　生效 ${t.applied || 0}`;
    return `<tr><td>${escapeHtml(labelMap[id] || id)}</td><td>${status}</td></tr>`;
  });
  $('#llmTaskTable').innerHTML = rows.length
    ? `<tr><th>任务</th><th>执行情况</th></tr>${rows.join('')}`
    : '<tr><td>本次运行没有产生 LLM 任务记录</td></tr>';

  const costRows = Object.keys(cost).map(k => {
    const c = cost[k] || {};
    let main = '';
    let ratio = null;
    if (k === 'prefix_cache') { ratio = c.hit_ratio; main = `命中 ${c.cached_tokens || 0} / 输入 ${c.prompt_tokens || 0}`; }
    else if (k === 'schema_retry') { ratio = 1 - (c.retry_ratio || 0); main = `重试 ${c.retries || 0} / 调用 ${c.calls || 0}`; }
    else if (k === 'local_cache') { main = `命中 ${c.hits || 0} 次`; }
    else if (k === 'rules_first') { ratio = c.saved_ratio; main = `规则消化 ${c.handled_by_rules || 0} / ${c.total || 0}`; }
    else if (k === 'batch_merge') { ratio = c.saved_ratio; main = `请求 ${c.calls || 0} / 逐条 ${c.sequential_baseline || 0}`; }
    else { ratio = c.saved_ratio; main = `基线 ${c.baseline_tokens || 0} / 实际 ${c.actual_prompt_tokens || c.actual_completion_tokens || 0}`; }
    const pct = ratio != null ? Math.round(ratio * 100) + '%' : '—';
    const barW = ratio != null ? Math.max(2, Math.round(ratio * 88)) : 0;
    const cell = ratio != null ? `<span class="bar" style="width:${barW}px"></span> ${pct}` : pct;
    return `<tr><td>${COST_LABELS[k] || k}<div class="llm-note">${escapeHtml(c.note || '')}</div></td><td>${main}</td><td class="num">${cell}</td></tr>`;
  });
  $('#llmCostTable').innerHTML = costRows.length
    ? `<tr><th>降本机制</th><th>核算口径</th><th class="num">指标</th></tr>${costRows.join('')}`
    : '<tr><td>未采集到降本数据</td></tr>';

  const notes = rep.notes || [];
  $('#llmNotes').innerHTML = notes.length
    ? notes.map(n => `<li>${escapeHtml(n)}</li>`).join('')
    : '<li>无</li>';
}

function bindLlm() {
  $('#btnLlmSettings').addEventListener('click', openLlmModal);
  $$('#llmModal [data-close]').forEach(el => el.addEventListener('click', closeLlmModal));
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeLlmModal(); });
  $('#btnSaveLlm').addEventListener('click', saveLlmSettings);
  $('#btnTestLlm').addEventListener('click', testLlm);
  $('#btnFetchModels').addEventListener('click', fetchModels);
  $('#llmToggle').addEventListener('click', () => {
    const el = $('#llmToggle');
    const on = el.getAttribute('aria-checked') === 'true';
    el.setAttribute('aria-checked', on ? 'false' : 'true');
    state.llmEnabled = !on;
  });
  loadLlmSettings();
  llmDeepLink();
}

document.addEventListener('DOMContentLoaded', bind);
