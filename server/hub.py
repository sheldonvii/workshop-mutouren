"""WebSocket 推送，实现 RFC 6455 的最小子集。服务端只推送，页面的操作走 HTTP 接口。

文本消息是 JSON 事件；二进制消息是预览帧：4 字节大端长度 + vision 事件 JSON + JPEG，
骨架和画面出自同一帧，页面不用再配对。每个连接只保留最新一帧，事件按顺序发送。
"""
import base64
import hashlib
import json
import struct
import threading
from collections import deque

PROTOCOL = 1
_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


def _header(opcode, size):
    if size < 126:
        return struct.pack('!BB', 0x80 | opcode, size)
    if size < 65536:
        return struct.pack('!BBH', 0x80 | opcode, 126, size)
    return struct.pack('!BBQ', 0x80 | opcode, 127, size)


class _Client:
    def __init__(self, rfile, wfile):
        self.rfile, self.wfile = rfile, wfile
        self.cond = threading.Condition()
        self.events = deque(maxlen=2000)
        self.frame = None
        self.alive = True
        self.write_lock = threading.Lock()

    def push_event(self, text):
        with self.cond:
            self.events.append(text)
            self.cond.notify()

    def push_frame(self, data):
        with self.cond:
            self.frame = data
            self.cond.notify()

    def close(self):
        with self.cond:
            self.alive = False
            self.cond.notify()

    def _send(self, opcode, payload):
        with self.write_lock:
            self.wfile.write(_header(opcode, len(payload)) + payload)
            self.wfile.flush()

    def sender(self):
        try:
            while True:
                with self.cond:
                    self.cond.wait_for(lambda: self.events or self.frame is not None or not self.alive, 5)
                    if not self.alive:
                        return
                    events, self.events = list(self.events), deque(maxlen=2000)
                    frame, self.frame = self.frame, None
                for text in events:
                    self._send(0x1, text)
                if frame is not None:
                    self._send(0x2, frame)
        except (OSError, ValueError):
            pass
        finally:
            self.close()

    def _read(self, n):
        data = self.rfile.read(n)
        if len(data) < n:
            raise EOFError
        return data

    def reader(self):
        try:
            while self.alive:
                b1, b2 = self._read(2)
                opcode, size = b1 & 0x0F, b2 & 0x7F
                if size == 126:
                    size = struct.unpack('!H', self._read(2))[0]
                elif size == 127:
                    size = struct.unpack('!Q', self._read(8))[0]
                if size > 1 << 20:
                    return
                mask = self._read(4) if b2 & 0x80 else None
                payload = self._read(size)
                if mask:
                    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                if opcode == 0x8:
                    self._send(0x8, payload[:2])
                    return
                if opcode == 0x9:
                    self._send(0xA, payload)
        except (OSError, EOFError, ValueError):
            pass
        finally:
            self.close()


class Hub:
    def __init__(self, on_connect=None):
        self.on_connect = on_connect
        self.clients = set()
        self.lock = threading.Lock()

    @staticmethod
    def _encode(obj):
        return json.dumps({'v': PROTOCOL, **obj}, ensure_ascii=False, separators=(',', ':'))

    def serve(self, handler):
        """在 HTTP 处理线程里完成握手并阻塞到连接关闭。"""
        key = handler.headers.get('Sec-WebSocket-Key')
        if not key or 'websocket' not in handler.headers.get('Upgrade', '').lower():
            handler.send_error(400, 'expected websocket upgrade')
            return
        accept = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
        handler.send_response(101, 'Switching Protocols')
        handler.send_header('Upgrade', 'websocket')
        handler.send_header('Connection', 'Upgrade')
        handler.send_header('Sec-WebSocket-Accept', accept)
        handler.end_headers()
        handler.close_connection = True
        client = _Client(handler.rfile, handler.wfile)
        for event in (self.on_connect() if self.on_connect else []):
            client.push_event(self._encode(event).encode())
        with self.lock:
            self.clients.add(client)
        threading.Thread(target=client.sender, daemon=True, name='ws-send').start()
        try:
            client.reader()
        finally:
            with self.lock:
                self.clients.discard(client)

    def event(self, obj):
        data = self._encode(obj).encode()
        with self.lock:
            clients = list(self.clients)
        for client in clients:
            client.push_event(data)

    def frame(self, meta, jpeg):
        head = self._encode(meta).encode()
        data = struct.pack('!I', len(head)) + head + jpeg
        with self.lock:
            clients = list(self.clients)
        for client in clients:
            client.push_frame(data)

    def count(self):
        with self.lock:
            return len(self.clients)

    def close_all(self):
        with self.lock:
            clients = list(self.clients)
        for client in clients:
            client.close()
