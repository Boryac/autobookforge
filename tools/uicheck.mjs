// AutoBookForge 界面诊断：CDP 直连，量真实布局 + 区域截图
// 用法: node tools/uicheck.mjs <url> <outDir> <width> <height>
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const CHROME = process.env.CHROME ||
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const URL_ = process.argv[2] || 'http://127.0.0.1:8000/';
const OUT = process.argv[3] || '.';
const W = parseInt(process.argv[4] || '1600', 10);
const H = parseInt(process.argv[5] || '1000', 10);
const PORT = 9333 + (parseInt(process.env.CDP_OFFSET || '0', 10));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'abfprof-'));

const chrome = spawn(CHROME, [
  '--headless=new', '--disable-gpu', '--no-sandbox', '--disable-dev-shm-usage',
  '--hide-scrollbars', '--no-first-run', '--no-default-browser-check',
  `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  `--window-size=${W},${H}`, 'about:blank',
], { stdio: 'ignore' });

function cleanup(code) {
  try { chrome.kill(); } catch {}
  try { fs.rmSync(profile, { recursive: true, force: true }); } catch {}
  process.exit(code || 0);
}

async function findTarget() {
  for (let i = 0; i < 60; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const list = await r.json();
      const page = list.find((t) => t.type === 'page');
      if (page && page.webSocketDebuggerUrl) return page.webSocketDebuggerUrl;
    } catch {}
    await sleep(250);
  }
  throw new Error('CDP 未就绪');
}

class CDP {
  constructor(ws) {
    this.ws = ws; this.id = 0; this.pending = new Map(); this.events = [];
    ws.addEventListener('message', (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && this.pending.has(m.id)) {
        const { resolve, reject } = this.pending.get(m.id);
        this.pending.delete(m.id);
        m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
      } else if (m.method) this.events.push(m);
    });
  }
  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (this.pending.has(id)) { this.pending.delete(id); reject(new Error('timeout ' + method)); }
      }, 30000);
    });
  }
  async eval(expr) {
    const r = await this.send('Runtime.evaluate', {
      expression: expr, returnByValue: true, awaitPromise: true,
    });
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
    return r.result.value;
  }
  async shot(file, clip, scale = 2) {
    const params = { format: 'png' };
    if (clip) params.clip = { ...clip, scale };
    const r = await this.send('Page.captureScreenshot', params);
    fs.writeFileSync(file, Buffer.from(r.data, 'base64'));
    return fs.statSync(file).size;
  }
}

const MEASURE = String.raw`(() => {
  const r = (el) => { const b = el.getBoundingClientRect(); return { x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height) }; };
  const cs = (el, k) => getComputedStyle(el)[k];
  const out = { viewport: { w: innerWidth, h: innerHeight }, doc: { sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth, sh: document.documentElement.scrollHeight, ch: document.documentElement.clientHeight } };
  const side = document.querySelector('.side');
  if (side) {
    out.side = { ...r(side), clientH: side.clientHeight, scrollH: side.scrollHeight, overflowY: cs(side, 'overflowY'), display: cs(side, 'display') };
    out.sideChildren = [...side.children].map((el) => ({
      tag: el.tagName, cls: el.className || '', ...r(el),
      clientH: el.clientHeight, scrollH: el.scrollHeight,
      clipped: el.scrollHeight - el.clientHeight > 2 && cs(el, 'overflowY') !== 'visible',
      overflowY: cs(el, 'overflowY'), pos: cs(el, 'position'),
    }));
  }
  out.tiles = [...document.querySelectorAll('.tile')].map((t) => ({
    cls: t.className, ...r(t), clientH: t.clientHeight, scrollH: t.scrollHeight,
    clipped: t.scrollHeight - t.clientHeight > 2, overflowY: cs(t, 'overflowY'),
  }));
  // 相邻磁贴是否重叠
  out.overlaps = [];
  const ch = out.sideChildren || [];
  for (let i = 1; i < ch.length; i++) {
    const a = ch[i - 1], b = ch[i];
    if (b.y < a.y + a.h - 1) out.overlaps.push([a.cls || a.tag, b.cls || b.tag, (a.y + a.h) - b.y]);
  }
  // 横向溢出元素
  out.hOverflow = [...document.querySelectorAll('body *')].filter((el) => {
    const b = el.getBoundingClientRect();
    return b.right > innerWidth + 1 || b.left < -1;
  }).slice(0, 12).map((el) => ({ tag: el.tagName, cls: (el.className || '').toString().slice(0, 40), ...r(el) }));
  // 关键控件是否存在且有尺寸
  out.controls = {};
  for (const id of ['segStyle','segFontEmbed','selFontBody','selFontTitle','selFontFormat','segIndent','segOrnament','chkLayout','btnStart','btnReset','segBudget','dropzone']) {
    const el = document.getElementById(id);
    out.controls[id] = el ? { ...r(el), opts: el.tagName === 'SELECT' ? el.options.length : undefined, val: el.value || undefined } : null;
  }
  out.fontOptions = [...document.querySelectorAll('#selFontBody option')].map((o) => o.textContent);
  return JSON.stringify(out);
})()`;

(async () => {
  const wsUrl = await findTarget();
  const ws = new WebSocket(wsUrl);
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
  const cdp = new CDP(ws);
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: W, height: H, deviceScaleFactor: 1, mobile: false,
  });
  await cdp.send('Page.navigate', { url: URL_ });
  await sleep(2500);

  const data = JSON.parse(await cdp.eval(MEASURE));
  fs.writeFileSync(path.join(OUT, '_uicheck.json'), JSON.stringify(data, null, 2));

  const full = path.join(OUT, '_uicheck-full.png');
  const size = await cdp.shot(full, null, 1);
  console.log('full', size, '->', full);

  const sideR = data.side ? { x: data.side.x, y: data.side.y, width: data.side.w, height: data.side.h } : null;
  if (sideR) {
    const s = await cdp.shot(path.join(OUT, '_uicheck-side.png'), sideR, 2);
    console.log('side', s);
    // 滚到底部再截一张，验证吸顶/粘性行为与末尾内容完整
    await cdp.eval("(()=>{const e=document.querySelector('.side-scroll');if(e)e.scrollTop=e.scrollHeight;return 1})()");
    await sleep(600);
    const s2 = await cdp.shot(path.join(OUT, '_uicheck-side-bottom.png'), sideR, 2);
    console.log('side-bottom', s2);
  }
  ws.close();
  cleanup(0);
})().catch((e) => { console.error('ERR', e.message); cleanup(1); });
