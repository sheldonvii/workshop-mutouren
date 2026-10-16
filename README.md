# 赛博木头人

在 SpacemiT K3 上还原「一二三木头人」：绿灯时云台背过身，屏幕给出本轮姿势；口令结束云台转回来，K3 用本地姿态模型检查玩家是否摆对、是否保持不动。设计与实施说明见 `../docs/赛博木头人-Demo工程实施文档.md`，前端设计取舍见 `../docs/前端设计调研.md`。

## 安装

在 K3（Bianbu，riscv64）的项目根目录执行：

```bash
export UV_CACHE_DIR="$PWD/.uv-cache"
~/.local/bin/uv venv .venv --python /usr/bin/python3.14
~/.local/bin/uv pip install --python .venv/bin/python \
  --index-url https://git.spacemit.com/api/v4/projects/33/packages/pypi/simple -r requirements.txt
.venv/bin/python scripts/download_models.py
sudo ./scripts/setup_k3_gpio.sh
```

`setup_k3_gpio.sh` 让普通用户无需 sudo 就能用 GPIO 软件脉冲驱动舵机，可重复执行；`--check` 只检查，`--uninstall` 撤销。已经打开的终端要重新登录才能看到新加的组。

姿态模型默认用 yolov8m-pose（`configs/hardware.json` 的 `vision.config`），K3 上 6 线程单帧约 50ms，跟得上 15fps 摄像头；关键点抖动约为 n 的一半。CPU 紧张时可以改回 `configs/yolov8s-pose.yaml` 或 `configs/yolov8n-pose.yaml`，三个模型由下载脚本一起取得。用 `tools/bench_pose.py` 可以重新比较耗时和抖动。

## 运行

```bash
./run.sh            # 前台运行，Ctrl-C 停止
```

| 页面 | 地址 | 用途 |
| --- | --- | --- |
| 玩家大屏 | `http://<K3 地址>:8322/` | 空格开局或再来一局，Esc 停止。浏览器要求先按一次键或点一下才会出声 |
| 调试页 | `http://<K3 地址>:8322/debug` | 判定值曲线、规则参数、舵机标定、录制、事件日志 |

服务默认监听 `0.0.0.0`，同一局域网内都能看到摄像头画面；只在本机用时改 `configs/hardware.json` 的 `server.host` 为 `127.0.0.1`。

## 接线（K3 Pico-ITX）

舵机信号接 26 针 FPC 的 `RX_PWM2`（K3 第 12 脚，GPIO bank0 第 29 根线）。本项目用的转接板与排线组合后编号整体反向：转接板第 n 号对应 K3 第 27−n 脚，所以信号在转接板 15 号，GND 在 1、4、7、10、19、22 号，3.3V 在 25、26 号。舵机应使用 5～6V 独立电源并与 K3 共地。插拔 FPC 前 K3 必须断电。

## 首次标定

1. 打开调试页，在"舵机"里用滑块把摄像头转到正对玩家的位置，点"当前脉宽记为面对"；再转到背对玩家的位置，点"当前脉宽记为背对"。
2. 观察两档之间实际转动要多久，填"转速"和"停稳等待"，点"应用并保存"。
3. 录制几段静止与小动作（"开始录制"后打一局），用 `tools/replay.py` 回放，调整"当前难度阈值"。

## 目录

| 路径 | 内容 |
| --- | --- |
| `server/` | 服务：采集、推理、状态机、舵机、推送、录制、抓拍 |
| `web/` | 玩家大屏与调试页 |
| `configs/` | `hardware.json` 硬件与舵机档位，`game.json` 规则，姿态模型配置 |
| `scripts/` | GPIO 权限配置、模型下载校验、运行中服务冒烟检查 |
| `tools/` | 舵机单独测试、摄像头与舵机联调页、录制回放、模型对比（测试图在 `tools/testdata/`） |
| `tests/` | 逻辑测试（`.venv/bin/python -m unittest tests.test_logic`）、浏览器检查（`node tests/browser_check.mjs`） |
| `licenses/` | 字体与图标许可 |

运行时生成的 `recordings/`、`snapshots/`、`logs/` 只保存在 K3 本地。
