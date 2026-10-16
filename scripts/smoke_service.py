"""运行中服务的冒烟检查：读取 /api/health，连上 WebSocket 收几秒事件和预览帧。不发控制指令。

用法：服务运行时执行 .venv/bin/python scripts/smoke_service.py [--seconds 5] [--port 8322]
"""
import argparse
import base64
import json
import os
import socket
import struct
import sys
import time
from collections import Counter
from urllib.request import urlopen


def ws_connect(host, port):
    sock = socket.create_connection((host, port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    sock.sendall((f'GET /api/ws HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                  f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
    head = b''
    while b'\r\n\r\n' not in head:
        chunk = sock.recv(1024)
        if not chunk:
            raise ConnectionError('握手时连接被关闭')
        head += chunk
    status = head.split(b'\r\n', 1)[0].decode()
    if ' 101 ' not in status:
        raise ConnectionError(f'握手失败：{status}')
    return sock, head.split(b'\r\n\r\n', 1)[1]


def read_exact(sock, buf, n):
    while len(buf) < n:
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError('连接已关闭')
        buf += chunk
    return buf[:n], buf[n:]


def read_message(sock, buf):
    head, buf = read_exact(sock, buf, 2)
    opcode, size = head[0] & 0x0F, head[1] & 0x7F
    if size == 126:
        ext, buf = read_exact(sock, buf, 2)
        size = struct.unpack('!H', ext)[0]
    elif size == 127:
        ext, buf = read_exact(sock, buf, 8)
        size = struct.unpack('!Q', ext)[0]
    payload, buf = read_exact(sock, buf, size)
    return opcode, payload, buf


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8322)
    parser.add_argument('--seconds', type=float, default=5)
    args = parser.parse_args()

    health = json.load(urlopen(f'http://{args.host}:{args.port}/api/health', timeout=5))
    print('health:', json.dumps({k: health[k] for k in ('ok', 'version', 'phase', 'clients')}, ensure_ascii=False))
    for part in ('camera', 'model', 'servo'):
        item = health[part]
        print(f'  {part}: ok={item["ok"]} error={item.get("error")}'
              + (f' driver={item["driver"]} note={item["note"]}' if part == 'servo' else ''))

    sock, buf = ws_connect(args.host, args.port)
    events, frames, sizes, people, last_metrics, first = Counter(), 0, [], Counter(), None, {}
    motions = []
    end = time.monotonic() + args.seconds
    while time.monotonic() < end:
        opcode, payload, buf = read_message(sock, buf)
        if opcode == 0x1:
            event = json.loads(payload)
            events[event['type']] += 1
            first.setdefault(event['type'], event)
            if event['type'] == 'metrics':
                last_metrics = event
        elif opcode == 0x2:
            n = struct.unpack('!I', payload[:4])[0]
            meta = json.loads(payload[4:4 + n])
            jpeg = payload[4 + n:]
            if not jpeg.startswith(b'\xff\xd8'):
                print('预览帧不是 JPEG', file=sys.stderr)
                sys.exit(1)
            frames += 1
            sizes.append(len(jpeg))
            people[meta['count']] += 1
            if meta.get('motion') is not None:
                motions.append(meta['motion'])
    sock.close()

    hello = first.get('hello', {})
    print(f'events: {dict(events)}')
    print(f'hello: protocol={hello.get("protocol")} poses={[p["id"] for p in hello.get("config", {}).get("poses", [])]}')
    state = first.get('state', {})
    print(f'state: phase={state.get("phase")} people={state.get("people")} can_start={state.get("can_start")}')
    print(f'frames: {frames} 帧 / {args.seconds:g}s = {frames / args.seconds:.1f}fps，'
          f'JPEG 平均 {sum(sizes) / max(1, len(sizes)) / 1024:.1f}KB，人数分布 {dict(people)}')
    if last_metrics:
        print(f'metrics: camera {last_metrics["camera_fps"]}fps，vision {last_metrics["vision_fps"]}fps，'
              f'infer {last_metrics["infer_ms"]}ms')
    if motions:
        motions.sort()
        print(f'画面晃动：中位 {motions[len(motions) // 2]}，p95 {motions[int(len(motions) * 0.95) - 1]}，'
              f'最大 {motions[-1]}（静止阈值默认 2.5）')
    ok = health['ok'] and frames > 0 and events['hello'] == 1 and events['state'] >= 1
    print('结果：通过' if ok else '结果：未通过')
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
