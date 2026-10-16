// 画面绘制：实时预览的骨架、姿势小人和晃动曲线。设计稿和正式页面共用。
// 关键点为 COCO 17 点，坐标是采集帧的像素坐标；左右指玩家自身的左右。

export const SKELETON = [
  [5, 7], [7, 9], [6, 8], [8, 10], [5, 6], [5, 11], [6, 12], [11, 12],
  [11, 13], [13, 15], [12, 14], [14, 16], [0, 1], [0, 2], [1, 3], [2, 4],
];

export const PARTS = {
  head: [0, 1, 2, 3, 4],
  left_arm: [5, 7, 9],
  right_arm: [6, 8, 10],
  torso: [5, 6, 11, 12],
  left_leg: [11, 13, 15],
  right_leg: [12, 14, 16],
};

const BODY = SKELETON.slice(0, 12);

export function token(name, el = document.documentElement) {
  return getComputedStyle(el).getPropertyValue(name).trim();
}

export function fitCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.round(canvas.clientWidth * dpr));
  const h = Math.max(1, Math.round(canvas.clientHeight * dpr));
  if (canvas.width !== w || canvas.height !== h) {
    canvas.width = w;
    canvas.height = h;
  }
  return { w, h };
}

function indexesOf(partNames) {
  const set = new Set();
  for (const name of partNames || []) for (const i of PARTS[name] || []) set.add(i);
  return set;
}

function drawPerson(ctx, kps, { sx, sy, visMin, width, color, hot, hotColor }) {
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  for (const [a, b] of SKELETON) {
    const A = kps[a];
    const B = kps[b];
    if (A[2] < visMin || B[2] < visMin) continue;
    const isHot = hot.has(a) && hot.has(b);
    ctx.strokeStyle = isHot ? hotColor : color;
    ctx.lineWidth = isHot ? width * 1.6 : width;
    ctx.beginPath();
    ctx.moveTo(A[0] * sx, A[1] * sy);
    ctx.lineTo(B[0] * sx, B[1] * sy);
    ctx.stroke();
  }
  kps.forEach(([x, y, v], i) => {
    if (v < visMin) return;
    ctx.fillStyle = hot.has(i) ? hotColor : color;
    ctx.beginPath();
    ctx.arc(x * sx, y * sy, width * (hot.has(i) ? 1.4 : 0.9), 0, Math.PI * 2);
    ctx.fill();
  });
}

/**
 * 画一帧预览：source 为图片或 ImageBitmap，vision 为服务端 vision 事件。
 * 默认镜像显示，像照镜子；hotParts 为要标红的部位名。
 */
export function drawPreview(canvas, source, vision, { mirror = true, visMin = 0.5, hotParts = [] } = {}) {
  const ctx = canvas.getContext('2d');
  const { w, h } = fitCanvas(canvas);
  const colors = { player: token('--bib'), other: token('--bib-35'), hot: token('--red') };
  ctx.save();
  ctx.clearRect(0, 0, w, h);
  if (mirror) {
    ctx.translate(w, 0);
    ctx.scale(-1, 1);
  }
  if (source) ctx.drawImage(source, 0, 0, w, h);
  if (vision && vision.people) {
    const [fw, fh] = vision.size;
    const player = vision.player ? vision.player.player_index : null;
    const hot = indexesOf(hotParts);
    const unit = h / 120;
    vision.people.forEach((person, i) => {
      const isPlayer = i === player;
      drawPerson(ctx, person.kps, {
        sx: w / fw, sy: h / fh, visMin,
        width: isPlayer ? unit : unit * 0.5,
        color: isPlayer ? colors.player : colors.other,
        hot: isPlayer ? hot : new Set(),
        hotColor: colors.hot,
      });
    });
  }
  ctx.restore();
}

// 姿势小人的标准关键点：面向镜头站立，坐标与 tests/test_logic.py 中的模板一致
const HEAD = [[640, 230], [655, 215], [625, 215], [670, 225], [610, 225]];
const SHOULDERS = [[700, 300], [580, 300]];
const HIPS = [[680, 500], [600, 500]];
const LEGS = [[685, 620], [595, 620], [690, 700], [590, 700]];
const ARMS = {
  neutral: [[710, 400], [570, 400], [715, 490], [565, 490]],
  raise_left: [[715, 200], [570, 400], [720, 100], [565, 490]],
  raise_right: [[710, 400], [565, 200], [715, 490], [560, 100]],
  both_up: [[715, 200], [565, 200], [720, 100], [560, 100]],
  t_pose: [[800, 300], [480, 300], [900, 300], [380, 300]],
  hands_on_hips: [[780, 420], [500, 420], [680, 500], [600, 500]],
};

export function figureKeypoints(poseId) {
  const [le, re, lw, rw] = ARMS[poseId] || ARMS.neutral;
  return [...HEAD, ...SHOULDERS, le, re, lw, rw, ...HIPS, ...LEGS].map(([x, y]) => [x, y, 1]);
}

// 所有姿势共用一个取景框，小人大小一致，抬起的手臂才看得出来
const FIGURE_FRAME = { minX: 360, maxX: 920, minY: 60, maxY: 715 };

/** 画姿势小人，左右镜像：玩家举起自己的左手，看到的是屏幕左侧那只手举起。 */
export function drawFigure(canvas, poseId, color) {
  const ctx = canvas.getContext('2d');
  const { w, h } = fitCanvas(canvas);
  const kps = figureKeypoints(poseId);
  const { minX, maxX, minY, maxY } = FIGURE_FRAME;
  const scale = Math.min(w / (maxX - minX), h / (maxY - minY));
  const ox = (w - (maxX - minX) * scale) / 2;
  const oy = (h - (maxY - minY) * scale) / 2;
  const px = (x) => w - (ox + (x - minX) * scale);
  const py = (y) => oy + (y - minY) * scale;
  ctx.clearRect(0, 0, w, h);
  ctx.strokeStyle = color;
  ctx.fillStyle = color;
  ctx.lineWidth = 34 * scale;
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  for (const [a, b] of BODY) {
    ctx.beginPath();
    ctx.moveTo(px(kps[a][0]), py(kps[a][1]));
    ctx.lineTo(px(kps[b][0]), py(kps[b][1]));
    ctx.stroke();
  }
  ctx.beginPath();
  ctx.arc(px(HEAD[0][0]), py(HEAD[0][1] - 18), 46 * scale, 0, Math.PI * 2);
  ctx.fill();
}

/** 晃动曲线：values 为最近的判定值（旧到新），threshold 为淘汰线。 */
export function drawTremor(canvas, values, threshold, color) {
  const ctx = canvas.getContext('2d');
  const { w, h } = fitCanvas(canvas);
  const top = Math.max(threshold * 1.6, ...values.filter((v) => v != null).map((v) => v * 1.1));
  const y = (v) => h - (Math.min(v, top) / top) * (h - 6) - 3;
  const line = Math.max(2, h / 28);
  ctx.clearRect(0, 0, w, h);
  ctx.strokeStyle = color;
  ctx.lineWidth = Math.max(2, line / 2);
  ctx.setLineDash([line * 1.6, line * 1.2]);
  ctx.globalAlpha = 0.75;
  ctx.beginPath();
  ctx.moveTo(0, y(threshold));
  ctx.lineTo(w, y(threshold));
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.globalAlpha = 1;
  ctx.lineWidth = line;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.beginPath();
  let started = false;
  values.forEach((v, i) => {
    if (v == null) return;
    const x = values.length < 2 ? w : (i / (values.length - 1)) * (w - line) + line / 2;
    if (started) ctx.lineTo(x, y(v));
    else ctx.moveTo(x, y(v));
    started = true;
  });
  ctx.stroke();
}
