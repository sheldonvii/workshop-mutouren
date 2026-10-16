"""姿态推理与静止判定。

关键点为 COCO 17 点，左右指玩家自身的左右。坐标都是原始采集帧的像素坐标。
位移按躯干长度归一化，躯干不可见时用 0.4 倍人体框高度近似（成年人躯干约占身高的 0.3～0.35，
框通常略高于人），使两种情况下阈值的量级一致。
"""
import time

import numpy as np

KEYPOINTS = ('nose', 'left_eye', 'right_eye', 'left_ear', 'right_ear', 'left_shoulder', 'right_shoulder',
             'left_elbow', 'right_elbow', 'left_wrist', 'right_wrist', 'left_hip', 'right_hip',
             'left_knee', 'right_knee', 'left_ankle', 'right_ankle')
NOSE, L_SHOULDER, R_SHOULDER, L_ELBOW, R_ELBOW, L_WRIST, R_WRIST, L_HIP, R_HIP = 0, 5, 6, 7, 8, 9, 10, 11, 12

PARTS = {
    'head': (0, 1, 2, 3, 4),
    'left_arm': (5, 7, 9),
    'right_arm': (6, 8, 10),
    'torso': (5, 6, 11, 12),
    'left_leg': (11, 13, 15),
    'right_leg': (12, 14, 16),
}

SKELETON = ((5, 7), (7, 9), (6, 8), (8, 10), (5, 6), (5, 11), (6, 12), (11, 12),
            (11, 13), (13, 15), (12, 14), (14, 16), (0, 1), (0, 2), (1, 3), (2, 4))


class Person:
    __slots__ = ('box', 'score', 'kps')

    def __init__(self, box, score, kps):
        self.box = np.asarray(box, dtype=np.float32)
        self.score = float(score)
        self.kps = np.asarray(kps, dtype=np.float32)

    @property
    def center(self):
        return np.array([(self.box[0] + self.box[2]) / 2, (self.box[1] + self.box[3]) / 2])

    @property
    def height(self):
        return float(self.box[3] - self.box[1])

    @property
    def area(self):
        return float((self.box[2] - self.box[0]) * (self.box[3] - self.box[1]))

    def visible(self, index, vis_min):
        return self.kps[index, 2] >= vis_min

    def to_json(self):
        return {'box': [round(float(v), 1) for v in self.box], 'score': round(self.score, 3),
                'kps': [[round(float(x), 1), round(float(y), 1), round(float(v), 2)] for x, y, v in self.kps]}

    @classmethod
    def from_json(cls, data):
        return cls(data['box'], data['score'], data['kps'])


def body_scale(person, vis_min):
    """躯干长度（肩中点到髋中点）；躯干不可见时用 0.4 倍框高。"""
    k = person.kps
    if all(person.visible(i, vis_min) for i in (L_SHOULDER, R_SHOULDER, L_HIP, R_HIP)):
        shoulders = (k[L_SHOULDER, :2] + k[R_SHOULDER, :2]) / 2
        hips = (k[L_HIP, :2] + k[R_HIP, :2]) / 2
        scale = float(np.linalg.norm(shoulders - hips))
        if scale > 1:
            return scale
    return max(1.0, 0.4 * person.height)


class PoseModel:
    """YOLOv8-Pose 封装。过滤掉置信度低或可见关键点太少的框，空画面上会出现这类误检。"""

    def __init__(self, cfg):
        self.config_path = cfg['config']
        self.min_score = float(cfg.get('min_score', 0.5))
        self.min_keypoints = int(cfg.get('min_keypoints', 5))
        self.vis_min = float(cfg.get('keypoint_min_visibility', 0.5))
        self.error = '尚未加载'
        self.load_s = None
        self._svc = None

    def load(self):
        try:
            from spacemit_vision import VisionServiceNative
            t = time.perf_counter()
            self._svc = VisionServiceNative.create(self.config_path, timing_enabled=True)
            if self._svc is None:
                raise RuntimeError(VisionServiceNative.last_create_error())
            self._svc.infer_image(np.zeros((720, 1280, 3), np.uint8))
            self.load_s = round(time.perf_counter() - t, 2)
            self.error = None
        except Exception as error:
            self._svc, self.error = None, f'姿态模型加载失败：{error}'

    @property
    def ready(self):
        return self._svc is not None

    def infer(self, image):
        """返回 (people, infer_ms)。"""
        from spacemit_vision import VisionServiceStatus
        t = time.perf_counter()
        status, results = self._svc.infer_image(image)
        infer_ms = (time.perf_counter() - t) * 1000
        if status != VisionServiceStatus.OK:
            raise RuntimeError(self._svc.last_error())
        people = []
        for r in results:
            if r.score < self.min_score:
                continue
            kps = [(k.x, k.y, k.visibility) for k in r.keypoints]
            if sum(v >= self.vis_min for _, _, v in kps) < self.min_keypoints:
                continue
            people.append(Person((r.x1, r.y1, r.x2, r.y2), r.score, kps))
        return people, infer_ms

    def status(self):
        return {'ok': self.ready, 'error': self.error, 'config': self.config_path, 'load_s': self.load_s}


class PlayerTracker:
    """单人锁定：开局取最大的人，之后按位置和尺寸找回。"""

    def __init__(self):
        self.ref = None

    def reset(self):
        self.ref = None

    @staticmethod
    def largest(people):
        return max(people, key=lambda p: p.area) if people else None

    def lock(self, people):
        self.ref = self.largest(people)
        return self.ref

    def find(self, people, strict=True):
        """strict 为 False 时，找不到相近的人就退回画面中最大的人（绿灯期间玩家可能换了位置）。"""
        if not people:
            return None
        if self.ref is None:
            return self.lock(people) if not strict else None
        best, best_cost = None, None
        ref_h = max(1.0, self.ref.height)
        for p in people:
            distance = float(np.linalg.norm(p.center - self.ref.center)) / ref_h
            ratio = p.height / ref_h
            if distance > 0.6 or not 0.6 < ratio < 1.6:
                continue
            cost = distance + abs(np.log(ratio))
            if best_cost is None or cost < best_cost:
                best, best_cost = p, cost
        if best is None and not strict:
            best = self.largest(people)
        if best is not None:
            self.ref = best
        return best


class MotionJudge:
    """红灯期间的静止判定：宽限期收集基准并估计每个关键点自身的抖动，之后逐帧比较。

    - 基准取宽限期最后 base_frames 帧的中值，也就是玩家摆好之后的姿势；宽限期前段还在调整姿势不影响基准。
    - 每个点的抖动只用宽限期后段（最后 noise_frames 帧）相邻两帧的差来估计，允许抖动 =
      clip(noise_k × 稳健标准差, 最低容忍, 最高容忍)。只有超出这部分的位移才算动。
      后段仍然剧烈跳动（noise_k × 标准差 > max_noise）的点多半被遮挡或看不清，不参与判定。
    - 当前帧取最近 smooth 帧的中值，压掉单帧跳变。眼睛和耳朵不参与判定，头部只看鼻子。
    - 判定值取扣除抖动后位移最大的那个点，与页面上标红的部位用同一个数：某个部位超过阈值，就开始计连续帧。
    """

    JUDGED = tuple(i for i in range(17) if i not in (1, 2, 3, 4))

    def __init__(self, vis_min=0.5, smooth=3, jitter_floor=0.03, noise_k=3.0, max_allow=0.15, max_noise=0.3,
                 base_frames=5, noise_frames=8, baseline_frames=30):
        self.vis_min = vis_min
        self.smooth = smooth
        self.jitter_floor = jitter_floor
        self.noise_k = noise_k
        self.max_allow = max_allow
        self.max_noise = max_noise
        self.base_frames = base_frames
        self.noise_frames = noise_frames
        self.baseline_frames = baseline_frames
        self.reset()

    def reset(self):
        self.samples = []
        self.base = None
        self.scale = None
        self.allow = None
        self.usable = None
        self.recent = []
        self.over = 0

    def add_baseline(self, person):
        self.samples.append(person)
        self.samples = self.samples[-self.baseline_frames:]

    def lock(self):
        """锁定基准并估计每个点的抖动。没有样本时返回 False。"""
        if not self.samples:
            return False
        settled = self.samples[-self.base_frames:]
        stack = np.stack([p.kps for p in settled])
        self.base = np.concatenate([np.median(stack[:, :, :2], axis=0), np.median(stack[:, :, 2:], axis=0)], axis=1)
        self.scale = float(np.median([body_scale(p, self.vis_min) for p in settled]))
        tail = np.stack([p.kps[:, :2] for p in self.samples[-self.noise_frames:]])
        if len(tail) >= 3:
            step = np.linalg.norm(np.diff(tail, axis=0), axis=2) / self.scale
            sigma = 1.4826 * np.median(step, axis=0) / np.sqrt(2)
        else:
            sigma = np.zeros(17)
        noise = self.noise_k * sigma
        self.usable = noise <= self.max_noise
        self.allow = np.clip(noise, self.jitter_floor, self.max_allow)
        self.recent = []
        self.over = 0
        return True

    def measure(self, person, threshold, consecutive):
        """返回 {'value', 'raw', 'parts', 'moved', 'part'}。value 是扣除抖动范围后位移最大的点，
        raw 是未扣除时的最大位移，parts 是各部位扣除后的最大位移；单位都是躯干长度。"""
        self.recent.append(person.kps)
        self.recent = self.recent[-self.smooth:]
        current = np.median(np.stack(self.recent), axis=0)
        mask = np.zeros(17, dtype=bool)
        mask[list(self.JUDGED)] = True
        mask &= self.usable & (self.base[:, 2] >= self.vis_min) & (current[:, 2] >= self.vis_min)
        if not mask.any():
            return {'value': None, 'raw': None, 'parts': {}, 'moved': False, 'part': None}
        d = np.linalg.norm(current[:, :2] - self.base[:, :2], axis=1) / self.scale
        excess = np.where(mask, np.maximum(0.0, d - self.allow), 0.0)
        parts = {}
        for name, indexes in PARTS.items():
            seen = [excess[i] for i in indexes if mask[i]]
            if seen:
                parts[name] = round(float(max(seen)), 4)
        value = float(excess[mask].max())
        raw = float(d[mask].max())
        self.over = self.over + 1 if value > threshold else 0
        part = max(parts, key=parts.get) if parts else None
        return {'value': round(value, 4), 'raw': round(raw, 4), 'parts': parts,
                'moved': self.over >= consecutive, 'part': part}


def frame_motion(image, previous):
    """画面晃动量：缩小成 96×54 灰度图后与上一帧的平均绝对差（0～255）。返回 (晃动量, 本帧缩略图)。"""
    import cv2
    small = cv2.resize(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), (96, 54), interpolation=cv2.INTER_AREA)
    if previous is None:
        return None, small
    return float(cv2.absdiff(small, previous).mean()), small
