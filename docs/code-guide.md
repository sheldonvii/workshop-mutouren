# 代码导读

本文面向第一次接手本 Demo 的开发者。它解释代码之间的调用关系和修改入口，不重复安装步骤、硬件接线、完整接口字段与脉冲时序细节。相关内容分别见 [README.md](../README.md)、[development.md](development.md)、[hardware.md](hardware.md) 和 [servo.md](servo.md)。

## 先看哪里

| 目的 | 先读的文件 | 需要理解的内容 |
| --- | --- | --- |
| 启动服务 | `run.sh`、`server/app.py` | 服务入口、CPU 亲和性设置、HTTP 路由、线程划分与舵机子进程 |
| 修改识别 | `server/vision.py`、`configs/yolov8*-pose.yaml` | 模型封装、关键点过滤、尺度归一化 |
| 修改判定 | `server/vision.py` 的 `MotionJudge`、`configs/game.json` | 基准建立、逐关节抖动容忍、连续帧确认 |
| 修改姿势规则 | `server/poses.py` | 五个姿势的几何条件与「看不清」的三态返回 |
| 修改玩法 | `server/game.py` | 状态机、阶段切换、人数防抖、停稳判定 |
| 修改舵机 | `server/servo.py`、`configs/hardware.json` | GPIO uAPI 脉冲、独立进程、实时调度、停发策略 |
| 修改页面 | `web/js/player.js`、`web/js/debug.js`、`web/js/draw.js` | 状态渲染、参数表单、骨架与曲线绘制 |
| 验证改动 | `tests/test_logic.py`、`tests/browser_check.mjs` | 离线逻辑测试与页面检查 |

## 一帧画面如何变成一次判定

```text
camera.py 采集线程（只留最新一帧）
  -> app.py _pipeline 取出新帧
  -> vision.frame_motion 算画面晃动（96×54 灰度帧差）
  -> vision.PoseModel.infer 推理，得人体框与 17 个关键点
  -> vision.PlayerTracker 找回倒计时阶段锁定的那位玩家
  -> game.on_frame
       ├─ 绿灯：poses.evaluate 判姿势对不对
       └─ 红灯：judge.measure 逐关节比较，超出容忍的部分取最大值
  -> hub.frame 推送二进制帧（4 字节长度 + JSON + JPEG）
  -> hub.event 推送状态与判定事件
  -> recorder.write_frame 落盘，供回放调参
  -> snapshots.save 在淘汰时存下触发判定的那一帧
```

`server/app.py` 只做编排。它设置 CPU 亲和性、拉起线程、分发 HTTP 路由，把推理结果交给 `Game`，再把结果推给前端。这里适合改路由、加接口、调整上报字段，不适合直接写判定算法。

进程内的角色是这样分的，改并发相关的东西前先认清自己动的是哪一个：

| 角色 | 位置 | 职责 |
| --- | --- | --- |
| 主线程 | `app.py` | HTTP 服务与信号处理 |
| `camera` 线程 | `camera.py` | 连续读帧，只保留最新一帧 |
| `pipeline` 线程 | `app.py` | 取帧、算晃动、推理、交给状态机、推送预览 |
| `ticker` 线程 | `app.py` | 每 20ms 推进状态机，每秒推一次指标 |
| `ws-send` 线程 | `hub.py` | 每个 WebSocket 连接一条，只负责发送 |
| 脉冲子进程 | `servo.py` | 独立进程，`SCHED_FIFO` 绑 CPU7 生成脉冲 |

推理只在 `pipeline` 这一个线程里跑，所以模型不需要考虑并发；舵机状态由 `servo.py` 内部的锁保护。加新线程时注意 `_keep_off_servo_cpu()` 是在 `start()` 最前面调用的——它靠**继承**生效，之后创建的线程才会拿到受限的 CPU 掩码。

`server/vision.py` 是算法主体，三块内容彼此独立：`PoseModel` 只管模型封装与过滤；`PlayerTracker` 只管找回玩家；`MotionJudge` 只管静止判定。改动其中一块不需要动另外两块。`body_scale` 提供统一的尺度基准（躯干长度），所有位移阈值都以它为单位，这是这套判定能在远近不同的位置给出相同含义的关键。

`server/game.py` 是状态机。它按阶段推进：`idle → countdown → green → turning → red → safe → …`，红灯再分 `grace`（宽限）与 `watch`（盯人）两个子阶段。停稳判定与停发脉冲都在这里（`_count_still`、`_settled`、`_release_if_settled`），因为这两件事的触发条件都来自状态而不是来自舵机。

`server/servo.py` 对上层是一台普通执行器，只有 `start` / `goto` / `set_us` / `release` / `status` / `stop` 几个方法；GPIO 与模拟两种实现在内部切换，申请不到 GPIO 时会自动退回模拟并在状态里写明原因，游戏仍能跑。脉冲生成在 `multiprocessing` 的子进程里，模块导入时会断言 ctypes 结构体大小与内核定义一致，避免结构错位。

## 前端与后端的关系

后端是唯一状态源。`server/hub.py` 用两条通道推送：文本帧是 JSON 事件（`state`、`verdict`、`servo`、`metrics`、`audio`、`error`），二进制帧是预览画面外加同帧的判定数据。骨架和画面出自同一帧，前端不需要自己配对。

`web/js/conn.js` 处理连接与重连，`web/js/player.js` 是大屏，`web/js/debug.js` 是调试页，`web/js/draw.js` 负责骨架与曲线绘制，`web/js/audio.js` 用 WebAudio 合成口令鼓点。前端的 `S.tremor` 曲线直接来自二进制帧里的 `player.value`，没有本地判定逻辑。

前端没有构建步骤：原生 ES modules 与 CSS，改完刷新即可。样式单位用的是容器查询单位（`cqh` / `cqw`），按屏幕高度缩放，所以在不同分辨率下版式一致。

修改二进制帧的字段时，需要同步核对 `server/app.py` 的 `_pipeline`、`web/js/conn.js` 的解析和 `web/js/player.js` 的消费处。

## 常见修改入口

| 需求 | 修改位置 | 同步核对 |
| --- | --- | --- |
| 换姿态模型尺寸 | `configs/hardware.json` 的 `vision.config` | 用 `tools/bench_pose.py` 重测耗时与抖动 |
| 调整误检过滤 | `configs/hardware.json` 的 `vision.min_score` / `min_keypoints` | 空画面是否出现假人体 |
| 调整判定灵敏度 | `configs/game.json` 的 `difficulties` 三档 | 用 `tools/replay.py` 回放录制数据进行批量比对 |
| 调整抖动容限 | `configs/game.json` 的 `jitter_floor` | `MotionJudge` 的上下限常量在 `server/vision.py` |
| 新增姿势 | `server/poses.py` 的规则与 `POSES` 注册表 | 调试页姿势复选框会自动出现；未知 id 会被校验拦下 |
| 改轮数与时长 | `configs/game.json` | `server/game.py` 的 `FIELDS` 校验范围 |
| 换执行器 | 实现与 `server/servo.py` 相同的方法集 | 状态字段 `positions` / `speed_us_per_s` / `settle_s` 的读写 |
| 改大屏版式 | `web/css/player.css`、`web/js/player.js` | 板载 Chromium 上实拍确认，单位是 `cqh` |

改动判定逻辑时，建议先在 `tests/test_logic.py` 中补充最小复现用例，再调整实现逻辑。

## 测试入口与修改建议

日常改动先跑离线逻辑测试，它不碰摄像头也不申请 GPIO：

```bash
.venv/bin/python -m unittest tests.test_logic
```

它是用假舵机（`FakeServo`）、`mock.Mock` 替掉的摄像头与模型，加上一个替换了 `time.monotonic` 的假时钟来驱动真实状态机的，所以状态转换、停稳、超时这类时序问题也能在离线按需加速复现，不必占用板子的设备。

涉及页面改动时，服务跑起来后执行 `node tests/browser_check.mjs`，它通过 `tests/cdp.mjs`（最小的 DevTools 协议封装）驱动板载 Chromium，检查页面加载与控制台报错。

需要确认硬件链路时：

| 目的 | 命令 |
| --- | --- |
| 确认 GPIO 权限配置 | `./scripts/setup_k3_gpio.sh --check` |
| 单独驱动舵机、看脉冲误差 | `.venv/bin/python tools/servo_gpio_test.py` |
| 边看画面边定档位 | `.venv/bin/python tools/camera_servo_test.py` |
| 服务跑起来后的冒烟检查 | `.venv/bin/python scripts/smoke_service.py` |
| 比较模型耗时与抖动 | `.venv/bin/python tools/bench_pose.py` |

修改脉冲生成逻辑时，需结合 [servo.md](servo.md) 了解停稳后停发脉冲的机制：摄像头固定在云台上，持续发送脉冲会引发机械微颤，导致画面差分将机械晃动误判为人身动作。
