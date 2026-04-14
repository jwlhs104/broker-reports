#!/bin/bash
# 啟動 Broker Reports Web UI
cd "$(dirname "$0")/.."

# 載入 .env
if [ -f web/.env ]; then
    set -a
    source web/.env
    set +a
fi

uvicorn web.app:app --host 0.0.0.0 --port 8200 --reload
