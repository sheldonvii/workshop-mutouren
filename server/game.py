"""游戏状态机。服务端是游戏状态的唯一来源，页面只渲染这里发出的事件。

阶段：idle 待机（含等待入镜）、countdown 准备倒计时、green 绿灯、turning 转头中、red 红灯盯人
（sub 为 grace 宽限或 watch 判定）、safe 本轮安全、eliminated 淘汰、win 胜利、paused 暂停、error 异常。
"""
import random
import threading
import time
import uuid

from . import poses
from .vision import MotionJudge, PlayerTracker

ACTIVE = ('countdown', 'green', 'turning', 'red', 'safe')
RESTING = ('idle', 'eliminated', 'win')

REASONS = {'moved': '动了', 'wrong_pose': '姿势不对', 'left_view': '离开了视野', 'unclear': '看不清姿势'}
PART_NAMES = {'head': '头部', 'left_arm': '左臂', 'right_arm': '右臂', 'torso': '躯干',
              'left_leg': '左腿', 'right_leg': '右腿'}

CHANTS = {'zh': '一、二、三，木头人', 'ko': '무궁화 꽃이 피었습니다'}
PEOPLE_STABLE_FRAMES = 4  # 人数连续这么多帧不变才更新，避免画面边缘的人反复出现消失时页面闪动


def _number(kind, low, high):
    def check(value):
        value = kind(value)
        if not low <= value <= high:
            raise ValueError(f'应在 {low}～{high} 之间')
        return value
    return check


def _span(low, high):
    def check(value):
        a, b = (float(v) for v in value)
        if not low <= a <= b <= high:
            raise ValueError(f'应为 [最小, 最大]，范围 {low}～{high}')
        return [a, b]
    return check


def _choice(*options):
    def check(value):
        if value not in options:
            raise ValueError(f'可选 {", ".join(options)}')
        return value
    return check


def _poses(value):
    ids = [v for v in value if v in poses.POSES]
    if not ids:
        raise ValueError('至少保留一个已知姿势')
    return ids


def _difficulties(value):
    out = {}
    for name, item in value.items():
        out[name] = {'threshold': _number(float, 0.005, 1.0)(item['threshold']),
                     'consecutive': _number(int, 1, 30)(item['consecutive'])}
    return out


FIELDS = {
    'rounds': _number(int, 1, 20),
    'countdown_s': _number(float, 0, 10),
    'green_s': _span(1, 20),
    'red_s': _span(1, 30),
    'grace_s': _number(float, 0.2, 3),
    'safe_s': _number(float, 0.5, 5),
    'result_hold_s': _number(float, 1, 60),
    'lost_frames': _number(int, 1, 60),
    'jitter_floor': _number(float, 0, 0.3),
    'still_threshold': _number(float, 0.1, 50),
    'still_frames': _number(int, 1, 30),
    'settle_timeout_s': _number(float, 0, 10),
    'unclear_policy': _choice('pass', 'eliminate'),
    'language': _choice(*CHANTS),
    'poses': _poses,
    'difficulties': _difficulties,
    'difficulty': None,
}

# 新增字段的默认值，旧的 game.json 里没有这些键时使用
DEFAULTS = {
    'jitter_floor': 0.03,      # 每个关键点最低容忍的抖动，单位为躯干长度
    'still_threshold': 2.5,    # 画面晃动量低于此值算静止，见 vision.frame_motion
    'still_frames': 4,         # 连续多少帧静止才算转身停稳
    'settle_timeout_s': 3.0,   # 估算到位后最多再等多久，超时也开始红灯
}


def validate(cfg, changes):
    """返回合并后的新配置；有非法字段时抛出 ValueError，cfg 不变。"""
    merged = {**DEFAULTS, **cfg}
    for key, value in changes.items():
        if key not in FIELDS:
            raise ValueError(f'未知字段 {key}')
        if FIELDS[key] is not None:
            try:
                merged[key] = FIELDS[key](value)
            except (TypeError, ValueError, KeyError) as error:
                raise ValueError(f'{key}：{error}') from None
        else:
            merged[key] = value
    if merged['difficulty'] not in merged['difficulties']:
        raise ValueError(f'difficulty：可选 {", ".join(merged["difficulties"])}')
    return merged


class Game:
    def __init__(self, cfg, servo, camera, model, emit, snapshots, vis_min):
        self.cfg = validate(cfg, {})
        self.servo, self.camera, self.model = servo, camera, model
        self.emit, self.snapshots = emit, snapshots
        self.vis_min = vis_min
        self.lock = threading.RLock()
        self.tracker = PlayerTracker()
        self.judge = MotionJudge(vis_min=vis_min)
        self.random = random.Random()
        self.phase, self.sub = 'idle', None
        self.phase_t = time.monotonic()
        self.stage_end = None
        self.stage_duration = None
        self.round = 0
        self.pose = None
        self.run_id = None
        self.message = None
        self.people = 0
        self.still = 0
        self.settle_pending = True  # 舵机刚转动、还没停发脉冲；服务启动时会先转到待机位
        self._people_seen = 0
        self._people_streak = 0
        self.last_verdict = None
        self.pose_samples = []
        self.lost = 0
        self.pose_checked = True
        self._last = None
        self._health = None
        self._servo_key = None

    # ---- 对外接口 ----

    def update_config(self, changes):
        with self.lock:
            self.cfg = validate(self.cfg, changes)
            self._emit_state()
            return self.cfg

    def action(self, name):
        """返回 (是否成功, 说明)。"""
        with self.lock:
            if name == 'start':
                return self._start()
            if name in ('stop', 'reset'):
                self.run_id = None
                self._goto('standby')
                self._enter('idle', message='已停止' if name == 'stop' else None)
                return True, '已回到待机'
            if name == 'pause':
                if self.phase not in ACTIVE:
                    return False, '当前没有进行中的回合'
                self._enter('paused', message='已暂停，继续后从本轮绿灯重新开始')
                return True, '已暂停'
            if name == 'resume':
                if self.phase != 'paused':
                    return False, '当前没有暂停'
                self._start_round()
                return True, f'从第 {self.round} 轮重新开始'
            return False, f'未知操作 {name}'

    def can_test_servo(self):
        return self.phase not in ACTIVE

    def note_manual_move(self):
        """调试页手动转动舵机后调用，停稳后同样停发脉冲。"""
        with self.lock:
            self.still = 0
            self.settle_pending = True

    def on_frame(self, frame, people, motion=None):
        """每帧推理结果。motion 为画面晃动量（vision.frame_motion），用于判断转身后是否停稳。
        返回附加在 vision 事件里的玩家分析。"""
        with self.lock:
            self._last = (frame, people)
            self._count_people(len(people))
            info = {'player_index': None, 'pose_ok': None, 'value': None, 'raw': None, 'threshold': None,
                    'parts': {}, 'judging': False}
            player = None
            if self.phase in RESTING or self.phase in ('paused', 'error'):
                player = self.tracker.largest(people)
            elif self.phase == 'countdown':
                player = self.tracker.lock(people)
            elif self.phase == 'red' and frame.t >= self.phase_t:
                player = self._judge_frame(frame, people, info)
            elif self.phase == 'safe':
                player = self.tracker.find(people, strict=False)
            if self.settle_pending:
                self._count_still(frame, motion)
            if player is not None:
                info['player_index'] = next(i for i, p in enumerate(people) if p is player)
                if self.pose is not None and info['pose_ok'] is None:
                    info['pose_ok'] = poses.evaluate(self.pose, player, self.vis_min)
            return info

    def tick(self):
        with self.lock:
            self.servo.tick()
            self._check_health()
            now = time.monotonic()
            due = self.stage_end is not None and now >= self.stage_end
            if self.phase == 'countdown' and due:
                if self.tracker.ref is None:
                    self._enter('idle', message='倒计时结束时没有检测到玩家')
                else:
                    self._start_round()
            elif self.phase == 'green' and due:
                self._goto('face')
                self._enter('turning')
            elif self.phase == 'turning' and now >= self.servo.settled_at:
                # 舵机没有位置反馈：估算到位后，还要画面连续静止几帧才算停稳；等太久也开始，避免卡住
                if self.still >= self.cfg['still_frames']:
                    self._enter_red()
                elif now >= self.servo.settled_at + self.cfg['settle_timeout_s']:
                    self._enter_red(message='画面一直没有完全静止，已开始判定')
            elif self.phase == 'red' and self.sub == 'grace' and due:
                self._end_grace()
            elif self.phase == 'red' and self.sub == 'watch' and due:
                self._verdict('safe')
                self._enter('safe', self.cfg['safe_s'])
            elif self.phase == 'safe' and due:
                if self.round < self.cfg['rounds']:
                    self.round += 1
                    self._start_round()
                else:
                    self._verdict('win')
                    self.emit({'type': 'audio', 'cue': 'win'})
                    self._enter('win', self.cfg['result_hold_s'])
            elif self.phase in ('eliminated', 'win') and due:
                self._enter('idle')
            self._release_if_settled(now)
            self._emit_servo_if_changed()

    def snapshot(self):
        with self.lock:
            return self._state()

    def phase_info(self):
        return self.phase, self.sub

    # ---- 内部 ----

    def _params(self):
        return self.cfg['difficulties'][self.cfg['difficulty']]

    def _count_people(self, n):
        if n == self._people_seen:
            self._people_streak += 1
        else:
            self._people_seen, self._people_streak = n, 1
        if self._people_streak >= PEOPLE_STABLE_FRAMES and n != self.people:
            self.people = n
            if self.phase in RESTING:
                self._emit_state()

    def _start(self):
        if self.phase not in RESTING:
            return False, '当前不能开局'
        if not self.model.ready:
            return False, self.model.error or '姿态模型未就绪'
        if not self.camera.status()['ok']:
            return False, '摄像头不可用'
        if not self.servo.status()['ok']:
            return False, '舵机不可用'
        if self.people == 0:
            return False, '画面中没有人'
        self.run_id = uuid.uuid4().hex[:8]
        self.round = 1
        self.pose = None
        self.last_verdict = None
        self.tracker.reset()
        self._goto('face')
        message = f'画面中有 {self.people} 人，将锁定最近的一位' if self.people > 1 else None
        self._enter('countdown', self.cfg['countdown_s'], message=message)
        return True, '开始倒计时'

    def _start_round(self):
        choices = [p for p in self.cfg['poses'] if p != self.pose] or self.cfg['poses']
        self.pose = self.random.choice(choices)
        duration = self.random.uniform(*self.cfg['green_s'])
        self._goto('back')
        self._enter('green', duration)
        lang = self.cfg['language']
        self.emit({'type': 'audio', 'cue': 'chant', 'lang': lang, 'text': CHANTS[lang],
                   'duration_ms': round(duration * 1000)})

    def _goto(self, name):
        self.servo.goto(name)
        self.still = 0
        self.settle_pending = True

    def _count_still(self, frame, motion):
        """统计舵机估算到位之后的连续静止帧数。"""
        if frame.t < self.servo.settled_at or motion is None:
            return
        self.still = self.still + 1 if motion < self.cfg['still_threshold'] else 0

    def _settled(self, now):
        """舵机是否已停稳：估算到位后画面连续静止，或者超过最长等待。"""
        if now < self.servo.settled_at:
            return False
        return (self.still >= self.cfg['still_frames']
                or now >= self.servo.settled_at + self.cfg['settle_timeout_s'])

    def _release_if_settled(self, now):
        """停稳后停发脉冲：软件脉冲有几微秒的误差，持续输出会让舵机在原位来回修正，带着摄像头抖。"""
        if self.settle_pending and self._settled(now):
            self.settle_pending = False
            if self.servo.hold == 'release':
                self.servo.release()

    def _enter_red(self, message=None):
        self.judge = MotionJudge(vis_min=self.vis_min, jitter_floor=self.cfg['jitter_floor'])
        self.pose_samples = []
        self.lost = 0
        self.pose_checked = True
        self.settle_pending = False
        if self.servo.hold == 'release':
            self.servo.release()
        self.emit({'type': 'audio', 'cue': 'turn'})
        self._enter('red', self.cfg['grace_s'], sub='grace', message=message)

    def _judge_frame(self, frame, people, info):
        if self.sub == 'grace':
            player = self.tracker.find(people, strict=False)
            if player is not None:
                self.judge.add_baseline(player)
                info['pose_ok'] = poses.evaluate(self.pose, player, self.vis_min)
                self.pose_samples.append(info['pose_ok'])
            return player
        player = self.tracker.find(people, strict=True)
        if player is None:
            self.lost += 1
            if self.lost >= self.cfg['lost_frames']:
                self._eliminate('left_view', frame, people)
            return None
        self.lost = 0
        params = self._params()
        result = self.judge.measure(player, params['threshold'], params['consecutive'])
        info.update(value=result['value'], raw=result['raw'], threshold=params['threshold'], parts=result['parts'],
                    judging=True)
        if result['moved']:
            self._eliminate('moved', frame, people, player, result['part'])
        return player

    def _end_grace(self):
        if not self.judge.samples:
            return self._eliminate('left_view')
        recent = self.pose_samples[-5:]
        good, bad, unclear = recent.count(True), recent.count(False), recent.count(None)
        if bad > good and bad >= unclear:
            return self._eliminate('wrong_pose')
        if unclear > good and unclear > bad:
            if self.cfg['unclear_policy'] == 'eliminate':
                return self._eliminate('unclear')
            self.pose_checked = False
        self.judge.lock()
        self.tracker.ref = self.judge.samples[-1]
        self._enter('red', self.random.uniform(*self.cfg['red_s']), sub='watch',
                    message=None if self.pose_checked else '姿势看不清，本轮只判断是否静止')

    def _eliminate(self, reason, frame=None, people=None, player=None, part=None):
        if frame is None and self._last is not None:
            frame, people = self._last
        snapshot = None
        if frame is not None:
            snapshot = self.snapshots.save(frame.image, people or [], player, part, self.run_id, self.round)
        self._verdict('eliminated', reason=reason, part=part, snapshot=snapshot)
        self.emit({'type': 'audio', 'cue': 'eliminated'})
        self._enter('eliminated', self.cfg['result_hold_s'])

    def _verdict(self, result, reason=None, part=None, snapshot=None):
        self.last_verdict = {
            'type': 'verdict', 'run': self.run_id, 'result': result, 'round': self.round,
            'rounds': self.cfg['rounds'], 'reason': reason, 'reason_text': REASONS.get(reason),
            'part': part, 'part_text': PART_NAMES.get(part), 'snapshot': snapshot,
        }
        self.emit(dict(self.last_verdict))

    def _enter(self, phase, duration=None, sub=None, message=None):
        now = time.monotonic()
        self.phase, self.sub = phase, sub
        if phase != 'red' or sub == 'grace':
            self.phase_t = now
        self.stage_duration = duration
        self.stage_end = None if duration is None else now + duration
        self.message = message
        if phase == 'idle':
            self.pose = None
            self.round = 0
        self._emit_state()

    def _state(self):
        now = time.monotonic()
        pose = {'id': self.pose, 'name': poses.POSES[self.pose]['name']} if self.pose else None
        can_start = (self.phase in RESTING and self.people > 0 and self.model.ready
                     and self.camera.status()['ok'] and self.servo.status()['ok'])
        return {
            'type': 'state', 'run': self.run_id, 'phase': self.phase, 'sub': self.sub,
            'round': self.round, 'rounds': self.cfg['rounds'], 'pose': pose,
            'ends_in_ms': None if self.stage_end is None else max(0, round((self.stage_end - now) * 1000)),
            'duration_ms': None if self.stage_duration is None else round(self.stage_duration * 1000),
            'people': self.people, 'can_start': can_start, 'message': self.message,
            'difficulty': self.cfg['difficulty'], 'pose_checked': self.pose_checked,
            'verdict': self.last_verdict,
            'problems': [{'source': source, 'message': text} for source, text in (self._health or ())],
        }

    def _emit_state(self):
        self.emit(self._state())

    def _check_health(self):
        problems = []
        camera, servo = self.camera.status(), self.servo.status()
        if not camera['ok']:
            problems.append(('camera', camera['error']))
        if not servo['ok']:
            problems.append(('servo', servo['error']))
        if not self.model.ready:
            problems.append(('model', self.model.error))
        key = tuple(problems)
        if key == self._health:
            return
        self._health = key
        for source, text in problems:
            self.emit({'type': 'error', 'source': source, 'code': f'{source}_unavailable', 'message': text,
                       'recoverable': source != 'model'})
        if problems and self.phase in ACTIVE:
            self._enter('error', message='；'.join(text for _, text in problems))
        elif not problems and self.phase == 'error':
            self._enter('idle', message='已恢复，可以重新开局')
        else:
            self._emit_state()

    def _emit_servo_if_changed(self):
        status = self.servo.status()
        key = (status['target'], status['us'], status['settled'], status['ok'], status['output'],
               tuple(sorted(status['positions'].items())), status['speed_us_per_s'], status['settle_s'])
        if key != self._servo_key:
            self._servo_key = key
            self.emit({'type': 'servo', **status})
