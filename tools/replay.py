#!/usr/bin/env python3
"""回放录制文件，用新参数重算红灯期间的静止判定，用于调阈值。

录制在调试页或 POST /api/record 开启，文件在 recordings/ 下。每段红灯用宽限期的最后几帧作基准，
和服务端的做法相同。
用法：.venv/bin/python tools/replay.py recordings/rec-xxx.jsonl [--threshold 0.1] [--consecutive 3]
"""
import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server.vision import MotionJudge, Person, PlayerTracker  # noqa: E402


def segments(frames):
    """按连续的 red 阶段切分，返回 [(grace 帧, watch 帧)]。"""
    out, grace, watch = [], [], []
    for f in frames + [{'phase': None, 'sub': None}]:
        if f['phase'] == 'red' and f['sub'] == 'grace':
            if watch:
                out.append((grace, watch))
                grace, watch = [], []
            grace.append(f)
        elif f['phase'] == 'red' and f['sub'] == 'watch':
            watch.append(f)
        elif grace or watch:
            out.append((grace, watch))
            grace, watch = [], []
    return out


def player_of(frame, tracker, strict):
    people = [Person.from_json(p) for p in frame['people']]
    return tracker.find(people, strict=strict)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('file', type=Path)
    parser.add_argument('--threshold', type=float, help='默认取录制时 normal 难度的阈值')
    parser.add_argument('--consecutive', type=int, help='连续超阈值帧数，默认取录制时 normal 难度')
    parser.add_argument('--jitter-floor', type=float, help='最低抖动容忍（躯干长度），默认取录制时的配置')
    parser.add_argument('--vis-min', type=float, default=0.5)
    args = parser.parse_args()

    lines = [json.loads(line) for line in args.file.read_text(encoding='utf-8').splitlines() if line.strip()]
    meta = next((x for x in lines if x['type'] == 'meta'), {})
    level = meta.get('game', {}).get('difficulties', {}).get('normal', {'threshold': 0.1, 'consecutive': 3})
    threshold = args.threshold if args.threshold is not None else level['threshold']
    consecutive = args.consecutive if args.consecutive is not None else level['consecutive']
    floor = args.jitter_floor if args.jitter_floor is not None else meta.get('game', {}).get('jitter_floor', 0.03)
    frames = [x for x in lines if x['type'] == 'frame']
    print(f'{args.file.name}：{len(frames)} 帧；阈值 {threshold}，连续 {consecutive} 帧，最低抖动容忍 {floor}')

    parts = segments(frames)
    if not parts:
        print('没有红灯阶段的帧。录制时需要实际打一局。')
        return
    for index, (grace, watch) in enumerate(parts, 1):
        tracker, judge = PlayerTracker(), MotionJudge(vis_min=args.vis_min, jitter_floor=floor)
        for f in grace:
            player = player_of(f, tracker, strict=False)
            if player is not None:
                judge.add_baseline(player)
        if not judge.lock():
            print(f'第 {index} 段：宽限期内没有找到玩家')
            continue
        tracker.ref = judge.samples[-1]
        values, moved_at, lost = [], None, 0
        for n, f in enumerate(watch):
            player = player_of(f, tracker, strict=True)
            if player is None:
                lost += 1
                continue
            result = judge.measure(player, threshold, consecutive)
            if result['value'] is not None:
                values.append(result['value'])
            if result['moved'] and moved_at is None:
                moved_at = (n, result['part'])
        if values:
            summary = (f'判定值 中位 {statistics.median(values):.3f}、p95 {sorted(values)[int(len(values) * 0.95) - 1]:.3f}、'
                       f'最大 {max(values):.3f}')
        else:
            summary = '没有可比较的帧'
        verdict = f'第 {moved_at[0]} 帧判为动了（{moved_at[1]}）' if moved_at else '未判为动了'
        print(f'第 {index} 段：宽限 {len(grace)} 帧、判定 {len(watch)} 帧、丢失 {lost} 帧；{summary}；{verdict}')


if __name__ == '__main__':
    main()
