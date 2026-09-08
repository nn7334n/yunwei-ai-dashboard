#!/bin/bash
# AI 控制面板启动器
DASHBOARD_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DASHBOARD_DIR"

# 清除代理
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
export no_proxy="*"
export NO_PROXY="*"

# 释放 9090 端口并杀掉旧进程
lsof -ti :9090 | xargs kill -9 2>/dev/null
pkill -9 -f "server.py" 2>/dev/null
sleep 1

# 启动后台服务
nohup python3 server.py > /tmp/ai-dashboard.log 2>&1 &
PID=$!
sleep 2

# 检查是否启动成功
if ps -p $PID > /dev/null 2>&1; then
  echo "AI 控制面板已启动: http://localhost:9090"
  open http://localhost:9090
else
  echo "启动失败，查看日志: /tmp/ai-dashboard.log"
  cat /tmp/ai-dashboard.log
fi
