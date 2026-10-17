# Agent 接手指南

本文为自动化 Agent 与接手开发者说明项目结构、设备边界与提交规则。当前仓库在 SpacemiT K3 上运行姿态识别，并通过 GPIO 软件脉冲驱动一台 PWM 舵机云台。

## 接手检查

1. 确认当前分支与工作树状态：

   ```bash
   pwd
   git branch --show-current
   git status --short --branch
   ```

2. 运行不占用硬件的自动化校验：

   ```bash
   .venv/bin/python -m unittest tests.test_logic
   ```

   若有失败项，先记录终端输出，区分环境依赖问题与代码逻辑问题。用例覆盖与执行方式参考 [docs/code-guide.md](docs/code-guide.md#测试入口与修改建议)。

3. 根据开发任务查阅对应文档：
   * 基础环境与快速启动：[README.md](README.md)
   * 代码模块与调用链路：[docs/code-guide.md](docs/code-guide.md)
   * 接口协议与判定算法：[docs/development.md](docs/development.md)
   * 硬件接线与引脚权限：[docs/hardware.md](docs/hardware.md)
   * 脉冲原理与舵机标定：[docs/servo.md](docs/servo.md)

## 模块职责与核心约定

系统将视觉处理、状态流转与硬件控制解耦，判定与姿势评估分别维持单一口径：

| 关注方向 | 核心模块 | 调用口径与设计约定 |
| --- | --- | --- |
| 静止判定 | `server/vision.py` (`MotionJudge`) | 基准建立、逐关节容差与连续帧确认统一由此处处理；依赖 `frame_motion` 提供的画面晃动值评估停稳状态 |
| 姿势评估 | `server/poses.py` (`evaluate`) | 提供五个姿势的几何判定，返回匹配、不匹配与不可见三态结果，业务层直接消费判定状态 |
| 玩法状态机 | `server/game.py` (`Game`) | 集中管理倒计时、红绿灯阶段切换、防抖滤波与停发脉冲时机 |
| 舵机驱动 | `server/servo.py` (`ServoController`) | 独立子进程以 `SCHED_FIFO` 调度生成高精度脉冲，未获 GPIO 权限时平滑降级至模拟模式 |
| 服务编排 | `server/app.py` | 设定 CPU 亲和性，管理采集、推理及通信线程，分发 HTTP 与 WebSocket 数据流 |

改动判定逻辑时，在 `tests/test_logic.py` 中补充验证用例后再调整实现。

## 设备与硬件边界

自动化测试默认不打开摄像头，亦不申请 GPIO 资源。连接实体硬件验证时，先通过 `fuser -v /dev/video1` 确认设备未被占用，再执行 `./run.sh`。

### 1. 进程生命周期与资源回收
舵机脉冲运行于独立的 `multiprocessing` 子进程中。正常通过 Ctrl-C 退出时主进程会等待并回收子进程；若使用强制终止信号（如 `kill -9`），子进程可能残留并持续占用 GPIO 线路。再次启动若提示引脚被占用，可通过 `pgrep -af spawn_main` 定位并清理残留进程。

### 2. 权限生效机制
执行 `scripts/setup_k3_gpio.sh` 会更新用户组与系统 limits。配置完成后需新开终端连接，并通过 `./scripts/setup_k3_gpio.sh --check` 复核生效状态。

### 3. 硬件驱动策略
云台依赖 Linux GPIO 字符设备与 pinctrl 自动切换生成脉冲，无需调整系统设备树（DTB）或板级配置。摄像头控制项由 `configs/hardware.json` 中的 `camera.controls` 配置并经 `v4l2-ctl` 下发，更换设备时先通过 `v4l2-ctl -d <设备> -l` 核对参数名称。

## 数据与配置边界

* **版本控制排除项**：模型权重、虚拟环境、录制数据、快照及日志均不纳入 Git 仓库管理。
* **参数标定流程**：`configs/game.json` 与 `configs/hardware.json` 中的默认值供初始化参考。实际运行需通过现场录制回放标定静止阈值，并依据安装位姿标定舵机档位 `positions`。调试页面的“应用”仅修改内存状态，“应用并保存”会持久化回写至配置文件。详细参数说明见 [docs/development.md](docs/development.md#静止判定与参数调优)。

## 分支与提交规则

开发在当前工作分支展开，改动聚焦于目标任务。

未获明确授权时，Agent 不执行 commit、push、创建分支或重置操作。任务完成后提供修改文件清单、验证命令与测试结果；收到提交要求后，先整理提交信息并等待确认。
