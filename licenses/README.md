# 第三方来源

| 内容 | 来源与许可 |
| --- | --- |
| 数字字体 | [Big Shoulders Stencil Display](https://github.com/xotypeco/big_shoulders)，SIL OFL 1.1，完整许可见 `BigShouldersStencilDisplay-OFL.txt`。用于号码布号码、轮次与倒计时数字 |
| 页面图标 | [Lucide 1.8.0](https://github.com/lucide-icons/lucide/tree/1.8.0)，完整许可见 `Lucide-LICENSE`，包含 ISC 及其继承图标的 MIT 条款 |
| 姿态模型权重 | [SpacemiT Model Zoo](https://archive.spacemit.com/spacemit-ai/model_zoo/) 的 `yolov8n/s/m-pose.q.onnx`，按 `scripts/download_models.py` 所列地址取得并校验 SHA-256。权重不随仓库分发 |
| 姿态推理运行时 | `spacemit_vision` 与 `onnxruntime` 的 SpacemiT 版本，从 [SpacemiT 私有 PyPI 索引](https://git.spacemit.com/api/v4/projects/33/packages/pypi/simple)取得 |
| 测试图片 | `tools/testdata/pose-sample.jpg` 由官方示例图片 `006_test.jpg` 制作，来源目录为 [官方示例图片](https://archive.spacemit.com/spacemit-ai/model_zoo/assets/image/)，上游未在本项目中附图片许可文本。同目录的 `pose-sample-people.json` 是在 K3 上用 YOLOv8n-Pose 实际推理得到的骨架数据，供 `tools/bench_pose.py` 使用 |
| 界面与游戏设计参考 | 视觉方向来自《鱿鱼游戏》的场地配色与「一二三木头人」玩法，相关出处列在仓库外的设计调研文档中。仓库内不使用剧集截图、玩偶与角色形象、官方 Logo 与字体，也不使用「Squid Game」「鱿鱼游戏」字样 |

第三方许可证保留各自适用范围，不作为整个项目或全部权重的统一授权声明。中文界面使用系统自带的 Noto Sans CJK SC，不随仓库分发。
