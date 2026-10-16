// 用 K3 自带的 Chromium 做无头检查的公共部分：启动浏览器、发送 DevTools 指令、收集控制台错误。
// 依赖 Node 22+ 自带的 fetch 与 WebSocket；Chromium 路径可用 CHROMIUM 指定。
import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function openBrowser(port = 9334) {
  const chromium = process.env.CHROMIUM || '/usr/lib/chromium/chromium';
  const profile = mkdtempSync(join(tmpdir(), 'mutouren-check-'));
  const browser = spawn(chromium, ['--headless=new', '--no-sandbox', '--disable-gpu', '--hide-scrollbars',
    `--user-data-dir=${profile}`, `--remote-debugging-port=${port}`, '--autoplay-policy=no-user-gesture-required',
    'about:blank'], { stdio: 'ignore' });

  let page = null;
  for (let i = 0; i < 40 && !page; i += 1) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
      page = list.find((t) => t.type === 'page');
    } catch { /* 浏览器还没起来 */ }
    if (!page) await sleep(500);
  }
  if (!page) throw new Error('Chromium 没有启动');

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((r) => { ws.onopen = r; });
  let id = 0;
  const pending = new Map();
  const problems = [];
  ws.onmessage = (message) => {
    const data = JSON.parse(message.data);
    if (data.id && pending.has(data.id)) {
      pending.get(data.id)(data.result);
      pending.delete(data.id);
    } else if (data.method === 'Runtime.exceptionThrown') {
      const d = data.params.exceptionDetails;
      problems.push(`异常：${d.exception?.description || d.text}`);
    } else if (data.method === 'Runtime.consoleAPICalled' && ['error', 'warning'].includes(data.params.type)) {
      problems.push(`控制台 ${data.params.type}：${data.params.args.map((a) => a.value ?? a.description).join(' ')}`);
    } else if (data.method === 'Log.entryAdded' && data.params.entry.level === 'error') {
      problems.push(`日志：${data.params.entry.text} ${data.params.entry.url || ''}`);
    }
  };
  const send = (method, params = {}) => new Promise((resolve) => {
    id += 1;
    pending.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params }));
  });
  await send('Runtime.enable');
  await send('Log.enable');
  await send('Page.enable');

  return {
    send,
    problems,
    async evaluate(expression) {
      const result = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
      return result.result?.value;
    },
    async screenshot() {
      const shot = await send('Page.captureScreenshot', { format: 'png' });
      return Buffer.from(shot.data, 'base64');
    },
    async viewport(width, height) {
      await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile: false });
    },
    async close() {
      ws.close();
      browser.kill();
      await sleep(300);
      rmSync(profile, { recursive: true, force: true });
    },
  };
}
