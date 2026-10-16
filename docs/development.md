# 架构设计与开发指南

本文介绍赛博木头人的数据流、姿态与静止判定算法、游戏状态机、HTTP 与 WebSocket 接口，以及参数调优和二次开发的入口。

## 运行环境与依赖

Demo 运行于 SpacemiT K3 平台（Bianbu 4.0.7 riscv64，Python 3.14.4）。

| 依赖组件 | 安装路径与方式 | 验证方法 |
| --- | --- | --- |
| SpacemiT 视觉库 | `sudo apt install opencv-spacemit=5.0.0-1bb1`（安装至 `/opt/opencv-spacemit`） | `dpkg-query -W opencv-spacemit` 应输出 `5.0.0-1bb1` |
| Python 依赖 | `uv pip install --python .venv/bin/python --index-url https://git.spacemit.com/api/v4/projects/33/packages/pypi/simple -r requirements.txt` | `python -c "import cv2; print(cv2.__version__)"` 应输出 `5.0.0` |
| Vision NPU 扩展 | 随 `requirements.txt` 安装，`spacemit_vision==0.1.4` | `python -c "from spacemit_vision import VisionServiceNative; print(VisionServiceNative)"` |
| 姿态模型权重 | `.venv/bin/python scripts/download_models.py`，落在 `models/` | 脚本按 SHA-256 校验，`--verify-only` 只校验不下载 |

镜像索引只能用一个源。`uv` 中 `--extra-index-url` 的优先级高于 `--index-url`，混用会把包解析成源码分发并触发本地编译，在 riscv64 上会失败。版本钉选的理由写在 [requirements.txt](../requirements.txt) 的注释里。

## 数据链路与模块划分

服务是单进程多线程的：一个采集线程、一个推理线程、一个状态机线程、一个指标推送线程，外加一个独立的舵机脉冲进程。

```text
USB 摄像头（MJPG 1280×720@30fps）
  └──> server/camera.py          采集线程，只保留最新一帧；断线自动重连
        └──> server/vision.py    YOLOv8-Pose 推理（NPU，NPU 上约 50ms/帧）
              ├──> 关键点过滤与人体框筛选
              ├──> server/poses.py       姿势规则判定（绿灯期间）
              ├──> MotionJudge           静止判定（红灯期间，逐关节抖动容忍）
              └──> server/game.py        状态机：推进阶段、判定淘汰
                    ├──> server/servo.py        GPIO 软件脉冲进程
                    ├──> server/hub.py          WebSocket 推送（事件 + 预览帧）
                    ├──> server/recorder.py     录制 jsonl，供回放调参
                    └──> server/snapshots.py    淘汰抓拍与骨架绘制
```

| 模块 | 职责 |
| --- | --- |
| `camera.py` | 打开设备、下发控制项、连续读帧；曝光等控制项由配置驱动，见 [hardware.md](hardware.md#摄像头) |
| `vision.py` | 模型封装、关键点过滤、`Person` 数据结构、`PlayerTracker` 玩家跟踪、`MotionJudge` 静止判定、`frame_motion` 画面晃动 |
| `poses.py` | 五个姿势的几何规则，返回「摆对 / 摆错 / 看不清」三态 |
| `game.py` | 游戏状态机、人数统计、舵机档位切换、停稳判定、淘汰与胜负 |
| `servo.py` | GPIO 软件脉冲与模拟驱动，对上层接口相同 |
| `hub.py` | 手写的最小 WebSocket 服务（RFC 6455 子集），只推送 |
| `app.py` | HTTP 服务、静态文件、参数读写、指标上报 |

## 姿态推理

用 YOLOv8-Pose 的三个官方尺寸（n / s / m），默认 m。COCO 17 关键点，左右指玩家自身的左右，坐标是原始采集帧的像素坐标。

### 误检过滤

空画面上模型会输出覆盖整帧的假人体，所以每个结果要过两道筛：

* `vision.min_score`（默认 0.5）：整体置信度下限。
* `vision.min_keypoints`（默认 5）：可见关键点数量下限，可见指 `visibility >= vision.keypoint_min_visibility`（默认 0.5）。

### 尺度归一化

所有位移阈值都以**躯干长度**为单位，而不是像素。躯干取两肩中点与两髋中点的距离；这四个点不可见时退回 `0.4 × 人体框高度`（成年人躯干约占身高 0.3～0.35，而框通常略高于人）。这样同一个阈值在远近不同的位置含义一致。

### 模型选择

| 模型 | 单帧耗时（K3，6 线程） | 关键点抖动 |
| --- | --- | --- |
| `yolov8n-pose` | 明显更低 | 约为 m 的 2 倍 |
| `yolov8m-pose` | 约 50ms | 基准，默认 |

m 模型配 1280×720@15fps 的推理节奏刚好跟得上。`tools/bench_pose.py` 可以重新测：它把同一张测试图加上亚像素平移与传感器噪声，反复推理后统计各关键点位置的标准差，同时给出「把人缩小一半」的远处版本，用来评估远距离下的抖动。

## 玩家跟踪

`PlayerTracker` 是单人锁定：开局取画面中面积最大的人，之后每帧按中心距离与身高比找回同一个人。

| 参数 | 规则 |
| --- | --- |
| 中心距离 | 以参考身高为单位，超过 0.6 视为不是同一人 |
| 身高比 | 需落在 0.6～1.6 之间 |

红灯判定使用严格模式：找不到目标就按「离开画面」处理。绿灯使用宽松模式：找不到相近的人时退回画面中最大的人，因为绿灯期间玩家可以自由走动。多人同框时只判定锁定的那一位，其他人既不影响判定也不被判定。

## 姿势规则

五个姿势用几何规则判断，全部以躯干长度为单位（见 [server/poses.py](../server/poses.py)）：

| 姿势（`POSES` 的 id 与名称） | 判定条件 |
| --- | --- |
| `raise_left` 举起左手 / `raise_right` 举起右手 | 该侧手腕高于肩线 0.5 个躯干，且另一侧手臂下垂或不可见 |
| `both_up` 双手举过头顶 | 两侧手腕都高于肩线 0.5 个躯干 |
| `hands_on_hips` 双手叉腰 | 手腕与髋部高度差不超过 0.45 个躯干、横向不超出肩宽加 0.5 个躯干，且肘部横向离开身体至少半个躯干加 0.3 |
| `t_pose` 双臂平举 | 两侧手腕都低于肩线，横向明显超出肩宽 |

所需关键点看不清时返回「看不清」，按 `game.unclear_policy` 处理：`pass` 只判静止、不淘汰（默认），`eliminate` 直接淘汰。

## 静止判定与参数调优

红灯期间的判定分四步，实现在 `vision.MotionJudge`：

**一、建立基准。** 进入红灯前的宽限期（`grace_s`，默认 1.0 秒）内不判定，玩家摆好姿势。基准取宽限期**最后 5 帧**的中位姿势。用最后几帧而不是全程，是因为玩家在宽限期前段可能还在调整姿势。

**二、估计抖动容忍。** 用宽限期**最后 8 帧**的相邻帧差估计每个关节自身的噪声：取帧差的中位绝对偏差换算成稳健标准差，乘以系数 3 得到该关节的容忍量，再夹在 `jitter_floor`（默认 0.03）与 0.15 个躯干之间。噪声超过 0.3 的关节直接剔除，不参与判定。眼睛和耳朵（四点）不参与判定，因为头部小幅晃动的位移在那里会被放大。

**三、逐帧比较。** 当前帧取最近 3 帧的中位，与基准比较，算出每个关节超出自身容忍量的部分，判定值取**全身最大的那一个**。取最大而不是平均值，是为了让单肢动作不被其他静止部位稀释——这一点曾出过一个 bug：判定值取全身前 3 点的平均，而页面按部位最大值标红，结果单臂动作在页面上标红了却不判失败。

**四、连续帧确认。** 判定值连续超过阈值达到 `consecutive` 帧才算「动了」，避免单帧跳点误判。

| 参数（`configs/game.json`） | 默认值 | 含义 |
| --- | --- | --- |
| `grace_s` | 1.0 | 宽限期时长，用来建立基准与估计抖动 |
| `jitter_floor` | 0.03 | 每个关节最低的抖动容忍（躯干长度） |
| `difficulty` / `difficulties` | normal | 三档难度的 `threshold`（阈值）与 `consecutive`（连续帧） |
| `easy` | 0.15 / 4 | 阈值宽松，需要更多连续帧 |
| `normal` | 0.10 / 3 | 默认 |
| `hard` | 0.06 / 2 | 阈值严格，判定更快 |

### 画面静止与停稳

舵机没有位置反馈。转动到位后，服务靠**画面**判断停稳：`frame_motion` 把帧缩成 96×54 灰度图求相邻帧的平均绝对差，连续 `still_frames`（默认 4）帧低于 `still_threshold`（默认 2.5）才算静止，或者最多再等 `settle_timeout_s`（默认 3.0）秒。停稳后才停发脉冲并进入宽限期。

实测静止时该值的中位数约 0.75，所以 2.5 这个阈值留了足够余量。如果现场光线变化剧烈或摄像头自动曝光频繁调整，可以把阈值调大一些。

### 调参方法

不要靠反复真人重玩来试参数。正确流程是录制一次、回放多次：

```bash
# 1. 在调试页点「开始录制」，让人分别做三种动作各录一段
#    站住不动 / 轻微晃动 / 明显动作；结束后点停止录制
# 2. 用回放脚本在不同参数下重跑判定
.venv/bin/python tools/replay.py --threshold 0.1 --consecutive 3 recordings/<文件>.jsonl
```

判定目标是：站住不动的那段不出现「动了」，明显动作的那段能稳定判出「动了」，轻微晃动落在两者之间由难度档决定。调试页的曲线图会画出扣除抖动前后的两条线，红色的虚线是阈值，可以直接看出余量。

## 游戏状态机

阶段定义与转换（见 [server/game.py](../server/game.py)）：

```text
idle ──开始──> countdown ──> green ──口令结束──> turning
                              ↑                    │
                              │                停稳（画面静止或超时）
                        safe（本轮过关）          ↓
                              ↑                 red（宽限 → 判定）
                              └──────────────────┤
                                                 ├─ 判定通过 ─> safe ─> 下一轮 green
                                                 └─ 判定失败 ─> eliminated
                                                 └─ 走完全部轮次 ─> win
```

| 阶段 | 说明 |
| --- | --- |
| `idle` | 待机，等待开局。需要画面里有人、模型与摄像头就绪才能开始 |
| `countdown` | 倒计时，`countdown_s` 秒 |
| `green` | 绿灯，云台背对玩家；屏幕给出本轮姿势并在 `green_s` 区间内随机取值后结束 |
| `turning` | 云台转回，等待画面静止或超时 |
| `red` | 红灯判定，分 `grace`（宽限）与 `watch`（盯人）两个子阶段 |
| `safe` | 本轮过关，停留 `safe_s` 秒 |
| `eliminated` / `win` | 淘汰 / 胜利，停留 `result_hold_s` 秒后可再开一局 |
| `paused` / `error` | 暂停 / 设备异常 |

人数统计做了防抖：连续 `PEOPLE_STABLE_FRAMES`（4）帧人数一致才更新，避免瞬时误检导致 `can_start` 反复跳变。

## HTTP 接口

| 路径 | 方法 | 请求体 | 核心响应 | 说明 |
| --- | --- | --- | --- | --- |
| `/api/health` | GET | 无 | `phase`、`camera`、`model`、`servo` | 各子系统的健康状态与实时指标 |
| `/api/config` | GET | 无 | `game`、`servo`、`poses`、`protocol` | 当前生效的全部配置 |
| `/api/config` | PUT | `{"game": {...}, "servo": {...}, "save": true}` | 同上 | 改配置；`save` 为真时写入 `configs/`，否则只在内存生效 |
| `/api/game` | POST | `{"action": "start" \| "stop" \| "reset" \| "pause" \| "resume"}` | `message`、`state` | 控制游戏；`stop` 与 `reset` 都回到待机，区别只在提示文字 |
| `/api/servo/test` | POST | `{"position": "face"}` 或 `{"us": 1500}` | 舵机状态 | 手动转动，游戏进行中返回 409 |
| `/api/record` | POST | `{"action": "start" \| "stop"}` | `file`、`active`、`frames` | 录制控制 |
| `/api/snapshots/<id>.jpg` | GET | 无 | JPEG | 淘汰抓拍 |
| `/api/ws` | GET | WebSocket | 见下文 | 事件与预览帧推送 |
| `/`、`/debug` | GET | 无 | HTML | 玩家大屏与调试页 |

请求体上限 64KB，超限返回错误。

## WebSocket 协议

服务端只推送，所有操作走 HTTP。两种帧：

**文本帧**是 JSON 事件，顶层一定带 `v`（协议版本，当前为 1）与 `type`：

| `type` | 触发 | 主要字段 |
| --- | --- | --- |
| `hello` | 连接建立或配置变更 | `version`、`config` |
| `state` | 阶段变化 | `phase`、`sub`、`round`、`rounds`、`pose`、`people`、`can_start`、`message`、`problems`、`ends_in_ms` |
| `verdict` | 一次判定结束 | `result`、`round`、`reason` |
| `servo` | 舵机状态变化 | `target`、`us`、`moving`、`settled`、`output`、`positions` |
| `metrics` | 每秒一次 | `camera_fps`、`vision_fps`、`infer_ms`、`clients` |
| `audio` | 音频提示 | `cue`（`chant` / `turn` / `eliminated` / `win`）、`text` |
| `error` | 子系统异常 | `source`、`code`、`message`、`recoverable` |

**二进制帧**是预览画面，格式为：

```text
[4 字节大端长度][JSON 元数据][JPEG 数据]
```

元数据里有 `seq`、`size`、`phase`、`sub`、`count`、`people`（每人的人体框、得分、17 个关键点）、`player`（当前玩家与判定值）、`motion`、`infer_ms`。骨架和画面出自同一帧，接收方不需要自己配对。每个连接只保留最新一帧，忙时主动丢帧而不是排队。

`player` 字段是判定过程的核心输出：`value` 是扣除抖动容忍后的判定值，`raw` 是原始位移，`threshold` 是当前阈值，`parts` 是各部位的判定值（用于画面上标红动了的部位），`judging` 表示当前是否处在判定阶段。调试页的曲线图用的就是这三个值。

前端实现在 `web/js/`，无构建步骤：`conn.js` 处理连接与重连，`player.js` 是大屏，`debug.js` 是调试页，`draw.js` 负责骨架与曲线绘制。

## 录制、回放与抓拍

`server/recorder.py` 把每一帧的结果写成 jsonl：每行一条记录，含帧序号、时间、阶段、判定值、原始位移、玩家关键点、画面晃动。文件存在 `recordings/`（已排除 Git）。

`server/snapshots.py` 在触发淘汰时保存该帧的抓拍，并在图上画出骨架：玩家用高亮色与红框标出，动了的部位用更粗的线段，其他人体用低饱和色淡化。最多保留 20 张，存在 `snapshots/`。

## 测试与代码规范

```bash
.venv/bin/python -m unittest tests.test_logic   # 21 项逻辑测试，无需硬件
node tests/browser_check.mjs                    # 无头浏览器检查
.venv/bin/python scripts/smoke_service.py       # 对运行中的服务做冒烟
```

`tests/test_logic.py` 用假舵机、假摄像头与假模型驱动状态机，覆盖姿势规则、静止判定、抖动容忍、单肢动作、停发脉冲、停稳超时、人数防抖与配置校验。新增判定逻辑时应当在这里补一条能复现问题的用例——此前「肢体超过阈值不判失败」的 bug 就是先用两条测试复现、再修的。

`tests/cdp.mjs` 是驱动板载 Chromium 的最小 DevTools 协议封装，`browser_check.mjs` 基于它检查页面加载与控制台报错。

编码规范：Python 用 Ruff（`check --select F,I` 与 `format`），前端为原生 HTML/CSS/JS；所有文件 UTF-8、LF 换行。

## 二次开发入口

| 想做什么 | 改哪里 |
| --- | --- |
| 换姿态模型或改过滤阈值 | `configs/hardware.json` 的 `vision`；模型配置在 `configs/yolov8*-pose.yaml` |
| 改游戏规则（轮数、时长、难度） | `configs/game.json`，或调试页实时改 |
| 加一个姿势 | `server/poses.py` 增加规则并在 `POSES` 注册，前端会自动出现选项 |
| 换执行器（别的云台、灯带、电机） | 实现与 `server/servo.py` 相同的接口：`start` / `goto` / `set_us` / `release` / `status` / `stop` |
| 多人玩法 | `PlayerTracker` 目前是单人锁定，需要改成多目标跟踪并给每人一个 `MotionJudge` 实例 |
| 接自己的程序 | 用 WebSocket 收二进制帧即可，格式见上文；不需要用前端 |
| 改界面 | `web/` 下是纯静态文件，改完刷新页面即可，没有构建步骤 |
