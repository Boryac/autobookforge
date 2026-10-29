// 界面审查：多视口测量 + 截图，检查磁贴裁切/横向溢出/控件可达性
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const URL_ = process.argv[2] || 'http://127.0.0.1:8000/';
const OUT = process.argv[3] || '.';
const VIEWPORTS = [[1600, 1000], [1366, 768], [1280, 800]];
const PORT = 9421;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'abfaudit-'));
const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox', '--disable-dev-shm-usage',
  '--hide-scrollbars', '--no-first-run', '--no-default-browser-check',
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`, '--window-size=1600,1000', 'about:blank'], { stdio: 'ignore' });

const EXPR = String.raw`(() => {
  const r = (el) => { const b = el.getBoundingClientRect(); return { y: Math.round(b.y), bottom: Math.round(b.y + b.height), h: Math.round(b.height) }; };
  const sc = document.querySelector('.side-scroll');
  const tiles = [...document.querySelectorAll('.tile')].map((t) => {
    const cs = getComputedStyle(t);
    return { cls: t.className.replace('tile ', ''), ...r(t), clientH: t.clientHeight, scrollH: t.scrollHeight,
      clipped: t.scrollHeight - t.clientHeight > 2 && cs.overflowY !== 'visible' && cs.overflow !== 'visible' };
  });
  const ctrls = {};
  ['segStyle','segCorrect','chkFormats','segCover','segFontEmbed','selFontFormat','selFontBody','selFontTitle',
   'segIndent','segOrnament','chkLayout','btnStart','btnReset','llmToggle','segBudget','btnLlmSettings'].forEach((id) => {
    const el = document.getElementById(id);
    if (!el) { ctrls[id] = 'MISSING'; return; }
    const b = el.getBoundingClientRect();
    const inner = b.y - sc.getBoundingClientRect().y + sc.scrollTop;
    ctrls[id] = { reachable: inner + b.height <= sc.scrollHeight + 2, w: Math.round(b.width), h: Math.round(b.height) };
  });
  const hOver = [...document.querySelectorAll('body *')].filter((el) => {
    const b = el.getBoundingClientRect();
    return b.right > innerWidth + 1 || b.left < -1;
  }).map((el) => (el.className || el.tagName).toString().slice(0, 30)).slice(0, 8);
  const box = (sel) => { const el = document.querySelector(sel); return el ? r(el) : null; };
  return JSON.stringify({
    vp: { w: innerWidth, h: innerHeight },
    doc: { sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth },
    hOver, tiles, ctrls,
    side: sc ? { clientH: sc.clientHeight, scrollH: sc.scrollHeight, pinned: sc.classList.contains('steps-pinned') } : null,
    steps: box('.tile-steps'), bar: box('.action-bar'),
    fontOpts: [...document.querySelectorAll('#selFontBody option')].map((o) => o.textContent),
    fontGroups: [...document.querySelectorAll('#selFontBody optgroup')].map((g) => g.label + ':' + g.children.length),
    hintFont: (document.getElementById('hintFont') || {}).textContent || '',
  });
})()`;

class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.p = new Map();
    ws.addEventListener('message', (e) => { const m = JSON.parse(e.data);
      if (m.id && this.p.has(m.id)) { const { res, rej } = this.p.get(m.id); this.p.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); } }); }
  send(method, params = {}) { const id = ++this.id;
    return new Promise((res, rej) => { this.p.set(id, { res, rej }); this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => { if (this.p.has(id)) { this.p.delete(id); rej(new Error('timeout ' + method)); } }, 30000); }); }
  async eval(expression) { const r = await this.send('Runtime.evaluate', { expression, returnByValue: true });
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails)); return r.result.value; }
  async shot(file, clip, scale = 1) { const p = { format: 'png' }; if (clip) p.clip = { ...clip, scale };
    const r = await this.send('Page.captureScreenshot', p); fs.writeFileSync(file, Buffer.from(r.data, 'base64')); return fs.statSync(file).size; }
}

(async () => {
  let wsUrl = null;
  for (let i = 0; i < 60 && !wsUrl; i++) {
    try { const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const p = l.find((t) => t.type === 'page'); if (p) wsUrl = p.webSocketDebuggerUrl; } catch {}
    if (!wsUrl) await sleep(250);
  }
  const ws = new WebSocket(wsUrl);
  await new Promise((r, j) => { ws.addEventListener('open', r); ws.addEventListener('error', j); });
  const cdp = new CDP(ws);
  await cdp.send('Page.enable'); await cdp.send('Runtime.enable');

  const lines = [];
  for (const [W, H] of VIEWPORTS) {
    await cdp.send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });
    await cdp.send('Page.navigate', { url: URL_ });
    await sleep(2500);
    const d = JSON.parse(await cdp.eval(EXPR));
    lines.push(`===== ${W}x${H} =====`);
    lines.push(`doc sw=${d.doc.sw} cw=${d.doc.cw}  横向溢出=${d.hOver.length ? d.hOver.join(',') : '无'}`);
    lines.push(`side-scroll clientH=${d.side.clientH} scrollH=${d.side.scrollH} pinned=${d.side.pinned}`);
    d.tiles.forEach((t) => lines.push(`  ${t.cls.padEnd(14)} y=${String(t.y).padStart(5)} h=${String(t.h).padStart(4)} 内容${String(t.scrollH).padStart(5)} ${t.clipped ? '<< 被裁切' : 'ok'}`));
    const bad = Object.entries(d.ctrls).filter(([, v]) => v === 'MISSING' || !v.reachable);
    lines.push(`  控件 ${Object.keys(d.ctrls).length} 个，问题：${bad.length ? bad.map(([k, v]) => k + ':' + JSON.stringify(v)).join(' ') : '无'}`);
    lines.push(`  steps y=${d.steps.y} h=${d.steps.h}   action-bar y=${d.bar.y} h=${d.bar.h}`);
    lines.push(`  字体分组 ${d.fontGroups.join(' | ')}`);
    lines.push(`  字体提示 ${d.hintFont.slice(0, 60)}`);
    await cdp.shot(path.join(OUT, `ui-${W}x${H}.png`), null, 1);
  }

  // 运行中状态：吸顶 + 半程滚动
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });
  await cdp.send('Page.navigate', { url: URL_ });
  await sleep(2200);
  await cdp.eval("(()=>{const s=document.querySelector('.side-scroll');s.classList.add('steps-pinned');s.scrollTop=620;return 1})()");
  await sleep(600);
  const run = JSON.parse(await cdp.eval(EXPR));
  lines.push('', '===== 运行中（吸顶 + 滚动 620px）=====', `steps y=${run.steps.y} h=${run.steps.h}  bar y=${run.bar.y} h=${run.bar.h} pinned=${run.side.pinned}`);
  await cdp.shot(path.join(OUT, 'ui-running.png'), null, 1);
  const bars = fs.readdirSync(OUT).filter((f) => f.startsWith('ui-'));
  lines.push('截图：' + bars.join(', '));
  fs.writeFileSync(path.join(OUT, '_uiaudit.txt'), lines.join('\n'), 'utf-8');
  ws.close();
  try { chrome.kill(); } catch {}
  try { fs.rmSync(profile, { recursive: true, force: true }); } catch {}
  process.exit(0);
})().catch((e) => { console.error('ERR', e.message); try { chrome.kill(); } catch {} process.exit(1); });
