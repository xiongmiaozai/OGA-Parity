#!/usr/bin/env bash
# MC Java -> Bedrock Vibrant Visuals 转换工具：一键启动离线工作台
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8000}"
echo "启动离线转换工作台：http://127.0.0.1:${PORT}"
exec python3 server.py
