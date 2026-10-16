# 赛博木头人 (Cyber Mugunghwa)

[![Platform](https://img.shields.io/badge/Platform-SpacemiT%20K3-0052CC?logo=linux&logoColor=white)](https://spacemit.com)
[![OS](https://img.shields.io/badge/OS-Bianbu%204.0.7%20(riscv64)-E95420?logo=ubuntu&logoColor=white)](https://archive.spacemit.com)
[![Python](https://img.shields.io/badge/Python-3.14.4-3776AB?logo=python&logoColor=white)](https://www.python.org)
[![Arch](https://img.shields.io/badge/Arch-RISC--V%2064--bit-16A34A?logo=riscv&logoColor=white)](https://riscv.org)

赛博木头人是运行在 SpacemiT K3 上的「一二三木头人」游戏。K3 用本地姿态模型判断玩家在红灯期间有没有动，同时通过 GPIO 软件脉冲驱动一台 PWM 舵机云台，让摄像头随游戏节奏转身，把「木头人回头了」这个动作做成真实的物理现象。

### 它能做什么

* **本地实时姿态判定**：YOLOv8-Pose 在 K3 的 NPU 上运行，逐帧输出 17 个 COCO 关键点，不依赖云端。判定器为每个关节单独估计抖动容忍，扣除人自然晃动后再比较，避免站着也判「动了」。
* **GPIO 软件脉冲驱动舵机**：不依赖 R 域 PWM 与设备树改动，直接用 Linux GPIO 字符设备的 uAPI v2 在独立进程里生成 20ms 周期的脉冲，绑定 CPU 并启用实时调度，实测脉宽误差在 ±30μs 量级。
* **可调参数与录制回放**：判定阈值、宽限时间、停稳判定等参数都能在调试页实时修改；录制一局后可用回放脚本在不同阈值下反复验证，不必每次真人重玩。

---

## 快速上手

### 1. 软件环境与模型

运行环境为 Bianbu 4.0.7 riscv64，要求 Python 3.14.4 与 uv。

```bash
# 创建虚拟环境
export UV_CACHE_DIR="$PWD/.uv-cache"
~/.local/bin/uv venv .venv --python /usr/bin/python3.14

# 安装依赖（只用 SpacemiT 下游索引，riscv64 预编译轮子）
~/.local/bin/uv pip install --python .venv/bin/python \
  --index-url https://git.spacemit.com/api/v4/projects/33/packages/pypi/simple \
  -r requirements.txt

# 下载并校验姿态模型权重
.venv/bin/python scripts/download_models.py
```

依赖版本与索引的注意事项写在 [requirements.txt](requirements.txt) 的注释里。模型权重存放在 `models/`，已排除 Git。

### 2. 硬件连接与权限

按 [硬件接口与电气规格](docs/hardware.md) 完成舵机、摄像头与供电连接，然后配置 GPIO 权限：

```bash
sudo ./scripts/setup_k3_gpio.sh
```

脚本把 SoC 的 GPIO 控制器交给系统组 `gpio`，并允许组成员使用实时调度，普通用户即可运行舵机程序。**执行后必须重新登录会话或新开 SSH 连接，已打开的终端看不到新加的组。** 用 `./scripts/setup_k3_gpio.sh --check` 可以只检查不改动。

### 3. 运行 Demo

```bash
./run.sh            # 前台运行，Ctrl-C 停止
```

| 页面 | 地址 | 用途 |
| --- | --- | --- |
| 玩家大屏 | `http://<K3 地址>:8322/` | 空格开局或再来一局，Esc 停止。浏览器要求先按键或点一下才会出声 |
| 调试页 | `http://<K3 地址>:8322/debug` | 判定值曲线、规则参数、舵机标定、录制、事件日志 |

服务默认监听 `0.0.0.0`，同一局域网内都能看到摄像头画面。只在本机使用时，把 [configs/hardware.json](configs/hardware.json) 的 `server.host` 改为 `127.0.0.1`。

### 4. 首次标定

换一台云台或重新装配后，两级舵机档位需要重新标定：

1. 打开调试页，用滑块把摄像头转到正对玩家的位置，点「当前脉宽记为面对」；再转到背对玩家的位置，点「当前脉宽记为背对」。
2. 观察两档之间实际转动耗时，填入「转速」与「停稳等待」，点「应用并保存」写入配置。
3. 录制几段静止与小动作（点「开始录制」后打一局），再用 `tools/replay.py` 回放调整阈值。

详细步骤与原理见 [舵机驱动与脉冲](docs/servo.md)。

## 自动化检查

```bash
.venv/bin/python -m unittest tests.test_logic   # 逻辑测试，21 项，不需要硬件
node tests/browser_check.mjs                    # 无头浏览器检查页面与控制台报错
.venv/bin/python scripts/smoke_service.py       # 对运行中的服务做冒烟检查
```

`tests/test_logic.py` 覆盖姿势规则、静止判定、抖动容忍、状态机与配置校验，可以离线运行。浏览器检查通过 DevTools 协议驱动板上的 Chromium，需要服务已在运行。

## 目录结构

| 路径 | 用途 |
| --- | --- |
| `server/` | 服务：采集、推理、状态机、舵机、推送、录制、抓拍 |
| `web/` | 玩家大屏与调试页，原生 CSS 与 ES modules，无构建步骤 |
| `configs/` | `hardware.json` 硬件与舵机档位，`game.json` 规则，姿态模型配置 |
| `scripts/` | GPIO 权限配置、模型下载校验、服务冒烟检查 |
| `tools/` | 舵机单独测试、摄像头与舵机联调页、录制回放、模型对比 |
| `tests/` | 逻辑测试与无头浏览器检查 |
| `docs/hardware.md` | K3 引脚定义、转接板映射、外设规格与供电排障 |
| `docs/servo.md` | GPIO 软件脉冲原理、时序与实时调度、标定与工具 |
| `docs/development.md` | 数据流、姿态与判定算法、HTTP 与 WebSocket 接口、参数说明 |
| `licenses/` | 第三方来源与许可 |

运行时生成的 `recordings/`、`snapshots/`、`logs/` 只保存在 K3 本地，已排除 Git。

## 在它基础上开发

这套代码是按可替换模块组织的，常见的改造入口：

* **换姿态模型**：`configs/` 下有 n、s、m 三个尺寸的配置，改 [configs/hardware.json](configs/hardware.json) 的 `vision.config` 即可；`tools/bench_pose.py` 可以重新比较耗时与抖动。
* **换游戏规则**：状态机与判定参数都在 [configs/game.json](configs/game.json)，也可以在调试页实时修改。
* **换外设**：`server/servo.py` 对上层暴露 `goto` / `set_us` / `release` 三个方法，模拟驱动与 GPIO 驱动可互换，接别的执行器只需实现同样的接口。
* **换前端**：`server/hub.py` 是手写的最小 WebSocket 服务，二进制帧的格式是 4 字节长度 + JSON + JPEG，任何语言都能接。

数据流、算法细节与完整接口定义见 [架构设计与开发指南](docs/development.md)。

## 许可

数字字体与页面图标来自第三方，来源与许可见 [licenses/README.md](licenses/README.md)。姿态模型权重来自 SpacemiT Model Zoo，按下载脚本所列地址取得。
