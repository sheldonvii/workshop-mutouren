"""赛博木头人服务：摄像头采集、姿态推理、游戏状态机、舵机和网页推送。

在项目根目录运行：./run.sh，或 .venv/bin/python -m server.app
"""
import argparse
import json
import logging
import mimetypes
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import cv2

from . import poses
from .camera import Camera
from .game import Game
from .hub import PROTOCOL, Hub
from .recorder import Recorder
from .servo import Servo
from .snapshots import SnapshotStore
from .vision import PoseModel, frame_motion

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / 'web'
HARDWARE_CFG = ROOT / 'configs' / 'hardware.json'
GAME_CFG = ROOT / 'configs' / 'game.json'
VERSION = '0.1.0'

log = logging.getLogger('mutouren')
mimetypes.add_type('text/javascript', '.js')


def load_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def save_json(path, data):
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


class Service:
    def __init__(self):
        self.hw = load_json(HARDWARE_CFG)
        game_cfg = load_json(GAME_CFG)
        self.hub = Hub(on_connect=self.initial_events)
        self.camera = Camera(self.hw['camera'])
        self.model = PoseModel(self.hw['vision'])
        self.servo = Servo(self.hw['servo'])
        self.recorder = Recorder(ROOT / 'recordings')
        self.game = Game(game_cfg, self.servo, self.camera, self.model, self.hub.event,
                         SnapshotStore(ROOT / 'snapshots'), self.model.vis_min)
        self.running = False
        self.vision_fps = 0.0
        self.infer_ms = None

    def start(self):
        self._keep_off_servo_cpu()
        self.model.load()
        log.info('姿态模型：%s', self.model.error or f'已加载（{self.model.load_s}s）')
        self.servo.start()
        log.info('舵机：%s，%s', self.servo.driver, self.servo.note)
        self.camera.start()
        self.running = True
        threading.Thread(target=self._pipeline, daemon=True, name='pipeline').start()
        threading.Thread(target=self._ticker, daemon=True, name='ticker').start()

    def _keep_off_servo_cpu(self):
        """让服务和推理线程不使用舵机脉冲进程独占的那个 CPU。须在加载模型、启动线程之前调用，之后创建的线程会继承。"""
        servo = self.hw['servo']
        if servo.get('driver') != 'gpio':
            return
        allowed = os.sched_getaffinity(0)
        cpu = int(servo.get('cpu', 7))
        if cpu not in allowed:
            cpu = max(allowed)
        if len(allowed) > 1:
            os.sched_setaffinity(0, allowed - {cpu})
            log.info('服务使用 CPU %s，CPU%d 留给舵机脉冲进程', ','.join(map(str, sorted(allowed - {cpu}))), cpu)

    def stop(self):
        self.running = False
        self.recorder.stop()
        self.hub.close_all()
        self.camera.stop()
        self.servo.stop()

    def initial_events(self):
        return [self.hello(), self.game.snapshot(), {'type': 'servo', **self.servo.status()}]

    def hello(self):
        return {'type': 'hello', 'protocol': PROTOCOL, 'version': VERSION, 'config': self.config()}

    def config(self):
        game = self.game.cfg
        return {'game': game, 'poses': poses.catalog(list(poses.POSES)),
                'servo': {k: v for k, v in self.servo.status().items()
                          if k in ('driver', 'positions', 'speed_us_per_s', 'settle_s', 'hold')}}

    def health(self):
        return {'ok': self.camera.status()['ok'] and self.model.ready and self.servo.status()['ok'],
                'version': VERSION, 'protocol': PROTOCOL, 'phase': self.game.phase,
                'camera': self.camera.status(), 'model': self.model.status(), 'servo': self.servo.status(),
                'recorder': self.recorder.status(), 'clients': self.hub.count()}

    def metrics(self):
        camera = self.camera.status()
        return {'type': 'metrics', 'camera_fps': camera['fps'], 'vision_fps': round(self.vision_fps, 1),
                'infer_ms': None if self.infer_ms is None else round(self.infer_ms, 1),
                'size': camera['size'], 'clients': self.hub.count(), 'servo_driver': self.servo.driver,
                'recorder': self.recorder.status()}

    def _pipeline(self):
        preview = self.hw['preview']
        seq, count, mark, error_shown = -1, 0, time.monotonic(), False
        previous_small = None
        while self.running:
            frame = self.camera.wait(seq, 1.0)
            if frame is None:
                continue
            seq = frame.seq
            motion, previous_small = frame_motion(frame.image, previous_small)
            people, infer_ms = [], None
            if self.model.ready:
                try:
                    people, infer_ms = self.model.infer(frame.image)
                    error_shown = False
                except Exception as error:
                    if not error_shown:
                        log.exception('推理失败')
                        self.hub.event({'type': 'error', 'source': 'model', 'code': 'infer_failed',
                                        'message': f'推理失败：{error}', 'recoverable': True})
                        error_shown = True
            info = self.game.on_frame(frame, people, motion)
            phase, sub = self.game.phase_info()
            self.recorder.write_frame(frame, phase, sub, people, info, motion)
            h, w = frame.image.shape[:2]
            small = cv2.resize(frame.image, (preview['width'], round(h * preview['width'] / w)),
                               interpolation=cv2.INTER_AREA)
            ok, jpeg = cv2.imencode('.jpg', small, [cv2.IMWRITE_JPEG_QUALITY, int(preview['quality'])])
            if ok:
                self.hub.frame({'type': 'vision', 'seq': frame.seq, 'size': [w, h], 'phase': phase, 'sub': sub,
                                'count': len(people), 'people': [p.to_json() for p in people],
                                'player': info, 'motion': None if motion is None else round(motion, 2),
                                'infer_ms': None if infer_ms is None else round(infer_ms, 1)},
                               jpeg.tobytes())
            count += 1
            if infer_ms is not None:
                self.infer_ms = infer_ms if self.infer_ms is None else 0.9 * self.infer_ms + 0.1 * infer_ms
            now = time.monotonic()
            if now - mark >= 1:
                self.vision_fps, count, mark = count / (now - mark), 0, now

    def _ticker(self):
        last_metrics = 0.0
        while self.running:
            try:
                self.game.tick()
            except Exception:
                log.exception('状态机出错')
            now = time.monotonic()
            if now - last_metrics >= 1:
                last_metrics = now
                self.hub.event(self.metrics())
            time.sleep(0.02)

    def update_config(self, body):
        if 'game' in body:
            self.game.update_config(body['game'])
        servo = body.get('servo') or {}
        if servo:
            self.servo.update(positions=servo.get('positions'), speed_us_per_s=servo.get('speed_us_per_s'),
                              settle_s=servo.get('settle_s'))
        if body.get('save'):
            save_json(GAME_CFG, self.game.cfg)
            status = self.servo.status()
            self.hw['servo'].update(positions=status['positions'], speed_us_per_s=status['speed_us_per_s'],
                                    settle_s=status['settle_s'])
            save_json(HARDWARE_CFG, self.hw)
        config = self.config()
        self.hub.event({'type': 'hello', 'protocol': PROTOCOL, 'version': VERSION, 'config': config})
        return config

    def servo_test(self, body):
        if not self.game.can_test_servo():
            raise PermissionError('游戏进行中，不能手动转动舵机')
        if 'position' in body:
            self.servo.goto(body['position'])
        elif 'us' in body:
            self.servo.set_us(int(body['us']))
        else:
            raise ValueError('需要 position 或 us')
        self.game.note_manual_move()
        return self.servo.status()

    def record(self, action):
        if action == 'start':
            return {'file': self.recorder.start({'hardware': self.hw, 'game': self.game.cfg}),
                    **self.recorder.status()}
        if action == 'stop':
            self.recorder.stop()
            return self.recorder.status()
        raise ValueError('action 应为 start 或 stop')


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):
            log.debug('%s %s', self.address_string(), fmt % args)

        def _json(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            size = int(self.headers.get('Content-Length') or 0)
            if size > 65536:
                raise ValueError('请求体过大')
            return json.loads(self.rfile.read(size) or b'{}') if size else {}

        def _file(self, path, cache=False):
            data = path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'max-age=3600' if cache else 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == '/api/ws':
                return service.hub.serve(self)
            if path == '/api/health':
                return self._json(200, service.health())
            if path == '/api/config':
                return self._json(200, service.config())
            if path.startswith('/api/snapshots/'):
                snap = service.game.snapshots.path(path.rsplit('/', 1)[1].removesuffix('.jpg'))
                return self._file(snap, cache=True) if snap else self._json(404, {'error': '抓拍不存在'})
            if path.startswith('/api/'):
                return self._json(404, {'error': '未知接口'})
            return self._static(path)

        def _static(self, path):
            name = {'/': 'index.html', '/debug': 'debug.html'}.get(path, path.lstrip('/'))
            target = (WEB / name).resolve()
            if WEB.resolve() not in target.parents or not target.is_file():
                return self._json(404, {'error': '页面不存在'})
            return self._file(target)

        def _post(self, handler):
            try:
                self._json(200, handler(self._body()))
            except PermissionError as error:
                self._json(409, {'error': str(error)})
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
                self._json(400, {'error': str(error)})

        def do_POST(self):
            path = urlparse(self.path).path
            if path == '/api/game':
                def game(body):
                    ok, message = service.game.action(body.get('action', ''))
                    if not ok:
                        raise PermissionError(message)
                    return {'ok': True, 'message': message, 'state': service.game.snapshot()}
                return self._post(game)
            if path == '/api/servo/test':
                return self._post(service.servo_test)
            if path == '/api/record':
                return self._post(lambda body: service.record(body.get('action')))
            self._json(404, {'error': '未知接口'})

        def do_PUT(self):
            if urlparse(self.path).path == '/api/config':
                return self._post(service.update_config)
            self._json(404, {'error': '未知接口'})

    return Handler


def main():
    parser = argparse.ArgumentParser(description='赛博木头人服务')
    parser.add_argument('--host', help='监听地址，默认取 configs/hardware.json')
    parser.add_argument('--port', type=int, help='端口，默认取 configs/hardware.json')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s', datefmt='%H:%M:%S')

    service = Service()
    host = args.host or service.hw['server']['host']
    port = args.port or service.hw['server']['port']
    try:
        server = ThreadingHTTPServer((host, port), make_handler(service))
    except OSError as error:
        raise SystemExit(f'无法监听 {host}:{port}（{error.strerror}）。可能已有服务在运行：pgrep -af server.app')
    server.daemon_threads = True
    service.start()

    def shutdown(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.info('服务已启动：http://%s:%d/  调试页 /debug', host, port)
    if host not in ('127.0.0.1', 'localhost'):
        log.info('监听 %s，同一局域网内都能打开页面看到摄像头画面', host)
    try:
        server.serve_forever()
    finally:
        service.stop()
        server.server_close()
        log.info('已停止：舵机回到待机位并停止输出，摄像头已释放')


if __name__ == '__main__':
    main()
