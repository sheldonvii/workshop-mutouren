"""摄像头采集：独立线程连续读帧，只保留最新一帧；断开后自动重连。"""
import subprocess
import threading
import time

import cv2


class Frame:
    __slots__ = ('seq', 't', 'image')

    def __init__(self, seq, t, image):
        self.seq, self.t, self.image = seq, t, image


class Camera:
    def __init__(self, cfg):
        self.cfg = cfg
        self.cond = threading.Condition()
        self.frame = None
        self.fps = 0.0
        self.size = (0, 0)
        self.error = '尚未打开'
        self.controls_error = None
        self.running = False
        self._cap = None
        self._seq = 0

    def start(self):
        self.running = True
        threading.Thread(target=self._loop, daemon=True, name='camera').start()

    def _apply_controls(self):
        """用 v4l2-ctl 下发 controls 里的控制项。

        OpenCV 的 CAP_PROP_AUTO_EXPOSURE 语义各家不一，UVC 摄像头又常以手动曝光开机，
        所以直接在设备节点上设标准控制项。失败只记日志，不影响采集。
        """
        controls = self.cfg.get('controls') or {}
        if not controls:
            return
        device = f'/dev/video{self.cfg["device"]}'
        args = []
        for name, value in controls.items():
            args += ['-c', f'{name}={value}']
        result = subprocess.run(['v4l2-ctl', '-d', device, *args], capture_output=True, text=True)
        if result.returncode != 0:
            text = (result.stderr or result.stdout).strip().splitlines()
            self.controls_error = text[0] if text else f'v4l2-ctl 退出码 {result.returncode}'
        else:
            self.controls_error = None

    def _open(self):
        cfg = self.cfg
        cap = cv2.VideoCapture(int(cfg['device']), cv2.CAP_V4L2)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*cfg.get('fourcc', 'MJPG')))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(cfg['width']))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(cfg['height']))
        cap.set(cv2.CAP_PROP_FPS, int(cfg.get('fps', 30)))
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not cap.isOpened():
            cap.release()
            return None
        self._apply_controls()
        return cap

    def _loop(self):
        failures, count, mark = 0, 0, time.monotonic()
        while self.running:
            if self._cap is None:
                self._cap = self._open()
                if self._cap is None:
                    self._set_error(f'无法打开 /dev/video{self.cfg["device"]}')
                    time.sleep(2)
                    continue
                failures = 0
            ok, image = self._cap.read()
            now = time.monotonic()
            if not ok:
                failures += 1
                if failures > 20:
                    self._cap.release()
                    self._cap = None
                    self._set_error('连续读帧失败，摄像头可能已断开，正在重连')
                time.sleep(0.05)
                continue
            failures = 0
            count += 1
            with self.cond:
                self._seq += 1
                self.frame = Frame(self._seq, now, image)
                self.size = (image.shape[1], image.shape[0])
                self.error = None
                if now - mark >= 1:
                    self.fps, count, mark = count / (now - mark), 0, now
                self.cond.notify_all()
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def _set_error(self, message):
        with self.cond:
            self.error, self.fps = message, 0.0
            self.cond.notify_all()

    def wait(self, last_seq, timeout=1.0):
        """等待比 last_seq 新的帧，超时返回 None。"""
        with self.cond:
            self.cond.wait_for(lambda: (self.frame is not None and self.frame.seq != last_seq) or not self.running,
                               timeout)
            frame = self.frame
        return frame if frame is not None and frame.seq != last_seq else None

    def status(self):
        return {'ok': self.error is None, 'error': self.error, 'fps': round(self.fps, 1),
                'size': list(self.size), 'device': f'/dev/video{self.cfg["device"]}',
                'controls_error': self.controls_error}

    def stop(self):
        self.running = False
        with self.cond:
            self.cond.notify_all()
