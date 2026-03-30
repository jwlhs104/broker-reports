#!/bin/bash
# 每日 Dropbox 同步 + 萃取券商報告
# crontab: 0 8 * * * /Users/jimquant/Desktop/workspace/broker-reports/scripts/cron_sync.sh

set -e

LOGFILE="/Users/jimquant/Desktop/workspace/broker-reports/logs/sync_$(date +%Y%m%d).log"
PROJECT_DIR="/Users/jimquant/Desktop/workspace/broker-reports"
PYTHON="/Users/jimquant/miniconda3/envs/ccbot/bin/python3"

# 確保 log 目錄存在
mkdir -p "$(dirname "$LOGFILE")"

echo "========== $(date) ==========" >> "$LOGFILE"

cd "$PROJECT_DIR"

# 加入 rclone 路徑
export PATH="/opt/homebrew/bin:$PATH"

# Step 1: 從 Dropbox 下載新檔案 + 掃描入 DB
$PYTHON -m scripts.sync_dropbox >> "$LOGFILE" 2>&1

# Step 2: LLM 萃取新報告（限制每次最多 50 份避免 API 費用爆掉）
$PYTHON -m scripts.ingest_all --limit 50 >> "$LOGFILE" 2>&1

echo "========== 完成 $(date) ==========" >> "$LOGFILE"
