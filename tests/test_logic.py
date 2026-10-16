"""姿势规则、静止判定和状态机的逻辑测试，不需要摄像头和舵机。

用法：.venv/bin/python -m unittest tests.test_logic -v
"""
import random
import unittest
from unittest import mock

from server import game as game_module
from server import poses
from server.camera import Frame
from server.game import Game
from server.vision import MotionJudge, Person

HEAD = [(640, 230), (655, 215), (625, 215), (670, 225), (610, 225)]
SHOULDERS = [(700, 300), (580, 300)]
HIPS = [(680, 500), (600, 500)]
LEGS = [(685, 620), (595, 620), (690, 700), (590, 700)]
ARMS = {
    'neutral': [(710, 400), (570, 400), (715, 490), (565, 490)],
    'raise_left': [(715, 200), (570, 400), (720, 100), (565, 490)],
    'raise_right': [(710, 400), (565, 200), (715, 490), (560, 100)],
    'both_up': [(715, 200), (565, 200), (720, 100), (560, 100)],
    't_pose': [(800, 300), (480, 300), (900, 300), (380, 300)],
    'hands_on_hips': [(780, 420), (500, 420), (680, 500), (600, 500)],
}


def person(arms='neutral', dx=0, dy=0, wrist_shift=0):
    elbows_wrists = ARMS[arms]
    points = HEAD + SHOULDERS + [elbows_wrists[0], elbows_wrists[1], elbows_wrists[2], elbows_wrists[3]] + HIPS + LEGS
    kps = [(x + dx, y + dy, 0.9) for x, y in points]
    x, y, v = kps[9]
    kps[9] = (x + wrist_shift, y, v)
    return Person((540 + dx, 180 + dy, 760 + dx, 720 + dy), 0.9, kps)


class PoseTest(unittest.TestCase):
    def test_each_pose_matches_only_itself_or_compatible(self):
        for pose_id in poses.POSES:
            self.assertTrue(poses.evaluate(pose_id, person(pose_id), 0.5), pose_id)
            self.assertFalse(poses.evaluate(pose_id, person('neutral'), 0.5), pose_id)

    def test_left_right_not_confused(self):
        self.assertFalse(poses.evaluate('raise_left', person('raise_right'), 0.5))
        self.assertFalse(poses.evaluate('raise_right', person('raise_left'), 0.5))
        self.assertFalse(poses.evaluate('raise_left', person('both_up'), 0.5))

    def test_invisible_keypoints_give_none(self):
        p = person('raise_left')
        p.kps[9, 2] = 0.1
        self.assertIsNone(poses.evaluate('raise_left', p, 0.5))


class MotionTest(unittest.TestCase):
    def test_still_vs_moved(self):
        judge = MotionJudge()
        for _ in range(3):
            judge.add_baseline(person('t_pose'))
        self.assertTrue(judge.lock())
        still = judge.measure(person('t_pose', dx=2), 0.1, 3)
        self.assertLess(still['value'], 0.05)
        self.assertFalse(still['moved'])
        # 当前帧取最近 3 帧的中值；动作幅度够大时，第一帧取中值后就已超过阈值
        results = [judge.measure(person('t_pose', wrist_shift=80), 0.1, 3) for _ in range(4)]
        self.assertFalse(results[1]['moved'])
        self.assertTrue(results[2]['moved'])
        self.assertEqual(results[2]['part'], 'left_arm')

    def test_one_limb_over_threshold_counts_as_moved(self):
        # 只动一条胳膊：手腕约 0.18、手肘约 0.09 躯干长度。页面会把左臂标红，判定也必须算动
        judge = MotionJudge()
        for _ in range(10):
            judge.add_baseline(person('t_pose'))
        judge.lock()
        moved = person('t_pose')
        moved.kps[9, 1] += 36
        moved.kps[7, 1] += 18
        results = [judge.measure(moved, 0.1, 3) for _ in range(4)]
        self.assertGreater(results[-1]['parts']['left_arm'], 0.1)
        self.assertGreater(results[-1]['value'], 0.1)
        self.assertTrue(results[-1]['moved'])
        self.assertEqual(results[-1]['part'], 'left_arm')

    def test_limb_still_settling_during_grace_is_still_judged(self):
        # 宽限期前半段手还在往上举，后半段停住；之后再动这只手必须能检测到
        judge = MotionJudge()
        for i in range(15):
            p = person('raise_left')
            if i < 8:
                p.kps[9, 1] += (8 - i) * 15
                p.kps[7, 1] += (8 - i) * 8
            judge.add_baseline(p)
        judge.lock()
        self.assertTrue(judge.usable[9] and judge.usable[7])
        moved = person('raise_left')
        moved.kps[9, 0] += 40
        results = [judge.measure(moved, 0.1, 3) for _ in range(4)]
        self.assertTrue(results[-1]['moved'])
        self.assertEqual(results[-1]['part'], 'left_arm')

    def test_keypoint_jitter_is_tolerated(self):
        rng = random.Random(7)

        def jittered():
            p = person('t_pose')
            p.kps[:, :2] += [[rng.uniform(-4, 4), rng.uniform(-4, 4)] for _ in range(17)]
            return p

        judge = MotionJudge()
        for _ in range(15):
            judge.add_baseline(jittered())
        judge.lock()
        results = [judge.measure(jittered(), 0.1, 4) for _ in range(60)]
        self.assertFalse(any(r['moved'] for r in results))
        self.assertLess(max(r['value'] for r in results), 0.03)
        self.assertGreater(max(r['raw'] for r in results), 0)

    def test_very_noisy_keypoint_is_ignored(self):
        rng = random.Random(3)
        samples = []
        for _ in range(15):
            p = person('t_pose')
            p.kps[9, :2] += [rng.uniform(-60, 60), rng.uniform(-60, 60)]
            samples.append(p)
        judge = MotionJudge()
        for p in samples:
            judge.add_baseline(p)
        judge.lock()
        self.assertFalse(judge.usable[9])
        moved_wrist = person('t_pose')
        moved_wrist.kps[9, 0] += 80
        self.assertEqual(judge.measure(moved_wrist, 0.1, 1)['value'], 0.0)


class FakeServo:
    hold = 'release'

    def __init__(self, clock):
        self.clock, self.settled_at, self.moves, self.released = clock, 0, [], 0

    def release(self):
        self.released += 1

    def goto(self, name):
        self.moves.append(name)
        self.settled_at = self.clock.now + 0.5
        return self.settled_at

    def tick(self):
        pass

    def status(self):
        return {'ok': True, 'error': None, 'target': self.moves[-1] if self.moves else None, 'us': 1500,
                'settled': self.clock.now >= self.settled_at, 'output': True,
                'positions': {'standby': 1500, 'face': 1500, 'back': 2500}, 'speed_us_per_s': 1500, 'settle_s': 0.4}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


class GameTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        patcher = mock.patch.object(game_module, 'time', self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        cfg = {'rounds': 2, 'countdown_s': 3, 'green_s': [4, 4], 'red_s': [3, 3], 'grace_s': 0.8, 'safe_s': 1,
               'result_hold_s': 5, 'lost_frames': 5, 'unclear_policy': 'pass', 'language': 'zh',
               'still_threshold': 2.5, 'still_frames': 4, 'settle_timeout_s': 3, 'jitter_floor': 0.03,
               'poses': ['raise_left'], 'difficulty': 'normal',
               'difficulties': {'normal': {'threshold': 0.1, 'consecutive': 3}}}
        camera = mock.Mock(**{'status.return_value': {'ok': True, 'error': None}})
        model = mock.Mock(ready=True, error=None)
        snapshots = mock.Mock(**{'save.return_value': 'snap-1'})
        self.events = []
        self.servo = FakeServo(self.clock)
        self.game = Game(cfg, self.servo, camera, model, self.events.append, snapshots, 0.5)
        self.seq = 0

    def feed(self, p, n=1, motion=0.5):
        for _ in range(n):
            self.seq += 1
            self.clock.now += 0.05
            self.game.on_frame(Frame(self.seq, self.clock.now, None), [p] if p else [], motion)

    def turn_and_grace(self, grace_person):
        self.advance(4)
        self.assertEqual(self.game.phase, 'turning')
        self.assertEqual(self.servo.moves[-1], 'face')
        self.advance(0.6)
        self.assertEqual(self.game.phase, 'turning')
        self.feed(person(), 4)
        self.game.tick()
        self.assertEqual((self.game.phase, self.game.sub), ('red', 'grace'))
        self.feed(grace_person, 5)
        self.advance(0.6)

    def advance(self, seconds):
        self.clock.now += seconds
        self.game.tick()

    def play_to_watch(self, grace_person):
        self.feed(person(), 4)
        self.assertTrue(self.game.action('start')[0])
        self.feed(person(), 3)
        self.advance(3)
        self.assertEqual(self.game.phase, 'green')
        self.assertEqual(self.servo.moves[-1], 'back')
        self.turn_and_grace(grace_person)

    def verdicts(self):
        return [e for e in self.events if e['type'] == 'verdict']

    def test_cannot_start_without_people(self):
        ok, message = self.game.action('start')
        self.assertFalse(ok)
        self.assertIn('没有人', message)

    def test_people_count_needs_stable_frames(self):
        self.feed(person(), 3)
        self.assertEqual(self.game.people, 0)
        self.feed(person())
        self.assertEqual(self.game.people, 1)
        self.feed(None, 2)
        self.feed(person())
        self.assertEqual(self.game.people, 1)

    def test_full_win(self):
        self.play_to_watch(person('raise_left'))
        self.assertEqual(self.game.sub, 'watch')
        self.feed(person('raise_left', dx=1), 10)
        self.advance(3)
        self.assertEqual(self.game.phase, 'safe')
        self.advance(1)
        self.assertEqual((self.game.phase, self.game.round), ('green', 2))
        self.turn_and_grace(person('raise_left'))
        self.feed(person('raise_left'), 10)
        self.advance(3)
        self.advance(1)
        self.assertEqual(self.game.phase, 'win')
        self.assertEqual([v['result'] for v in self.verdicts()], ['safe', 'safe', 'win'])
        self.advance(5)
        self.assertEqual(self.game.phase, 'idle')

    def test_waits_for_still_image_after_turning(self):
        self.feed(person(), 4)
        self.game.action('start')
        self.feed(person(), 3)
        self.advance(3)
        self.advance(4)
        self.advance(0.6)
        self.feed(person(), 10, motion=12)
        self.game.tick()
        self.assertEqual(self.game.phase, 'turning')
        self.feed(person(), 3)
        self.game.tick()
        self.assertEqual(self.game.phase, 'turning')
        self.feed(person())
        self.game.tick()
        self.assertEqual((self.game.phase, self.game.sub), ('red', 'grace'))
        self.assertEqual(self.servo.released, 1)

    def test_pulses_stop_after_every_settled_move(self):
        self.feed(person(), 4)
        self.assertTrue(self.game.settle_pending)
        self.advance(0.1)
        self.assertEqual(self.servo.released, 1)
        self.game.action('start')
        self.assertTrue(self.game.settle_pending)
        self.feed(person(), 15)
        self.game.tick()
        self.assertEqual(self.servo.released, 2)
        self.advance(2.5)
        self.assertEqual(self.game.phase, 'green')
        self.advance(0.6)
        self.feed(None, 4)
        self.game.tick()
        self.assertEqual(self.servo.released, 3)

    def test_busy_scene_still_releases_after_timeout(self):
        self.servo.goto('face')
        self.game.note_manual_move()
        self.feed(person(), 4, motion=12)
        self.advance(1)
        self.assertEqual(self.servo.released, 0)
        self.advance(3)
        self.assertEqual(self.servo.released, 1)

    def test_settle_timeout_starts_red_anyway(self):
        self.feed(person(), 4)
        self.game.action('start')
        self.feed(person(), 3)
        self.advance(3)
        self.advance(4)
        self.advance(0.6)
        self.feed(person(), 20, motion=12)
        self.game.tick()
        self.assertEqual(self.game.phase, 'turning')
        self.advance(3)
        self.assertEqual((self.game.phase, self.game.sub), ('red', 'grace'))
        self.assertIn('没有完全静止', self.game.message)

    def test_moved_is_eliminated(self):
        self.play_to_watch(person('raise_left'))
        self.feed(person('raise_left', wrist_shift=80), 3)
        self.assertEqual(self.game.phase, 'eliminated')
        verdict = self.verdicts()[-1]
        self.assertEqual((verdict['reason'], verdict['part'], verdict['snapshot']), ('moved', 'left_arm', 'snap-1'))

    def test_wrong_pose_is_eliminated(self):
        self.play_to_watch(person('neutral'))
        self.assertEqual(self.game.phase, 'eliminated')
        self.assertEqual(self.verdicts()[-1]['reason'], 'wrong_pose')

    def test_left_view_is_eliminated(self):
        self.play_to_watch(person('raise_left'))
        self.feed(None, 5)
        self.assertEqual(self.game.phase, 'eliminated')
        self.assertEqual(self.verdicts()[-1]['reason'], 'left_view')

    def test_frames_before_red_are_ignored(self):
        self.play_to_watch(person('raise_left'))
        stale = Frame(999, self.game.phase_t - 0.1, None)
        info = self.game.on_frame(stale, [person('raise_left', wrist_shift=200)])
        self.assertFalse(info['judging'])
        self.assertEqual(self.game.phase, 'red')

    def test_pause_and_resume_restarts_round(self):
        self.play_to_watch(person('raise_left'))
        self.assertTrue(self.game.action('pause')[0])
        self.assertEqual(self.game.phase, 'paused')
        self.assertTrue(self.game.action('resume')[0])
        self.assertEqual((self.game.phase, self.game.round), ('green', 1))

    def test_invalid_config_is_rejected(self):
        with self.assertRaises(ValueError):
            self.game.update_config({'rounds': 0})
        with self.assertRaises(ValueError):
            self.game.update_config({'difficulty': 'nightmare'})
        self.assertEqual(self.game.cfg['rounds'], 2)


if __name__ == '__main__':
    unittest.main()
