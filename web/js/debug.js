// 调试页：实时画面、判定值曲线、规则参数、舵机标定、录制和事件日志。
import { api, connect } from './conn.js';
import { drawPreview, fitCanvas } from './draw.js';

const $ = (id) => document.getElementById(id);
const PHASES = {
  idle: '待机', countdown: '倒计时', green: '绿灯', turning: '转头中', red: '红灯', safe: '本轮过关',
  eliminated: '淘汰', win: '胜利', paused: '暂停', error: '异常',
};
const SUBS = { grace: '宽限', watch: '判定' };
const ACTIVE = new Set(['countdown', 'green', 'turning', 'red', 'safe']);
const CHART_SECONDS = 20;

const NUMBER_FIELDS = [
  ['rounds', '轮数', 1, 20, 1],
  ['countdown_s', '倒计时（秒）', 0, 10, 0.5],
  ['green_s.0', '绿灯最短（秒）', 1, 20, 0.5],
  ['green_s.1', '绿灯最长（秒）', 1, 20, 0.5],
  ['red_s.0', '红灯最短（秒）', 1, 30, 0.5],
  ['red_s.1', '红灯最长（秒）', 1, 30, 0.5],
  ['grace_s', '宽限（秒）', 0.2, 3, 0.1],
  ['safe_s', '过关停留（秒）', 0.5, 5, 0.5],
  ['result_hold_s', '结果停留（秒）', 1, 60, 1],
  ['lost_frames', '离开判定（帧）', 1, 60, 1],
  ['jitter_floor', '最低抖动容忍（躯干）', 0, 0.3, 0.005],
  ['still_threshold', '画面静止阈值', 0.1, 50, 0.1],
  ['still_frames', '连续静止帧数', 1, 30, 1],
  ['settle_timeout_s', '停稳最长等待（秒）', 0, 10, 0.5],
];
const SELECTS = [
  ['difficulty', '难度', [['easy', '简单'], ['normal', '普通'], ['hard', '困难']]],
  ['language', '口令', [['zh', '中文'], ['ko', '韩语']]],
  ['unclear_policy', '姿势看不清时', [['pass', '只判静止'], ['eliminate', '直接淘汰']]],
];

const S = { motion: null, config: null, state: null, servo: null, formDirty: false, latest: null, decoding: false, shown: null, dirty: false, chart: [], log: [] };

function message(id, text, bad = false) {
  const node = $(id);
  node.textContent = text;
  node.toggleAttribute('data-bad', bad);
}

async function call(id, path, body, method = 'POST') {
  try {
    const result = await api(path, body, method);
    return result;
  } catch (error) {
    message(id, error.message, true);
    return null;
  }
}

// ---- 规则表单 ----

function getPath(obj, path) {
  return path.split('.').reduce((o, k) => (o == null ? o : o[k]), obj);
}

function buildForm(cfg, poses) {
  const form = $('config');
  form.replaceChildren();
  for (const [key, label, min, max, step] of NUMBER_FIELDS) {
    form.append(field(key, label, `<input type="number" id="f-${key}" name="${key}" min="${min}" max="${max}" step="${step}">`));
  }
  for (const [key, label, options] of SELECTS) {
    const opts = options.map(([v, t]) => `<option value="${v}">${t}</option>`).join('');
    form.append(field(key, label, `<select id="f-${key}" name="${key}">${opts}</select>`));
  }
  form.append(field('threshold', '当前难度阈值', '<input type="number" id="f-threshold" min="0.005" max="1" step="0.005">'));
  form.append(field('consecutive', '当前难度连续帧', '<input type="number" id="f-consecutive" min="1" max="30" step="1">'));
  const set = document.createElement('fieldset');
  set.className = 'field';
  set.innerHTML = `<legend>姿势库</legend><div class="checks">${poses.map((p) =>
    `<label><input type="checkbox" name="pose" value="${p.id}"> ${p.name}</label>`).join('')}</div>`;
  form.append(set);
  form.addEventListener('input', () => { S.formDirty = true; });
  $('f-difficulty').addEventListener('change', () => fillDifficulty(S.config.game, $('f-difficulty').value));
}

function field(key, label, control) {
  const wrap = document.createElement('div');
  wrap.className = 'field';
  wrap.innerHTML = `<label for="f-${key}">${label}</label>${control}`;
  return wrap;
}

function fillForm(cfg) {
  for (const [key] of NUMBER_FIELDS) $(`f-${key}`).value = getPath(cfg, key);
  for (const [key] of SELECTS) $(`f-${key}`).value = cfg[key];
  fillDifficulty(cfg, cfg.difficulty);
  for (const box of document.querySelectorAll('input[name="pose"]')) box.checked = cfg.poses.includes(box.value);
  S.formDirty = false;
}

function fillDifficulty(cfg, name) {
  const level = cfg.difficulties[name] || {};
  $('f-threshold').value = level.threshold ?? '';
  $('f-consecutive').value = level.consecutive ?? '';
}

function collectForm() {
  const cfg = S.config.game;
  const num = (key) => Number($(`f-${key}`).value);
  const difficulty = $('f-difficulty').value;
  return {
    rounds: num('rounds'),
    countdown_s: num('countdown_s'),
    green_s: [num('green_s.0'), num('green_s.1')],
    red_s: [num('red_s.0'), num('red_s.1')],
    grace_s: num('grace_s'),
    safe_s: num('safe_s'),
    result_hold_s: num('result_hold_s'),
    lost_frames: num('lost_frames'),
    jitter_floor: num('jitter_floor'),
    still_threshold: num('still_threshold'),
    still_frames: num('still_frames'),
    settle_timeout_s: num('settle_timeout_s'),
    difficulty,
    language: $('f-language').value,
    unclear_policy: $('f-unclear_policy').value,
    poses: [...document.querySelectorAll('input[name="pose"]:checked')].map((b) => b.value),
    difficulties: { ...cfg.difficulties, [difficulty]: { threshold: num('threshold'), consecutive: num('consecutive') } },
  };
}

async function applyConfig(save) {
  const body = { game: collectForm(), servo: { speed_us_per_s: Number($('speed').value), settle_s: Number($('settle').value) } };
  if (save) body.save = true;
  const result = await call('config-msg', '/api/config', body, 'PUT');
  if (result) {
    S.formDirty = false;
    message('config-msg', save ? '已应用并写入 configs/' : '已应用，重启服务后恢复配置文件里的值');
  }
}

// ---- 舵机 ----

function renderServo() {
  const s = S.servo;
  if (!s) return;
  const pos = s.positions;
  const where = s.output ? `${s.us}μs（${s.target === 'manual' ? '手动' : s.target}）` : '未输出';
  const state = s.ok ? '' : `，${s.error}`;
  message('servo-msg', `${s.driver === 'gpio' ? 'GPIO 脉冲' : '模拟舵机'}：${where}；待机 ${pos.standby}、面对 ${pos.face}、背对 ${pos.back}μs${state}`, !s.ok);
  if (document.activeElement !== $('speed')) $('speed').value = s.speed_us_per_s;
  if (document.activeElement !== $('settle')) $('settle').value = s.settle_s;
  const locked = ACTIVE.has(S.state?.phase);
  for (const b of document.querySelectorAll('[data-position], [data-mark], #us-go')) b.disabled = locked;
}

function syncUs(value) {
  $('us').value = value;
  $('us-range').value = value;
}

// ---- 画面与曲线 ----

function onFrame(meta, blob) {
  const p = meta.player;
  S.motion = meta.motion;
  if (p?.judging && p.value != null) S.chart.push({ t: performance.now(), v: p.value, raw: p.raw, thr: p.threshold });
  S.latest = { meta, blob };
  if (!S.decoding) decodeLatest();
}

async function decodeLatest() {
  S.decoding = true;
  while (S.latest) {
    const { meta, blob } = S.latest;
    S.latest = null;
    try {
      const bitmap = await createImageBitmap(blob);
      S.shown?.bitmap.close();
      S.shown = { meta, bitmap };
      S.dirty = true;
    } catch {
      // 丢弃损坏的帧
    }
  }
  S.decoding = false;
}

function drawChart() {
  const canvas = $('chart');
  const ctx = canvas.getContext('2d');
  const { w, h } = fitCanvas(canvas);
  const now = performance.now();
  S.chart = S.chart.filter((p) => now - p.t < CHART_SECONDS * 1000);
  ctx.clearRect(0, 0, w, h);
  if (!S.chart.length) return;
  const thr = S.chart[S.chart.length - 1].thr;
  const top = Math.max(thr * 2, ...S.chart.map((p) => Math.max(p.v, p.raw ?? 0) * 1.1));
  const x = (t) => w - ((now - t) / (CHART_SECONDS * 1000)) * w;
  const y = (v) => h - (v / top) * (h - 8) - 4;
  const dpr = window.devicePixelRatio || 1;
  ctx.strokeStyle = '#e23d5e';
  ctx.lineWidth = 2 * dpr;
  ctx.setLineDash([6 * dpr, 5 * dpr]);
  ctx.beginPath();
  ctx.moveTo(0, y(thr));
  ctx.lineTo(w, y(thr));
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.strokeStyle = 'rgb(24 33 58 / 0.3)';
  ctx.beginPath();
  S.chart.filter((p) => p.raw != null).forEach((p, i) => (i ? ctx.lineTo(x(p.t), y(p.raw)) : ctx.moveTo(x(p.t), y(p.raw))));
  ctx.stroke();
  ctx.strokeStyle = '#18213a';
  ctx.beginPath();
  S.chart.forEach((p, i) => (i ? ctx.lineTo(x(p.t), y(p.v)) : ctx.moveTo(x(p.t), y(p.v))));
  ctx.stroke();
  ctx.fillStyle = 'rgb(24 33 58 / 0.72)';
  ctx.font = `${12 * dpr}px sans-serif`;
  ctx.fillText(`最近 ${CHART_SECONDS} 秒；当前 ${S.chart[S.chart.length - 1].v.toFixed(3)}，阈值 ${thr}`, 6 * dpr, 16 * dpr);
}

function loop() {
  if (S.dirty && S.shown) {
    drawPreview($('preview'), S.shown.bitmap, S.shown.meta);
    S.dirty = false;
  }
  drawChart();
  requestAnimationFrame(loop);
}

// ---- 事件 ----

function renderMetrics(m) {
  const rows = [
    ['摄像头', `${m.camera_fps} fps，${m.size[0]}×${m.size[1]}`],
    ['推理', `${m.vision_fps} fps，单帧 ${m.infer_ms ?? '-'} ms`],
    ['画面晃动', S.motion == null ? '-' : `${S.motion}（低于静止阈值 ${S.config?.game.still_threshold ?? '-'} 算静止）`],
    ['舵机', m.servo_driver === 'gpio' ? 'GPIO 脉冲' : '模拟'],
    ['连接页面', `${m.clients} 个`],
    ['录制', m.recorder.active ? `${m.recorder.file}，${m.recorder.frames} 帧` : '未录制'],
  ];
  $('metrics').innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');
}

function log(event) {
  const time = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  const { v, type, ...rest } = event;
  S.log.unshift(`${time} ${type} ${JSON.stringify(rest)}`);
  S.log.length = Math.min(S.log.length, 200);
  $('log').textContent = S.log.join('\n');
}

function onEvent(event) {
  if (event.type !== 'metrics') log(event);
  if (event.type === 'hello') {
    const first = !S.config;
    S.config = event.config;
    if (first) buildForm(event.config.game, event.config.poses);
    if (!S.formDirty) fillForm(event.config.game);
  } else if (event.type === 'state') {
    S.state = event;
    const problems = (event.problems || []).map((p) => p.message).join('；');
    $('phase').textContent = `${PHASES[event.phase] || event.phase}${event.sub ? `·${SUBS[event.sub]}` : ''}`
      + (event.round ? ` 第 ${event.round}/${event.rounds} 轮` : '') + `，${event.people} 人`;
    message('game-msg', problems || event.message || '', Boolean(problems));
    renderServo();
  } else if (event.type === 'servo') {
    S.servo = event;
    renderServo();
  } else if (event.type === 'metrics') {
    renderMetrics(event);
  }
}

// ---- 操作 ----

for (const button of document.querySelectorAll('[data-game]')) {
  button.addEventListener('click', async () => {
    const result = await call('game-msg', '/api/game', { action: button.dataset.game });
    if (result) message('game-msg', result.message);
  });
}

for (const button of document.querySelectorAll('[data-record]')) {
  button.addEventListener('click', async () => {
    const result = await call('record-msg', '/api/record', { action: button.dataset.record });
    if (result) message('record-msg', result.active ? `正在录制 ${result.file}` : `已停止，文件 ${result.file ?? '无'}`);
  });
}

for (const button of document.querySelectorAll('[data-position]')) {
  button.addEventListener('click', async () => {
    const s = await call('servo-msg', '/api/servo/test', { position: button.dataset.position });
    if (s) syncUs(s.us);
  });
}

$('us-range').addEventListener('input', () => { $('us').value = $('us-range').value; });
$('us-range').addEventListener('change', () => call('servo-msg', '/api/servo/test', { us: Number($('us-range').value) }));
$('us').addEventListener('change', () => { $('us-range').value = $('us').value; });
$('us-go').addEventListener('click', () => call('servo-msg', '/api/servo/test', { us: Number($('us').value) }));

for (const button of document.querySelectorAll('[data-mark]')) {
  button.addEventListener('click', async () => {
    const us = Number($('us').value);
    const result = await call('servo-msg', '/api/config', { servo: { positions: { [button.dataset.mark]: us } } }, 'PUT');
    if (result) message('servo-msg', `已把 ${us}μs 记为${button.dataset.mark === 'face' ? '面对' : '背对'}，点"应用并保存"写入配置文件`);
  });
}

$('apply').addEventListener('click', () => applyConfig(false));
$('save').addEventListener('click', () => applyConfig(true));

connect({
  onEvent,
  onFrame,
  onStatus(ok) {
    $('conn').textContent = ok ? '已连接' : '连接断开，正在重连';
    $('conn').toggleAttribute('data-bad', !ok);
  },
});
requestAnimationFrame(loop);
