"""录制：每帧写一行 JSON，供 tools/replay.py 回放调参。文件只存在 K3 本地。"""
import json
import threading
import time
from pathlib import Path


class Recorder:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.lock = threading.Lock()
        self.file = None
        self.path = None
        self.frames = 0
        self.started = None

    @property
    def active(self):
        return self.file is not None

    def start(self, meta):
        with self.lock:
            if self.file is not None:
                return self.path.name
            self.folder.mkdir(parents=True, exist_ok=True)
            self.path = self.folder / time.strftime('rec-%Y%m%d-%H%M%S.jsonl')
            self.file = self.path.open('w', encoding='utf-8')
            self.frames, self.started = 0, time.monotonic()
            self._write({'type': 'meta', 'created': time.strftime('%Y-%m-%dT%H:%M:%S'), **meta})
            return self.path.name

    def write_frame(self, frame, phase, sub, people, info, motion=None):
        with self.lock:
            if self.file is None:
                return
            self._write({'type': 'frame', 'seq': frame.seq, 't': round(frame.t, 4), 'phase': phase, 'sub': sub,
                         'people': [p.to_json() for p in people], 'player': info['player_index'],
                         'value': info['value'], 'raw': info.get('raw'), 'pose_ok': info['pose_ok'],
                         'motion': None if motion is None else round(motion, 2)})
            self.frames += 1

    def _write(self, obj):
        self.file.write(json.dumps(obj, ensure_ascii=False, separators=(',', ':')) + '\n')

    def stop(self):
        with self.lock:
            if self.file is None:
                return None
            self.file.close()
            name, self.file = self.path.name, None
            return name

    def status(self):
        return {'active': self.active, 'file': self.path.name if self.path else None, 'frames': self.frames,
                'seconds': round(time.monotonic() - self.started, 1) if self.active else None}
