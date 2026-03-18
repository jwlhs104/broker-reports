"""匯入所有報告: 掃描 → 擷取 → 建立索引

用法:
  python -m scripts.ingest_all              # 完整匯入 (含 LLM 擷取)
  python -m scripts.ingest_all --scan-only  # 只掃描檔名，不呼叫 LLM
  python -m scripts.ingest_all --limit 5    # 只處理 5 份報告
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.ingest import ingest_all

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="匯入券商報告")
    parser.add_argument("--scan-only", action="store_true", help="只掃描檔名，不執行 LLM 擷取")
    parser.add_argument("--limit", type=int, default=None, help="限制處理的報告數量")
    args = parser.parse_args()

    ingest_all(extract=not args.scan_only, limit=args.limit)
