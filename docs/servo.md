# 舵机驱动与 GPIO 软件脉冲

本文档介绍云台驱动架构，包括采用 GPIO 软件脉冲的工程选型、脉冲生成逻辑、独立进程与实时调度设计，以及云台标定与测试方法。接线与电气规范见 [硬件接口与电气规格](hardware.md)。

## 驱动选型背景

SpacemiT K3 芯片内置硬件 PWM 控制器，其引出脚 `RX_PWM1` 与 `RX_PWM2` 对应 R 域的 `rpwm1` / `rpwm2`。但官方发布的 `k3-pico-itx.dtb` 设备树中未激活对应节点，且系统内核不支持设备树插件动态加载。

为了在不重新编译与替换内核设备树的前提下驱动云台，项目采用 Linux GPIO 字符设备 uAPI v2 生成软件脉冲。内核 pinctrl 驱动在用户空间申请引脚时会自动将 `RX_PWM2` 切换为 GPIO 功能。标准 RC 舵机工作于 20ms 周期、500～2500μs 脉宽，内部具备滤波死区，对微秒级脉宽抖动容忍度较高，满足软件脉冲驱动条件。

## 脉冲生成机制

[server/servo.py](../server/servo.py) 直接通过 `ioctl` 与 Linux GPIO 字符设备 uAPI v2 通信，无需额外安装 `libgpiod` 依赖库。

| 接口调用 | ioctl 操作码 | 功能定义 |
| --- | --- | --- |
| `GET_CHIPINFO` | 0x8044b401 | 根据 `d4019000.gpio-bank0` 标签匹配控制器，避免硬编码节点号 |
| `GET_LINEINFO` | 0xc110b405 | 读取引脚状态与占用信息（校验 `FLAG_USED`） |
| `GET_LINE` | 0xc250b407 | 以 `FLAG_OUTPUT` 模式申请引脚句柄，绑定标识 `mutouren-servo` |
| `SET_VALUES` | 0xc040b40e | 执行电平切换 |

底层数据结构使用 `ctypes` 严格映射内核结构体，模块加载时会对 `LineRequest`（592 字节）与 `LineInfo`（256 字节）进行内存尺寸断言。

单次脉冲输出采用“拉高电平 → 纳秒级高精度忙等 → 拉低电平”逻辑：

```python
def pulse_once(line, width_us):
    t0 = time.perf_counter_ns()
    line.set(1)
    stop = t0 + width_us * 1000
    while time.perf_counter_ns() < stop:
        pass
    line.set(0)
    return (time.perf_counter_ns() - t0) / 1000
```

脉宽阶段采用全忙等以规避 `time.sleep` 的毫秒级调度延迟；20ms 周期的空闲间隔则采用“休眠至下一周期前 1ms + 剩余 1ms 忙等补齐”的混合策略，兼顾时序精度与 CPU 负载平衡。

## 独立进程与实时调度

由于 Python 全局解释器锁（GIL）与主进程 NPU 姿态推理的并发调度开销，脉冲生成置于独立子进程（`multiprocessing` spawn 模式）中执行：

* **核心绑定**：通过 `os.sched_setaffinity` 强制将脉冲子进程绑定至独立的 CPU7。主服务进程在加载视觉模型前主动将自身及线程限制在 CPU 0～6，物理隔绝推理对脉冲核心的调度干扰。
* **实时优先级**：子进程启用 `SCHED_FIFO` 实时调度策略（优先级 80），确保脉冲循环不被普通系统进程抢占。
* **GC 抑制**：循环运行期间调用 `gc.disable()` 屏蔽垃圾回收，杜绝 GC 偶发卡顿引起的脉宽异常。

子进程每发送 25 个脉冲周期统计一次实际时序，通过共享内存向上汇报指标。系统空载时脉宽误差维持在 ±10μs 内；NPU 满载推理时，实测 95 分位误差约为 28μs，处于舵机物理死区安全范围内。

## 脉冲停发机制

云台转动到位且画面稳定后，子进程将输出脉宽置零，主动停止发送控制脉冲（配置项 `servo.hold: "release"`）。

持续发送 PWM 信号时，高频脉宽微扰会导致舵机齿轮产生轻微机械颤动，使得固定在云台上的摄像头发生像素级位移。该位移会被人体关键点追踪算法误判为玩家动作。停发脉冲可彻底消除此抖动。

停稳条件通过前后端协同判定：当云台旋转达到预估时长后，算法检测连续 4 帧（`still_frames`）的画面差分值低于阈值 2.5（`still_threshold`），或达到最大等待超时（`settle_timeout_s: 3.0`），即视为停稳并切入红灯判定。

## 转动耗时估算与姿态同步

舵机硬件无实时位置反馈，系统通过配置的角速度参数估算转动耗时：

$$
t_{	ext{move}} = \frac{|	ext{Target Pulse} - 	ext{Current Pulse}|}{	ext{speed\_us\_per\_s}}$$

配置参数保存在 [configs/hardware.json](../configs/hardware.json) 中：
* `speed_us_per_s`：脉宽变化速率，默认 1500 μs/s。
* `settle_s`：预估到位后的额外停稳缓冲时间。

前端界面的“木头人头部翻转”动画时长同样依据 $t_{	ext{move}}$ 动态计算，使虚拟画面动效与物理云台旋转保持同步。

## 档位标定流程

新装配或更换云台后需校准脉宽参数，通过调试界面完成：

1. 启动服务并访问 `http://<K3 地址>:8322/debug`。
2. 拖动舵机滑块将摄像头调整至正对玩家区域，点击“当前脉宽记为面对”（写入 `face`）。
3. 将摄像头旋转至完全背对玩家区域，点击“当前脉宽记为背对”（写入 `back`）。
4. 记录两档位之间的实际机械旋转时间，折算为 `speed_us_per_s` 与 `settle_s`，点击“应用并保存”持久化至配置文件。

## 单独测试工具

* **脉冲与接线校验**：运行 `python tools/servo_gpio_test.py`。工具支持独立驱动指定脉宽、执行档位扫描，或输出交替跳变信号配合万用表定位信号针脚。
* **摄像头与云台联调**：运行 `python tools/camera_servo_test.py` 启动轻量级交互页，边观察实时取景画面边确认旋转死角与目标档位。
* **规则参数离线回放**：运行 `python tools/replay.py --threshold 0.1 --consecutive 3 <录制文件>.jsonl`，利用真实游戏录制数据批量验证不同算法阈值下的误报率与检出率。
