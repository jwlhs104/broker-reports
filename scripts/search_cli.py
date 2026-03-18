"""搜尋報告

用法:
  python -m scripts.search_cli 光通訊                    # 智能搜尋 (主題 → 產業 → 關鍵字 → 全文)
  python -m scripts.search_cli --stock 3081              # 依股票代號
  python -m scripts.search_cli --broker 凱基             # 依券商
  python -m scripts.search_cli --industry 光通訊         # 依產業
  python -m scripts.search_cli --topic CPO               # 依主題標籤
  python -m scripts.search_cli --rating 買進             # 依評等
  python -m scripts.search_cli --mentions 2330           # 反查：哪些報告提到台積電
  python -m scripts.search_cli 光通訊 --rating 買進      # 組合：智能搜尋 + 條件篩選
  python -m scripts.search_cli --from 2025-01-01 --to 2025-03-31  # 依日期區間
  python -m scripts.search_cli 光通訊 --from 2025-01-01            # 組合：智能搜尋 + 日期區間
"""
import argparse
import shutil
import sys
import os
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rich.console import Console
from rich.table import Table
from rich.prompt import Prompt
from src.database import init_db
from src.search import search_reports, search_by_mentioned_stock, smart_search

console = Console()


def _print_results(results: list, title: str):
    """顯示搜尋結果表格，帶行號"""
    if not results:
        console.print("[yellow]找不到符合條件的報告[/yellow]")
        return

    table = Table(title=f"{title} ({len(results)} 筆)")
    table.add_column("#", style="dim", justify="right")
    table.add_column("代號", style="cyan")
    table.add_column("名稱", style="white")
    table.add_column("日期", style="green")
    table.add_column("券商", style="magenta")
    table.add_column("產業", style="blue")
    table.add_column("評等", style="bold")
    table.add_column("目標價", justify="right")
    table.add_column("投資邏輯", max_width=40)

    for i, r in enumerate(results, 1):
        rating = r.rating or "-"
        if rating == "買進":
            rating = f"[green]{rating}[/green]"
        elif rating == "賣出":
            rating = f"[red]{rating}[/red]"

        table.add_row(
            str(i),
            r.stock_code or "-",
            r.stock_name or "-",
            str(r.report_date) if r.report_date else "-",
            r.broker or "-",
            r.industry or "-",
            rating,
            f"{r.target_price:.1f}" if r.target_price else "-",
            (r.investment_thesis[:40] + "...") if r.investment_thesis and len(r.investment_thesis) > 40 else (r.investment_thesis or "-"),
        )

    console.print(table)


def _parse_selection(selection: str, total: int) -> list[int]:
    """解析使用者的選擇輸入，回傳 0-based index 列表

    支援格式: all, 1,3,5, 1-5, 1-3,7,9-11
    """
    selection = selection.strip().lower()
    if selection in ("all", "a", "全部"):
        return list(range(total))

    indices = []
    for part in selection.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                start, end = part.split("-", 1)
                start, end = int(start.strip()), int(end.strip())
                indices.extend(range(start - 1, min(end, total)))
            except ValueError:
                continue
        else:
            try:
                idx = int(part) - 1
                if 0 <= idx < total:
                    indices.append(idx)
            except ValueError:
                continue

    return sorted(set(indices))


def _copy_reports(results: list):
    """互動式選擇並複製報告到指定資料夾"""
    console.print()
    selection = Prompt.ask(
        "[bold]選擇要匯出的報告[/bold] (編號如 1,3,5 或 1-5 或 all，直接 Enter 跳過)"
    )
    if not selection:
        return

    indices = _parse_selection(selection, len(results))
    if not indices:
        console.print("[yellow]沒有有效的選擇[/yellow]")
        return

    selected = [results[i] for i in indices]
    console.print(f"已選擇 [cyan]{len(selected)}[/cyan] 份報告")

    dest = Prompt.ask("[bold]目標資料夾路徑[/bold]", default="./export")
    dest_path = Path(dest).resolve()

    # 若資料夾已存在，先清空舊檔案
    if dest_path.exists():
        old_files = list(dest_path.glob("*.pdf"))
        if old_files:
            console.print(f"[yellow]清空 {dest_path} 中的 {len(old_files)} 個舊 PDF...[/yellow]")
            for f in old_files:
                f.unlink()

    dest_path.mkdir(parents=True, exist_ok=True)

    copied = 0
    for r in selected:
        src = Path(r.file_path)
        if not src.exists():
            console.print(f"[yellow]⚠ 找不到檔案: {r.filename}[/yellow]")
            continue
        dst = dest_path / src.name
        shutil.copy2(src, dst)
        copied += 1

    console.print(f"[green]✓ 已複製 {copied} 份報告到 {dest_path}[/green]")


def main():
    parser = argparse.ArgumentParser(description="搜尋券商報告")
    parser.add_argument("query", nargs="?", help="智能搜尋關鍵字 (搜主題/產業/全文)")
    parser.add_argument("--stock", help="股票代號")
    parser.add_argument("--broker", help="券商名稱")
    parser.add_argument("--industry", help="產業分類")
    parser.add_argument("--topic", help="主題標籤")
    parser.add_argument("--rating", help="投資評等 (買進/持有/賣出)")
    parser.add_argument("--mentions", help="反查：哪些報告提到此股票代碼")
    parser.add_argument("--from", dest="date_from", help="起始日期 (YYYY-MM-DD)")
    parser.add_argument("--to", dest="date_to", help="結束日期 (YYYY-MM-DD)")
    parser.add_argument("--limit", type=int, default=50, help="最多顯示幾筆 (預設 50)")
    args = parser.parse_args()

    init_db()
    results = None

    # 解析日期區間
    d_from = date.fromisoformat(args.date_from) if args.date_from else None
    d_to = date.fromisoformat(args.date_to) if args.date_to else None

    # 反查模式
    if args.mentions:
        results = search_by_mentioned_stock(args.mentions)
        _print_results(results, f"提及 {args.mentions} 的報告")

    # 有結構化條件時用結構化搜尋
    elif any([args.stock, args.broker, args.industry, args.topic, args.rating, d_from, d_to]) and not args.query:
        results = search_reports(
            stock_code=args.stock,
            broker=args.broker,
            industry=args.industry,
            topic=args.topic,
            rating=args.rating,
            date_from=d_from,
            date_to=d_to,
        )
        results = results[:args.limit]
        _print_results(results, "搜尋結果")

    elif args.query:
        # 智能搜尋
        results = smart_search(args.query, limit=args.limit)
        # 二次過濾
        has_filters = any([args.stock, args.broker, args.industry, args.rating, d_from, d_to])
        if has_filters:
            filtered = []
            for r in results:
                if args.stock and r.stock_code != args.stock:
                    continue
                if args.broker and args.broker not in (r.broker or ""):
                    continue
                if args.industry and args.industry not in (r.industry or ""):
                    continue
                if args.rating and r.rating != args.rating:
                    continue
                if d_from and (not r.report_date or r.report_date < d_from):
                    continue
                if d_to and (not r.report_date or r.report_date > d_to):
                    continue
                filtered.append(r)
            results = filtered
        _print_results(results, f"「{args.query}」搜尋結果")

    else:
        parser.print_help()
        return

    # 第二步：選擇並匯出
    if results:
        _copy_reports(results)


if __name__ == "__main__":
    main()
