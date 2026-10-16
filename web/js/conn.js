// 与服务端的连接：WebSocket 只收事件和预览帧，操作走 HTTP 接口。断线后自动重连。
export const PROTOCOL = 1;

export function connect({ onEvent, onFrame, onStatus }) {
  let socket = null;
  let delay = 500;
  let closed = false;

  function open() {
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    socket = new WebSocket(`${scheme}://${location.host}/api/ws`);
    socket.binaryType = 'arraybuffer';
    socket.onopen = () => {
      delay = 500;
      onStatus(true);
    };
    socket.onmessage = (message) => {
      if (typeof message.data === 'string') {
        const event = JSON.parse(message.data);
        if (event.v !== PROTOCOL) console.warn('协议版本不一致', event.v, PROTOCOL);
        onEvent(event);
        return;
      }
      const view = new DataView(message.data);
      const size = view.getUint32(0);
      const meta = JSON.parse(new TextDecoder().decode(new Uint8Array(message.data, 4, size)));
      onFrame(meta, new Blob([new Uint8Array(message.data, 4 + size)], { type: 'image/jpeg' }));
    };
    socket.onclose = () => {
      onStatus(false);
      if (!closed) setTimeout(open, delay);
      delay = Math.min(delay * 2, 5000);
    };
  }

  open();
  return { close() { closed = true; socket?.close(); } };
}

/** 调用接口，失败时抛出带服务端说明的错误。 */
export async function api(path, body, method = 'POST') {
  const response = await fetch(path, {
    method,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`);
  return data;
}
