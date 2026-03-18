"""從檔名解析股票代號、日期、券商"""
import re
from datetime import date
from pathlib import Path
from dataclasses import dataclass
from typing import Optional


@dataclass
class FilenameMeta:
    stock_code: Optional[str]
    report_date: Optional[date]
    broker: Optional[str]
    filename: str


def parse_filename(filepath: str) -> FilenameMeta:
    """解析檔名格式: {stock_code}_{YYYYMMDD}_{broker}.pdf"""
    filename = Path(filepath).stem  # 去掉 .pdf
    parts = filename.split("_")

    stock_code = None
    report_date = None
    broker = None

    if len(parts) >= 3:
        stock_code = parts[0]
        try:
            d = parts[1]
            report_date = date(int(d[:4]), int(d[4:6]), int(d[6:8]))
        except (ValueError, IndexError):
            pass
        broker = "_".join(parts[2:])  # 處理券商名稱含底線的情況
    elif len(parts) == 2:
        stock_code = parts[0]
        # 第二部分可能是日期或券商
        try:
            d = parts[1]
            report_date = date(int(d[:4]), int(d[4:6]), int(d[6:8]))
        except (ValueError, IndexError):
            broker = parts[1]

    return FilenameMeta(
        stock_code=stock_code,
        report_date=report_date,
        broker=broker,
        filename=Path(filepath).name,
    )
