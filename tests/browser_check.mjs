// 浏览器检查：用 K3 自带的 Chromium 打开玩家大屏和调试页，收集控制台错误并截图。服务需要已在运行。
// 截图含实时摄像头画面，看完请删除。
// 用法：node tests/browser_check.mjs [输出目录] [地址]
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { openBrowser, sleep } from './cdp.mjs';

const out = process.argv[2] || '/tmp';
const base = process.argv[3] || 'http://127.0.0.1:8322';
const browser = await openBrowser();

for (const [name, path, width, height] of [['player', '/', 1920, 1080], ['player-narrow', '/', 720, 1280], ['debug', '/debug', 1440, 1400]]) {
  await browser.viewport(width, height);
  await browser.send('Page.navigate', { url: base + path });
  await sleep(5000);
  const probe = await browser.evaluate(`JSON.stringify({phase: document.querySelector('#screen')?.dataset.phase,
    line: document.querySelector('#line')?.textContent, action: document.querySelector('#action')?.disabled,
    fonts: [...document.fonts].filter(f => f.status === 'loaded').map(f => f.family + ' ' + f.weight),
    overflow: document.documentElement.scrollWidth > innerWidth})`);
  writeFileSync(join(out, `check-${name}.png`), await browser.screenshot());
  console.log(`${name} ${width}×${height}: ${probe}`);
}

console.log(browser.problems.length ? browser.problems.join('\n') : '没有控制台错误');
await browser.close();
process.exit(browser.problems.length ? 1 : 0);
