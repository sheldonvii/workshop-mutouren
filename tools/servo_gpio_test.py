#!/usr/bin/env python3
"""用 GPIO 软件脉冲测试 PWM 舵机，不经过游戏服务。

GPIO 操作复用 server/servo.py。需要 /dev/gpiochip* 的读写权限，见 scripts/setup_k3_gpio.sh。
K3 Pico-ITX 26 针 FPC：第 11 脚 RX_PWM1、第 12 脚 RX_PWM2，分别是 d4019000.gpio bank0 的第 28、29 根线。
本工程的转接板编号与 K3 相反：转接板第 n 号 = K3 第 27−n 脚，RX_PWM2 在转接板 15 号。
"""
import argparse
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server.servo import PULSE_MAX_US, PULSE_MIN_US, GpioLine, pulse_once, realtime, wait_next_period  # noqa: E402

CHIP_LABEL = 'd4019000.gpio-bank0'
FPC_PIN_TO_LINE = {11: 28, 12: 29}


def drive(line, width_us, seconds):
    widths = []
    frame = time.perf_counter_ns()
    end = frame + int(seconds * 1e9)
    while frame < end:
        widths.append(pulse_once(line, width_us))
        frame = wait_next_period(frame)
    return widths


def parse_sequence(text):
    steps = []
    for item in text.split(','):
        width, seconds = item.split(':')
        width, seconds = int(width), float(seconds)
        if not PULSE_MIN_US <= width <= PULSE_MAX_US:
            raise SystemExit(f'脉宽 {width}μs 超出 {PULSE_MIN_US}～{PULSE_MAX_US}μs')
        steps.append((width, seconds))
    return steps


def hold(line, mode, seconds):
    end = time.monotonic() + seconds
    level = 0 if mode == 'low' else 1
    while time.monotonic() < end:
        line.set(level)
        time.sleep(min(1.0, max(0.0, end - time.monotonic())))
        if mode == 'blink':
            level ^= 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--fpc-pin', type=int, choices=sorted(FPC_PIN_TO_LINE), default=12,
                        help='舵机白线所接的 K3 FPC 针脚（默认 12）')
    parser.add_argument('--sequence', default='1500:2,1200:1.5,1800:1.5,1500:1.5',
                        help='脉宽μs:持续秒，逗号分隔；默认在中位附近小幅往返')
    parser.add_argument('--hold', choices=['high', 'low', 'blink'],
                        help='不发脉冲：保持高/低电平，或每秒高低交替（blink），便于用万用表找针脚')
    parser.add_argument('--seconds', type=float, default=15, help='--hold 的持续时间（秒）')
    args = parser.parse_args()
    steps = parse_sequence(args.sequence)

    line_no = FPC_PIN_TO_LINE[args.fpc_pin]
    try:
        line = GpioLine(CHIP_LABEL, line_no, consumer=b'servo-gpio-test')
    except (PermissionError, FileNotFoundError, RuntimeError) as error:
        raise SystemExit(str(error))
    where = f'{line.chip_path}（{CHIP_LABEL}）第 {line_no} 根线 = FPC 第 {args.fpc_pin} 脚'
    try:
        if args.hold:
            names = {'high': '保持高电平', 'low': '保持低电平', 'blink': '每秒高低交替'}
            print(f'{where}；{names[args.hold]} {args.seconds:g} 秒', flush=True)
            hold(line, args.hold, args.seconds)
        else:
            print(f'{where}；{realtime(max(os.sched_getaffinity(0)))}', flush=True)
            for width, seconds in steps:
                widths = drive(line, width, seconds)
                errors = sorted(abs(w - width) for w in widths)
                print(f'目标 {width}μs × {seconds}s：{len(widths)} 个脉冲，实测中位 {statistics.median(widths):.0f}μs，'
                      f'误差 p95 {errors[int(len(errors) * 0.95) - 1]:.0f}μs、最大 {errors[-1]:.0f}μs', flush=True)
    finally:
        line.close()
    print('结束，信号线已置低并释放')


if __name__ == '__main__':
    main()
