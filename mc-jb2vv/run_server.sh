#!/usr/bin/env bash
# MC-JB2VV 离线转换工作台 · 后台守护脚本
# 功能：崩溃/退出后自动重启；日志写入 server-daemon.log
set -u

ROOT=/workspace/mc-jb2vv
PY=/root/.pyenv/versions/3.11.1/bin/python3
LOG="$ROOT/server-daemon.log"

cd "$ROOT" || exit 1
export PORT=3000
export HOST=0.0.0.0
# 如需指定资源/网页目录可在此 export，例如：
# export MCJB2VV_DATA=/workspace/mc-jb2vv/data
# export MCJB_WEB_DIR=/workspace/mc-jb2vv/web

echo "[$(date '+%F %T')] daemon started (pid=$$)" >> "$LOG"
while true; do
  echo "[$(date '+%F %T')] launching server.py" >> "$LOG"
  "$PY" server.py >> "$LOG" 2>&1
  code=$?
  echo "[$(date '+%F %T')] server.py exited code=$code, restart in 2s" >> "$LOG"
  sleep 2
done
