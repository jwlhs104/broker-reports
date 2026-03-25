"""從 tg-archiver 同步新文件到 broker-reports

用法:
  python -m scripts.sync_tg                    # 同步並自動擷取
  python -m scripts.sync_tg --dry-run          # 只列出待同步文件
  python -m scripts.sync_tg --no-extract       # 同步但不執行 LLM 擷取
  python -m scripts.sync_tg --limit 5          # 只同步 5 份
  python -m scripts.sync_tg --status           # 顯示同步狀態
"""

import argparse
import os
import sys

# 清除巢狀 Claude Code 標記（同 ingest_all.py）
for _key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
    os.environ.pop(_key, None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.tg_sync import show_sync_status, sync_documents

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="從 tg-archiver 同步券商報告")
    parser.add_argument("--dry-run", action="store_true", help="只列出待同步文件，不實際複製")
    parser.add_argument("--no-extract", action="store_true", help="同步後不執行 LLM 擷取")
    parser.add_argument("--limit", type=int, default=None, help="限制同步數量")
    parser.add_argument("--status", action="store_true", help="顯示同步狀態統計")
    args = parser.parse_args()

    if args.status:
        show_sync_status()
    else:
        sync_documents(
            dry_run=args.dry_run,
            extract=not args.no_extract,
            limit=args.limit,
        )
