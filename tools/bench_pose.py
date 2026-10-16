#!/usr/bin/env python3
"""比较姿态模型：推理耗时、关键点抖动，以及"裁出玩家区域再推理"的效果。

抖动的测法：对同一张测试图反复加入传感器噪声和亚像素平移，模拟摄像头逐帧的细微变化，
统计玩家各关键点位置的标准差（单位为躯干长度）。另做一份把人缩小一半的"远处"版本。
服务运行时也能跑，但会与服务争用 CPU，耗时偏高；比较各模型时条件相同即可。
用法：.venv/bin/python tools/bench_pose.py [--image 图片] [--models n,s,m] [--threads 4] [--runs 20]
"""
import argparse
import statistics
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from server.vision import PlayerTracker, PoseModel, body_scale  # noqa: E402

IMAGE = ROOT / 'tools' / 'testdata' / 'pose-sample.jpg'


def far_version(image, factor=0.5):
    """把画面缩小后居中放在同尺寸灰底上，模拟人站得更远。"""
    h, w = image.shape[:2]
    small = cv2.resize(image, (int(w * factor), int(h * factor)), interpolation=cv2.INTER_AREA)
    canvas = np.full_like(image, 120)
    y, x = (h - small.shape[0]) // 2, (w - small.shape[1]) // 2
    canvas[y:y + small.shape[0], x:x + small.shape[1]] = small
    return canvas


def jitter(image, rng):
    dx, dy = rng.uniform(-0.8, 0.8, 2)
    shifted = cv2.warpAffine(image, np.float32([[1, 0, dx], [0, 1, dy]]), (image.shape[1], image.shape[0]),
                             borderMode=cv2.BORDER_REFLECT)
    noisy = shifted.astype(np.float32) + rng.normal(0, 4, image.shape)
    return np.clip(noisy, 0, 255).astype(np.uint8), (dx, dy)


def crop_box(person, shape, margin=0.25):
    x1, y1, x2, y2 = person.box
    w, h = x2 - x1, y2 - y1
    x1, x2 = max(0, x1 - w * margin), min(shape[1], x2 + w * margin)
    y1, y2 = max(0, y1 - h * margin), min(shape[0], y2 + h * margin)
    return int(x1), int(y1), int(x2), int(y2)


def run_case(model, image, runs, crop, rng):
    reference = PlayerTracker.largest(model.infer(image)[0])
    if reference is None:
        return None
    box = crop_box(reference, image.shape) if crop else None
    tracker = PlayerTracker()
    tracker.ref = reference
    points, scales, times = [], [], []
    for _ in range(runs):
        frame, (dx, dy) = jitter(image, rng)
        t = time.perf_counter()
        if crop:
            x1, y1, x2, y2 = box
            people, _ = model.infer(np.ascontiguousarray(frame[y1:y2, x1:x2]))
            for p in people:
                p.kps[:, 0] += x1
                p.kps[:, 1] += y1
                p.box += [x1, y1, x1, y1]
        else:
            people, _ = model.infer(frame)
        times.append((time.perf_counter() - t) * 1000)
        player = tracker.find(people, strict=True)
        if player is None:
            points.append(None)
            continue
        kps = player.kps.copy()
        kps[:, 0] -= dx
        kps[:, 1] -= dy
        points.append(kps)
        scales.append(body_scale(player, model.vis_min))
    found = [p for p in points if p is not None]
    if len(found) < 3:
        return {'found': len(found), 'ms': statistics.median(times)}
    stack = np.stack(found)
    visible = (stack[:, :, 2] >= model.vis_min).all(axis=0)
    spread = np.sqrt(((stack[:, :, :2] - stack[:, :, :2].mean(axis=0)) ** 2).sum(axis=2).mean(axis=0))
    spread = spread[visible] / statistics.median(scales)
    return {'found': len(found), 'visible': int(visible.sum()), 'ms': statistics.median(times),
            'median': float(np.median(spread)), 'p90': float(np.percentile(spread, 90)), 'max': float(spread.max())}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--image', type=Path, default=IMAGE, help='测试图片，默认 tools/testdata/pose-sample.jpg')
    parser.add_argument('--models', default='n,s,m')
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--runs', type=int, default=20)
    args = parser.parse_args()
    base = cv2.imread(str(args.image))
    if base is None:
        sys.exit(f'读不到图片 {args.image}')
    images = {'近': base, '远': far_version(base)}
    print(f'测试图 {args.image.name}，每项 {args.runs} 次，推理线程 {args.threads}；抖动为关键点位置标准差（躯干长度）')
    print('模型  画面 方式   可见点  中位耗时  抖动中位  抖动p90  抖动最大')
    for size in args.models.split(','):
        config = ROOT / 'configs' / f'yolov8{size}-pose.yaml'
        text = config.read_text(encoding='utf-8').replace('num_threads: 4', f'num_threads: {args.threads}')
        tmp = ROOT / 'configs' / f'.bench-{size}.yaml'
        tmp.write_text(text, encoding='utf-8')
        model = PoseModel({'config': str(tmp)})
        model.load()
        tmp.unlink()
        if not model.ready:
            print(size, model.error)
            continue
        for name, image in images.items():
            for crop in (False, True):
                r = run_case(model, image, args.runs, crop, np.random.default_rng(1))
                label = '裁剪' if crop else '整幅'
                if r is None or 'median' not in r:
                    print(f'{size:>4}  {name}   {label}   没有稳定找到玩家')
                    continue
                print(f'{size:>4}  {name}   {label}   {r["visible"]:>4}   {r["ms"]:>6.1f}ms   {r["median"]:.4f}   '
                      f'{r["p90"]:.4f}   {r["max"]:.4f}')


if __name__ == '__main__':
    main()
