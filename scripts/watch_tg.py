"""盤中即時監控 — 新券商報告 AI 研判 + Telegram 推送

用法:
  python -m scripts.watch_tg                       # 啟動盤中監控 (預設 MEDIUM 以上推送)
  python -m scripts.watch_tg --priority HIGH       # 只推送高優先度
  python -m scripts.watch_tg --priority LOW        # 推送所有訊息
  python -m scripts.watch_tg --interval 60         # 每 60 秒輪詢
  python -m scripts.watch_tg --no-text             # 不監控文字訊息，只看文件
  python -m scripts.watch_tg --all-hours           # 不限盤中時間，全天監控
  python -m scripts.watch_tg --once                # 只跑一次（不進入 daemon 模式）
  python -m scripts.watch_tg --reset               # 重設水位線，從頭開始
"""

import argparse
import os
import sys
from pathlib import Path

# 注意：不在此處清除 CLAUDECODE 環境變數
# Agent SDK 需要完整環境來找到 Claude 二進位檔
# 巢狀問題由 _triage_subprocess.py 在子進程中處理

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import CONFIG
from src.tg_watcher import ReportWatcher

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="盤中即時監控 — AI 研判 + Telegram 推送")
    parser.add_argument(
        "--priority", choices=["HIGH", "MEDIUM", "LOW"], default="MEDIUM", help="最低推送優先度 (預設 MEDIUM)"
    )
    parser.add_argument("--interval", type=int, default=30, help="輪詢間隔秒數 (預設 30)")
    parser.add_argument("--no-text", action="store_true", help="不監控文字訊息，只監控文件 (PDF/DOCX)")
    parser.add_argument("--all-hours", action="store_true", help="全天監控 (不限盤中時間)")
    parser.add_argument("--once", action="store_true", help="只執行一次輪詢 (不進入 daemon 模式)")
    parser.add_argument("--reset", action="store_true", help="重設水位線，從 ID=0 重新開始")
    parser.add_argument(
        "--model", type=str, default="claude-haiku-4-20250414", help="Claude 研判模型 (預設 claude-haiku-4-20250414)"
    )

    args = parser.parse_args()

    # 檢查必要設定
    tg_config = CONFIG.get("tg_archiver", {})
    if not tg_config.get("bot_token"):
        print("❌ 缺少設定: tg_archiver.bot_token")
        print("   請在 config.yaml 中設定 Telegram Bot Token")
        sys.exit(1)
    if not tg_config.get("notify_chat_id") and not args.once:
        print("❌ 缺少設定: tg_archiver.notify_chat_id")
        print("   請在 config.yaml 中設定通知目標 Chat ID")
        print("   (可以是你的 Telegram User ID，用 @userinfobot 查詢)")
        sys.exit(1)

    # 重設水位線
    if args.reset:
        checkpoint = Path(CONFIG["paths"]["reports_dir"]).parent / ".watcher_checkpoint"
        if checkpoint.exists():
            checkpoint.unlink()
            print("✅ 水位線已重設")

    watcher = ReportWatcher(
        min_priority=args.priority,
        poll_interval=args.interval,
        also_watch_text=not args.no_text,
        triage_model=args.model,
    )

    if args.once:
        count = watcher.poll_once()
        print(f"處理了 {count} 則訊息")
    else:
        watcher.run(market_hours_only=not args.all_hours)
