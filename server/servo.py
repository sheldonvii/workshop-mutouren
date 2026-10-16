"""舵机驱动：GPIO 软件脉冲或模拟舵机，对上层接口相同。

GPIO 软件脉冲在独立进程里生成，绑定 CPU 并启用实时调度；需要 /dev/gpiochip* 的读写权限和
实时调度限额，见 scripts/setup_k3_gpio.sh。申请 GPIO 线时 pinctrl 驱动会把引脚切换为 GPIO 功能，
不需要改设备树。GPIO 不可用时自动退回模拟舵机，状态里标明原因。
舵机没有位置反馈，是否到位按配置的转速估算。
"""
import ctypes
import fcntl
import gc
import glob
import multiprocessing as mp
import os
import queue
import threading
import time

PULSE_MIN_US, PULSE_MAX_US = 500, 2500
PERIOD_NS = 20_000_000

_FLAG_USED = 1 << 0
_FLAG_OUTPUT = 1 << 3


class ChipInfo(ctypes.Structure):
    _fields_ = [('name', ctypes.c_char * 32), ('label', ctypes.c_char * 32), ('lines', ctypes.c_uint32)]


class LineAttribute(ctypes.Structure):
    _fields_ = [('id', ctypes.c_uint32), ('padding', ctypes.c_uint32), ('value', ctypes.c_uint64)]


class LineConfigAttribute(ctypes.Structure):
    _fields_ = [('attr', LineAttribute), ('mask', ctypes.c_uint64)]


class LineConfig(ctypes.Structure):
    _fields_ = [('flags', ctypes.c_uint64), ('num_attrs', ctypes.c_uint32), ('padding', ctypes.c_uint32 * 5),
                ('attrs', LineConfigAttribute * 10)]


class LineRequest(ctypes.Structure):
    _fields_ = [('offsets', ctypes.c_uint32 * 64), ('consumer', ctypes.c_char * 32), ('config', LineConfig),
                ('num_lines', ctypes.c_uint32), ('event_buffer_size', ctypes.c_uint32),
                ('padding', ctypes.c_uint32 * 5), ('fd', ctypes.c_int32)]


class LineInfo(ctypes.Structure):
    _fields_ = [('name', ctypes.c_char * 32), ('consumer', ctypes.c_char * 32), ('offset', ctypes.c_uint32),
                ('num_attrs', ctypes.c_uint32), ('flags', ctypes.c_uint64), ('attrs', LineAttribute * 10),
                ('padding', ctypes.c_uint32 * 4)]


class LineValues(ctypes.Structure):
    _fields_ = [('bits', ctypes.c_uint64), ('mask', ctypes.c_uint64)]


def _ioc(direction, nr, struct):
    return (direction << 30) | (ctypes.sizeof(struct) << 16) | (0xB4 << 8) | nr


GET_CHIPINFO = _ioc(2, 0x01, ChipInfo)
GET_LINEINFO = _ioc(3, 0x05, LineInfo)
GET_LINE = _ioc(3, 0x07, LineRequest)
SET_VALUES = _ioc(3, 0x0F, LineValues)

assert ctypes.sizeof(LineRequest) == 592 and ctypes.sizeof(LineInfo) == 256, 'GPIO uAPI 结构体大小与内核不符'

HIGH, LOW = LineValues(1, 1), LineValues(0, 1)


def find_chip(label):
    """按标签找到 gpiochip，返回 (路径, fd)。"""
    for path in sorted(glob.glob('/dev/gpiochip*')):
        try:
            fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        except PermissionError:
            raise PermissionError(f'没有 {path} 的读写权限，先执行 sudo scripts/setup_k3_gpio.sh') from None
        info = ChipInfo()
        fcntl.ioctl(fd, GET_CHIPINFO, info)
        if info.label.decode() == label:
            return path, fd
        os.close(fd)
    raise FileNotFoundError(f'没有找到标签为 {label} 的 gpiochip')


class GpioLine:
    """单根 GPIO 输出线。"""

    def __init__(self, chip_label, line, consumer=b'mutouren-servo'):
        self.chip_path, self.chip_fd = find_chip(chip_label)
        try:
            info = LineInfo(offset=line)
            fcntl.ioctl(self.chip_fd, GET_LINEINFO, info)
            if info.flags & _FLAG_USED:
                raise RuntimeError(f'{chip_label} 第 {line} 根线已被占用：{info.consumer.decode()!r}')
            req = LineRequest(num_lines=1, consumer=consumer)
            req.offsets[0] = line
            req.config.flags = _FLAG_OUTPUT
            fcntl.ioctl(self.chip_fd, GET_LINE, req)
        except BaseException:
            os.close(self.chip_fd)
            raise
        self.fd = req.fd

    def set(self, value):
        fcntl.ioctl(self.fd, SET_VALUES, HIGH if value else LOW)

    def close(self):
        try:
            self.set(0)
        finally:
            os.close(self.fd)
            os.close(self.chip_fd)


def realtime(cpu):
    """绑定 CPU 并切换到 SCHED_FIFO，返回说明文字。

    直接绑定 cpu，不受继承来的 CPU 掩码限制（服务主进程会把这个核让出来）；绑定失败时退回可用集合的最后一个。
    """
    notes = []
    try:
        os.sched_setaffinity(0, {cpu})
        notes.append(f'绑定 CPU{cpu}')
    except OSError:
        try:
            target = max(os.sched_getaffinity(0))
            os.sched_setaffinity(0, {target})
            notes.append(f'CPU{cpu} 不可用，绑定 CPU{target}')
        except OSError as error:
            notes.append(f'未绑定 CPU（{error}）')
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(80))
        notes.append('SCHED_FIFO 80')
    except OSError as error:
        notes.append(f'未启用实时调度（{error}）')
    return '，'.join(notes)


def pulse_once(line, width_us):
    """输出一个脉冲，返回实测脉宽（μs）。"""
    t0 = time.perf_counter_ns()
    line.set(1)
    stop = t0 + width_us * 1000
    while time.perf_counter_ns() < stop:
        pass
    line.set(0)
    return (time.perf_counter_ns() - t0) / 1000


def wait_next_period(frame):
    """睡到距下一个周期起点约 1ms，再忙等对齐。返回下一个周期起点。"""
    frame += PERIOD_NS
    remain = frame - time.perf_counter_ns()
    if remain > 1_500_000:
        time.sleep((remain - 1_000_000) / 1e9)
    while time.perf_counter_ns() < frame:
        pass
    return frame


def _pulse_process(width_us, alive, stats, messages, chip_label, line_offset, cpu):
    """独立进程：width_us 为 0 时不输出，否则按 20ms 周期持续输出该脉宽。

    每输出 25 个脉冲（约 0.5 秒）把时序统计写入 stats：脉宽误差 p95、脉宽误差最大值、周期起点最大延迟，单位 μs。
    关闭垃圾回收，避免回收停顿打乱脉宽。
    """
    gc.disable()
    try:
        line = GpioLine(chip_label, line_offset)
    except Exception as error:
        messages.put(('error', str(error)))
        return
    messages.put(('ready', f'{line.chip_path} 第 {line_offset} 根线，{realtime(cpu)}'))
    errors, lates = [], []
    frame = time.perf_counter_ns()
    try:
        while alive.value:
            width = width_us.value
            if width:
                lates.append((time.perf_counter_ns() - frame) / 1000)
                errors.append(abs(pulse_once(line, width) - width))
                if len(errors) >= 25:
                    errors.sort()
                    stats[0], stats[1], stats[2] = errors[int(len(errors) * 0.95) - 1], errors[-1], max(lates)
                    errors, lates = [], []
            frame = wait_next_period(frame)
    finally:
        line.close()


class Servo:
    """舵机：按名称转到配置的档位，或在调试时直接指定脉宽。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.positions = {name: int(us) for name, us in cfg['positions'].items()}
        for name, us in self.positions.items():
            check_width(us, name)
        self.speed = float(cfg['speed_us_per_s'])
        self.settle_s = float(cfg['settle_s'])
        self.hold = cfg.get('hold', 'pulse')
        self.driver = 'sim'
        self.note = ''
        self.error = None
        self._lock = threading.Lock()
        self._proc = None
        self._width = None
        self._alive = None
        self._stats = None
        self._target = None
        self._us = None
        self._arrive_at = 0.0
        self._settled_at = 0.0

    def start(self):
        if self.cfg.get('driver') == 'gpio':
            self._start_gpio()
        else:
            self.note = '配置为模拟舵机'
        self.goto('standby')

    def _start_gpio(self):
        ctx = mp.get_context('spawn')
        self._width, self._alive = ctx.Value('i', 0), ctx.Value('b', 1)
        self._stats = ctx.Array('d', 3)
        messages = ctx.Queue()
        self._proc = ctx.Process(target=_pulse_process, daemon=True, name='servo-pulse',
                                 args=(self._width, self._alive, self._stats, messages, self.cfg['chip_label'],
                                       int(self.cfg['line']), int(self.cfg.get('cpu', 7))))
        self._proc.start()
        try:
            kind, text = messages.get(timeout=5)
        except queue.Empty:
            kind, text = 'error', '脉冲进程 5 秒内没有就绪'
        if kind == 'ready':
            self.driver, self.note = 'gpio', text
        else:
            self._alive.value = 0
            self._proc.join(timeout=2)
            self._proc = None
            self._width = self._stats = None
            self.driver, self.note = 'sim', f'GPIO 不可用，已改用模拟舵机：{text}'

    def update(self, positions=None, speed_us_per_s=None, settle_s=None):
        with self._lock:
            if positions:
                for name, us in positions.items():
                    check_width(int(us), name)
                self.positions.update({name: int(us) for name, us in positions.items()})
            if speed_us_per_s is not None:
                self.speed = max(100.0, float(speed_us_per_s))
            if settle_s is not None:
                self.settle_s = max(0.0, float(settle_s))

    def goto(self, name):
        """转到档位，返回预计停稳的时刻（time.monotonic）。"""
        return self._move(name, self.positions[name])

    def set_us(self, us):
        check_width(us, 'manual')
        return self._move('manual', us)

    def release(self):
        """停发脉冲，舵机靠齿轮阻力停在原位；下次 goto 或 set_us 时恢复输出。"""
        if self._width is not None:
            self._width.value = 0

    def _move(self, name, us):
        with self._lock:
            now = time.monotonic()
            travel = (PULSE_MAX_US - PULSE_MIN_US) if self._us is None else abs(us - self._us)
            self._us, self._target = us, name
            self._arrive_at = now + travel / self.speed
            self._settled_at = self._arrive_at + self.settle_s
            if self._width is not None:
                self._width.value = us
            return self._settled_at

    @property
    def settled_at(self):
        return self._settled_at

    def tick(self):
        """定时调用：检查脉冲进程是否还活着。保持方式为 release 时，由游戏在画面停稳、开始判定时调用 release()；
        不按估算时间自动停发，因为估算偏快时会让舵机停在半路。"""
        if self._proc is not None and not self._proc.is_alive() and self.error is None:
            self.error = '脉冲进程已退出'

    def status(self):
        now = time.monotonic()
        return {
            'driver': self.driver,
            'ok': self.error is None,
            'error': self.error,
            'note': self.note,
            'target': self._target,
            'us': self._us,
            'output': bool(self._width is not None and self._width.value),
            'moving': now < self._arrive_at,
            'settled': now >= self._settled_at,
            'settled_in_ms': max(0, round((self._settled_at - now) * 1000)),
            'positions': dict(self.positions),
            'speed_us_per_s': self.speed,
            'settle_s': self.settle_s,
            'hold': self.hold,
            'pulse_error_p95_us': None if self._stats is None else round(self._stats[0], 1),
            'pulse_error_max_us': None if self._stats is None else round(self._stats[1], 1),
            'period_late_max_us': None if self._stats is None else round(self._stats[2], 1),
        }

    def stop(self):
        """回到待机档位后停止输出并结束脉冲进程。"""
        if self._proc is not None and self._proc.is_alive():
            settled = self.goto('standby')
            time.sleep(min(2.0, max(0.0, settled - time.monotonic())))
        if self._width is not None:
            self._width.value = 0
            self._alive.value = 0
        if self._proc is not None:
            self._proc.join(timeout=2)
            self._proc = None


def check_width(us, name=''):
    if not PULSE_MIN_US <= us <= PULSE_MAX_US:
        raise ValueError(f'{name} 脉宽 {us}μs 超出 {PULSE_MIN_US}～{PULSE_MAX_US}μs')
