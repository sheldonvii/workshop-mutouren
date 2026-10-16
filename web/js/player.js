// 玩家大屏：只渲染服务端事件，不自行推进回合或判定。事件格式见实施文档第 5.6 节。
import { api, connect } from './conn.js';
import { Sound } from './audio.js';
import { drawFigure, drawPreview, drawTremor } from './draw.js';

const $ = (id) => document.getElementById(id);
const el = {
  screen: $('screen'), wall: $('wall'), wipe: $('wipe'), confetti: $('confetti'), conn: $('conn'),
  word: $('word'), reason: $('reason'), line: $('line'), note: $('note'), count: $('count'), chant: $('chant'),
  prompt: $('prompt'), promptFigure: $('prompt-figure'), promptName: $('prompt-name'),
  tremor: $('tremor'), tremorCanvas: $('tremor-canvas'), tremorHint: $('tremor-hint'),
  head: $('head'), preview: $('preview'), snapshot: $('snapshot'), tag: $('tag'),
  bib: $('bib'), bibNo: $('bib-no'), roundNum: $('round-num'), roundOf: $('round-of'), difficulty: $('difficulty'),
  chip: $('chip'), chipFigure: $('chip-figure'), chipName: $('chip-name'),
  chipOk: $('chip-ok'), chipBad: $('chip-bad'), chipUnknown: $('chip-unknown'),
  status: $('status'), action: $('action'), actionLabel: $('action-label'), actionKey: $('action-key'),
};

const CHANTS = { zh: '一、二、三，木头人', ko: '무궁화 꽃이 피었습니다' };
const DIFFICULTY = { easy: '简单', normal: '普通', hard: '困难' };
const RESTING = new Set(['idle', 'eliminated', 'win']);
const SHOWS_POSE = new Set(['green', 'turning', 'red', 'safe', 'eliminated']);
const TREMOR_FRAMES = 45;
const DANGER_RATIO = 0.7;
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)');
const sound = new Sound();

const S = {
  game: null, state: null, stateAt: 0, servo: null, connected: false,
  latest: null, decoding: false, shown: null, dirty: false,
  tremor: [], threshold: 0.1, hotParts: [], chantTimers: [], countShown: null,
  flash: null, flashTimer: null, landTimer: null,
};

// ---- 工具 ----

function restart(node, cls) {
  node.classList.remove(cls);
  void node.offsetWidth;
  node.classList.add(cls);
}

function setText(node, text) {
  node.textContent = text || '';
  node.hidden = !text;
}

function stamp(node) {
  if (reduceMotion.matches) return;
  restart(node, 'stamp');
  node.addEventListener('animationend', () => node.classList.remove('stamp'), { once: true });
  restart(el.wall, 'shake');
  setTimeout(() => el.wall.classList.remove('shake'), 300);
}

function bibNumber(run) {
  return String((parseInt(run.slice(0, 6), 16) % 999) + 1).padStart(3, '0');
}

function remainingMs(st) {
  return st.ends_in_ms == null ? null : Math.max(0, st.ends_in_ms - (performance.now() - S.stateAt));
}

function flash(text) {
  S.flash = text;
  clearTimeout(S.flashTimer);
  S.flashTimer = setTimeout(() => {
    S.flash = null;
    if (S.state) render(S.state);
  }, 3000);
  if (S.state) render(S.state);
}

// ---- 墙面：新颜色从玩偶的头向外扩散；淘汰时闪两下熄灯 ----

function paintWall(phase) {
  const before = getComputedStyle(el.wall).backgroundColor;
  el.screen.dataset.phase = phase;
  const after = getComputedStyle(el.wall).backgroundColor;
  if (before === after || reduceMotion.matches) return;
  const wall = el.wall.getBoundingClientRect();
  const head = el.head.getBoundingClientRect();
  el.wipe.style.setProperty('--ox', `${head.left + head.width / 2 - wall.left}px`);
  el.wipe.style.setProperty('--oy', `${head.top + head.height / 2 - wall.top}px`);
  el.wipe.style.setProperty('--wipe-from', before);
  el.wipe.className = 'wipe';
  void el.wipe.offsetWidth;
  el.wipe.className = `wipe ${phase === 'eliminated' ? 'blackout' : 'go'}`;
}

// ---- 口令：唱到哪个字点亮哪个字，最后一个字在转身前约 0.25 秒落下 ----

function buildChant(lang, sungAll) {
  const text = CHANTS[lang] || CHANTS.zh;
  const chars = [...text].filter((c) => /[一-鿿가-힯]/.test(c));
  el.chant.lang = lang;
  el.chant.setAttribute('aria-label', text);
  el.chant.replaceChildren(...chars.map((c) => {
    const span = document.createElement('span');
    span.textContent = c;
    if (sungAll) span.className = 'sung';
    return span;
  }));
}

function playChant(cue) {
  clearChant();
  buildChant(cue.lang, reduceMotion.matches);
  const spans = [...el.chant.children];
  const step = Math.max(120, (cue.duration_ms - 250) / Math.max(1, spans.length - 1));
  spans.forEach((span, i) => {
    S.chantTimers.push(setTimeout(() => {
      span.classList.add('sung');
      sound.drum(i === spans.length - 1);
    }, i * step));
  });
}

function clearChant() {
  S.chantTimers.forEach(clearTimeout);
  S.chantTimers = [];
}

// ---- 阶段切换：只在阶段或子阶段变化时执行一次 ----

function enterPhase(st, prev) {
  const { phase } = st;
  clearChant();
  el.screen.removeAttribute('data-danger');
  for (const node of [el.reason, el.count, el.chant, el.prompt, el.tremor]) node.hidden = true;
  el.word.hidden = false;
  el.word.className = 'state-word';
  el.snapshot.hidden = true;
  el.preview.hidden = false;
  el.head.classList.remove('scanning');
  S.hotParts = [];
  paintWall(phase);

  if (phase === 'idle') {
    el.word.innerHTML = '一二三，<br>木头人';
    el.word.classList.add('state-word--title');
  } else if (phase === 'countdown') {
    el.word.hidden = true;
    el.count.hidden = false;
    S.countShown = null;
  } else if (phase === 'green') {
    el.word.hidden = true;
    buildChant(S.game?.language || 'zh', false);
    el.chant.hidden = false;
    showPrompt(st.pose);
  } else if (phase === 'turning') {
    el.word.textContent = '别动';
    stamp(el.word);
  } else if (phase === 'red') {
    el.word.textContent = '别动';
    if (st.sub === 'grace') {
      if (prev?.phase !== 'turning') stamp(el.word);
      startScan(st.duration_ms);
    } else {
      S.tremor = [];
      el.tremor.hidden = false;
      setText(el.tremorHint, '超过虚线就淘汰');
      drawTremorNow();
    }
  } else if (phase === 'safe') {
    el.word.textContent = '过关';
    stamp(el.word);
    sound.ding();
  } else if (phase === 'eliminated') {
    el.word.textContent = '淘汰';
    stamp(el.word);
    showVerdict(st.verdict);
  } else if (phase === 'win') {
    el.word.textContent = '你赢了';
    el.word.classList.add('state-word--title');
    stamp(el.word);
    confetti();
  } else {
    el.word.textContent = '暂停';
    el.word.classList.add('state-word--title');
  }
}

function showPrompt(pose) {
  if (!pose) return;
  el.prompt.hidden = false;
  el.promptName.textContent = pose.name;
  requestAnimationFrame(() => drawFigure(el.promptFigure, pose.id, getComputedStyle(el.promptFigure).color));
}

function startScan(ms) {
  if (reduceMotion.matches) return;
  el.head.style.setProperty('--scan-ms', `${ms || 800}ms`);
  restart(el.head, 'scanning');
}

function showVerdict(verdict) {
  if (!verdict) return;
  let reason = verdict.reason_text || '';
  if (verdict.reason === 'moved' && verdict.part_text) reason = `${verdict.part_text}动了`;
  if (verdict.reason === 'left_view') reason = '离开了画面';
  setText(el.reason, reason);
  if (reason) restart(el.reason, 'rise');
  const src = verdict.snapshot_url || (verdict.snapshot && `/api/snapshots/${verdict.snapshot}.jpg`);
  if (src) {
    el.snapshot.src = src;
    el.snapshot.hidden = false;
    el.preview.hidden = true;
  }
}

function confetti() {
  if (reduceMotion.matches) return;
  const colors = ['--bib', '--sand', '--sky', '--red'];
  const pieces = Array.from({ length: 70 }, (_, i) => {
    const piece = document.createElement('i');
    piece.style.left = `${Math.random() * 100}%`;
    piece.style.setProperty('--c', `var(${colors[i % colors.length]})`);
    piece.style.setProperty('--d', `${1.8 + Math.random() * 1.6}s`);
    piece.style.setProperty('--delay', `${Math.random() * 0.6}s`);
    piece.style.setProperty('--dx', `${(Math.random() - 0.5) * 30}cqw`);
    piece.style.setProperty('--r', `${(Math.random() - 0.5) * 1080}deg`);
    return piece;
  });
  el.confetti.replaceChildren(...pieces);
  setTimeout(() => el.confetti.replaceChildren(), 4200);
}

// ---- 每次 state 事件都刷新的部分 ----

function render(st) {
  const { phase } = st;
  const problems = st.problems || [];
  let line = '';
  let note = '';
  if (phase === 'idle') {
    if (problems.length) line = problems.map((p) => p.message).join('；');
    else if (st.people === 0) line = '画面里没有人，站到摄像头前';
    else line = '站进画面，按空格开始';
    if (!problems.length && st.people > 1) note = `画面里有 ${st.people} 人，开局后盯住最近的那位`;
  } else if (phase === 'countdown') {
    line = '准备';
    note = st.message;
  } else if (phase === 'red' && st.sub === 'watch') {
    note = st.message;
  } else if (phase === 'safe') {
    line = `第 ${st.round} 轮，共 ${st.rounds} 轮`;
  } else if (phase === 'win') {
    line = `${st.rounds} 轮都没动`;
  } else if (phase === 'eliminated') {
    if (st.verdict?.reason === 'wrong_pose' && st.pose) line = `应该${st.pose.name}`;
    note = holdNote(st);
  } else if (phase === 'paused') {
    line = st.message || '已暂停';
  } else if (phase === 'error') {
    line = problems.map((p) => p.message).join('；') || st.message || '设备异常';
  }
  setText(el.line, line);
  setText(el.note, S.flash || note);
  renderGround(st);
  renderAction(st);
  renderStatus();
}

function holdNote(st) {
  const ms = remainingMs(st);
  return ms == null ? '' : `${Math.ceil(ms / 1000)} 秒后回到待机`;
}

function renderGround(st) {
  el.bib.hidden = !st.run;
  if (st.run) el.bibNo.textContent = bibNumber(st.run);
  const inGame = st.run && st.round > 0;
  const num = String(inGame ? st.round : st.rounds);
  if (el.roundNum.textContent !== num) {
    el.roundNum.textContent = num;
    if (inGame && !reduceMotion.matches) restart(el.roundNum, 'roll');
  }
  el.roundOf.hidden = !inGame;
  el.roundOf.textContent = `/${st.rounds}`;
  el.difficulty.hidden = !RESTING.has(st.phase);
  el.difficulty.textContent = `难度：${DIFFICULTY[st.difficulty] || st.difficulty}`;
  const showChip = Boolean(st.pose) && SHOWS_POSE.has(st.phase);
  el.chip.hidden = !showChip;
  if (showChip && el.chipName.textContent !== st.pose.name) {
    el.chipName.textContent = st.pose.name;
    requestAnimationFrame(() => drawFigure(el.chipFigure, st.pose.id, getComputedStyle(el.chipFigure).color));
  }
  if (st.phase !== 'red') setPoseIcon(null, false);
}

function setPoseIcon(ok, visible = true) {
  el.chipOk.hidden = !(visible && ok === true);
  el.chipBad.hidden = !(visible && ok === false);
  el.chipUnknown.hidden = !(visible && ok == null);
}

function renderAction(st) {
  const { phase } = st;
  let label = '停止';
  let key = 'Esc';
  let enabled = true;
  if (RESTING.has(phase)) {
    label = phase === 'idle' ? '开始' : '再来一局';
    key = '空格';
    enabled = st.can_start;
  } else if (phase === 'paused') {
    label = '继续';
    key = '空格';
  }
  el.actionLabel.textContent = label;
  el.actionKey.textContent = key;
  el.action.disabled = !enabled || !S.connected;
}

function renderStatus() {
  const parts = [];
  if (S.servo?.driver === 'sim') parts.push('舵机：模拟');
  if (S.servo && !S.servo.ok) parts.push('舵机异常');
  if (!sound.ready) parts.push('按任意键开启声音');
  setText(el.status, parts.join('，'));
}

// ---- 事件 ----

function onState(st) {
  const prev = S.state;
  S.state = st;
  S.stateAt = performance.now();
  if (!prev || prev.phase !== st.phase || prev.sub !== st.sub) enterPhase(st, prev);
  render(st);
}

function onServo(status) {
  const first = S.servo === null;
  S.servo = status;
  const facing = status.target === 'back' ? 'back' : 'face';
  if (el.head.dataset.facing !== facing) {
    const travel = Math.max(200, status.settled_in_ms - status.settle_s * 1000);
    el.head.style.setProperty('--turn-ms', `${first || reduceMotion.matches ? 0 : travel}ms`);
    el.head.dataset.facing = facing;
    clearTimeout(S.landTimer);
    if (!first && !reduceMotion.matches) S.landTimer = setTimeout(() => restart(el.head, 'landed'), travel);
  }
  el.tag.textContent = '模拟舵机';
  el.tag.hidden = status.driver !== 'sim';
  renderStatus();
}

function onAudio(cue) {
  if (cue.cue === 'chant') playChant(cue);
  else if (cue.cue === 'turn') sound.whistle();
  else if (cue.cue === 'eliminated') sound.buzzer();
  else if (cue.cue === 'win') sound.fanfare();
}

function onEvent(event) {
  if (event.type === 'hello') S.game = event.config.game;
  else if (event.type === 'state') onState(event);
  else if (event.type === 'servo') onServo(event);
  else if (event.type === 'audio') onAudio(event);
  else if (event.type === 'error' && event.code === 'infer_failed') flash(event.message);
}

function onVision(meta) {
  const st = S.state;
  const player = meta.player;
  if (!st || st.phase !== 'red' || !player) return;
  if (player.player_index != null) setPoseIcon(player.pose_ok);
  if (st.sub !== 'watch' || !player.judging) return;
  S.threshold = player.threshold;
  S.tremor.push(player.value);
  if (S.tremor.length > TREMOR_FRAMES) S.tremor.shift();
  S.hotParts = Object.entries(player.parts || {}).filter(([, v]) => v > player.threshold).map(([k]) => k);
  const danger = player.value != null && player.value >= player.threshold * DANGER_RATIO;
  el.screen.toggleAttribute('data-danger', danger);
  setText(el.tremorHint, danger ? '快超线了' : '超过虚线就淘汰');
  drawTremorNow();
}

function drawTremorNow() {
  if (!el.tremor.hidden) drawTremor(el.tremorCanvas, S.tremor, S.threshold, getComputedStyle(el.tremorCanvas).color);
}

function onFrame(meta, blob) {
  onVision(meta);
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
      // 损坏的帧直接丢弃，等下一帧
    }
  }
  S.decoding = false;
}

// ---- 动画循环：画预览、倒计时数字、淘汰后回到待机的秒数 ----

function loop() {
  const st = S.state;
  if (st) {
    const ms = remainingMs(st);
    if (st.phase === 'countdown' && ms != null) {
      const n = Math.max(1, Math.ceil(ms / 1000));
      if (n !== S.countShown) {
        S.countShown = n;
        el.count.textContent = n;
        stamp(el.count);
        sound.tick(n === 1);
      }
    } else if (st.phase === 'eliminated' && !S.flash) {
      const text = holdNote(st);
      if (el.note.textContent !== text) setText(el.note, text);
    }
  }
  if (S.dirty && S.shown && !el.preview.hidden && el.head.dataset.facing === 'face') {
    drawPreview(el.preview, S.shown.bitmap, S.shown.meta, { hotParts: S.hotParts });
    S.dirty = false;
  }
  requestAnimationFrame(loop);
}

// ---- 操作：空格开局或继续，Esc 停止 ----

async function act(action) {
  try {
    await api('/api/game', { action });
  } catch (error) {
    flash(error.message);
  }
}

function primary() {
  const phase = S.state?.phase;
  if (RESTING.has(phase)) act('start');
  else if (phase === 'paused') act('resume');
}

function stop() {
  const phase = S.state?.phase;
  if (phase && !RESTING.has(phase)) act('stop');
}

function unlockSound() {
  sound.unlock();
  renderStatus();
}

el.action.addEventListener('click', () => {
  unlockSound();
  const phase = S.state?.phase;
  if (RESTING.has(phase) || phase === 'paused') primary();
  else stop();
});

addEventListener('keydown', (event) => {
  unlockSound();
  if (event.repeat) return;
  if (event.code === 'Space' && !event.target.closest('button')) {
    event.preventDefault();
    primary();
  } else if (event.code === 'Escape') {
    event.preventDefault();
    stop();
  }
});

addEventListener('pointerdown', unlockSound);

new ResizeObserver(() => {
  S.dirty = true;
  drawTremorNow();
  if (!el.prompt.hidden && S.state?.pose) showPrompt(S.state.pose);
  if (!el.chip.hidden && S.state?.pose) drawFigure(el.chipFigure, S.state.pose.id, getComputedStyle(el.chipFigure).color);
}).observe(el.screen);

window.lucide?.createIcons({ attrs: { 'stroke-width': 2 } });
const handlers = {
  onEvent,
  onFrame,
  onStatus(ok) {
    S.connected = ok;
    el.conn.hidden = ok;
    if (S.state) renderAction(S.state);
  },
};
connect(handlers);
requestAnimationFrame(loop);
