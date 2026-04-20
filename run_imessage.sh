#!/bin/bash

# 自动发送 iMessage 脚本
# 运行路径和日志记录

PROJECT_DIR="/Users/wangyingqing/Desktop/projects/auto_send_imessage"
LOG_DIR="$PROJECT_DIR/logs"

# 创建日志目录
mkdir -p "$LOG_DIR"

# 带时间戳的日志文件
LOG_FILE="$LOG_DIR/imessage_$(date '+%Y-%m-%d_%H-%M-%S').log"

# 执行脚本并记录日志
cd "$PROJECT_DIR"
{
    echo "=========================================="
    echo "开始执行 iMessage 发送任务"
    echo "执行时间: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "=========================================="
    
    python3 main.py
    
    echo "=========================================="
    echo "任务执行完成"
    echo "完成时间: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "=========================================="
} >> "$LOG_FILE" 2>&1

exit 0
