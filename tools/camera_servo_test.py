#!/usr/bin/env python3
"""摄像头 + 舵机联调页面，不经过游戏服务，用于装配和标定。

复用 server/camera.py 和 server/servo.py，参数取 configs/hardware.json。网页显示摄像头画面，
按钮、滑块和方向键控制舵机脉宽，用来找"面对玩家"和"背对玩家"两档的脉宽。
启动后不输出脉冲，按下按钮才开始。不要和游戏服务同时运行，两者共用摄像头和 GPIO 线。
用法：.venv/bin/python tools/camera_servo_test.py [--port 8322]
"""
import argparse
import json
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from server.camera import Camera  # noqa: E402
from server.servo import PULSE_MAX_US, PULSE_MIN_US, Servo  # noqa: E402

PREVIEW_WIDTH = 640

PAGE = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>摄像头舵机联调</title>
<style>
body{margin:0;font-family:system-ui,"Noto Sans CJK SC",sans-serif;background:#111;color:#eee;display:flex;flex-direction:column;align-items:center;gap:12px;padding:16px}
img{max-width:100%;width:960px;background:#000;border-radius:8px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;justify-content:center}
button{font-size:18px;padding:10px 18px;border-radius:8px;border:0;background:#2d6cdf;color:#fff;cursor:pointer}
button.stop{background:#a33}
input[type=range]{width:min(640px,90vw)}
#status{font-variant-numeric:tabular-nums;color:#aaa}
</style></head><body>
<img src="/stream.mjpg" alt="摄像头实时画面">
<div class="row">
<button data-us="1000">1000μs</button><button data-us="1500">1500μs</button><button data-us="2000">2000μs</button>
<button id="sweep">来回扫一遍</button><button class="stop" data-us="0">停止输出</button>
</div>
<div class="row"><input id="slider" type="range" min="500" max="2500" step="10" value="1500"><span id="val">1500μs</span></div>
<div id="status">连接中…</div>
<p style="color:#888;font-size:14px">方向键 ← → 每次 50μs；注意摄像头 USB 线不要被拉扯</p>
<script>
const set = us => fetch('/servo?us=' + us, {method: 'POST'});
document.querySelectorAll('button[data-us]').forEach(b => b.onclick = () => set(b.dataset.us));
const slider = document.getElementById('slider'), val = document.getElementById('val');
slider.oninput = () => { val.textContent = slider.value + 'μs'; set(slider.value); };
document.getElementById('sweep').onclick = async () => {
  for (const us of [1500, 1000, 1500, 2000, 1500]) { await set(us); await new Promise(r => setTimeout(r, 1200)); }
};
let current = 1500;
addEventListener('keydown', e => {
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
  current = Math.max(500, Math.min(2500, current + (e.key === 'ArrowLeft' ? -50 : 50)));
  slider.value = current; val.textContent = current + 'μs'; set(current);
});
setInterval(async () => {
  try {
    const s = await (await fetch('/status')).json();
    if (s.us) current = s.us;
    document.getElementById('status').textContent =
      `舵机 ${s.us ? s.us + 'μs' : '未输出'}（${s.driver}） ｜ 摄像头 ${s.size[0]}×${s.size[1]} ${s.fps.toFixed(1)}fps`;
  } catch { document.getElementById('status').textContent = '服务未连接'; }
}, 500);
</script></body></html>'''


def make_handler(camera, servo):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == '/':
                self._send(200, PAGE.encode(), 'text/html; charset=utf-8')
            elif path == '/status':
                s, c = servo.status(), camera.status()
                body = {'us': s['us'] if s['output'] else 0, 'driver': s['driver'], 'fps': c['fps'], 'size': c['size']}
                self._send(200, json.dumps(body).encode(), 'application/json')
            elif path == '/stream.mjpg':
                self._stream()
            else:
                self._send(404, b'not found', 'text/plain')

        def _stream(self):
            self.send_response(200)
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            seq = -1
            try:
                while camera.running:
                    frame = camera.wait(seq)
                    if frame is None:
                        continue
                    seq = frame.seq
                    h, w = frame.image.shape[:2]
                    small = cv2.resize(frame.image, (PREVIEW_WIDTH, round(h * PREVIEW_WIDTH / w)))
                    ok, jpeg = cv2.imencode('.jpg', small, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    if ok:
                        data = jpeg.tobytes()
                        self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '
                                         + str(len(data)).encode() + b'\r\n\r\n' + data + b'\r\n')
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            url = urlparse(self.path)
            if url.path != '/servo':
                return self._send(404, b'not found', 'text/plain')
            try:
                us = int(parse_qs(url.query).get('us', ['-1'])[0])
            except ValueError:
                us = -1
            if us == 0:
                servo.release()
            elif PULSE_MIN_US <= us <= PULSE_MAX_US:
                servo.set_us(us)
            else:
                return self._send(400, b'us must be 0 or 500-2500', 'text/plain')
            self._send(200, json.dumps({'us': us}).encode(), 'application/json')

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8322)
    args = parser.parse_args()
    hw = json.loads((ROOT / 'configs' / 'hardware.json').read_text(encoding='utf-8'))

    servo = Servo(hw['servo'])
    servo.start()
    servo.release()
    camera = Camera(hw['camera'])
    camera.start()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(camera, servo))
    server.daemon_threads = True

    def shutdown(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print(f'联调页面：http://{args.host}:{args.port}/  舵机：{servo.driver}，{servo.note}；默认不输出脉冲', flush=True)
    try:
        server.serve_forever()
    finally:
        servo.release()
        servo.stop()
        camera.stop()
        print('已停止：舵机信号置低，摄像头已释放', flush=True)


if __name__ == '__main__':
    main()
