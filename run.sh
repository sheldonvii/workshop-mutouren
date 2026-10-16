#!/usr/bin/env bash
# 启动赛博木头人服务。参数原样传给 server.app，例如 ./run.sh --port 8322 --verbose
set -euo pipefail
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { echo "缺少 .venv，见 README 的安装步骤" >&2; exit 1; }
[ -f models/yolov8m-pose.q.onnx ] || { echo "缺少姿态模型，先运行 .venv/bin/python scripts/download_models.py" >&2; exit 1; }
exec .venv/bin/python -m server.app "$@"
