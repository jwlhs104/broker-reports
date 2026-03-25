"""從 tg-archiver 同步券商報告到 broker-reports 系統

流程：
1. 讀取 tg-archiver 的 SQLite DB（唯讀）
2. 找出尚未同步的文件訊息（PDF / DOCX）
3. 複製檔案到 broker-reports/reports/ 目錄
4. 呼叫既有的 ingest pipeline 進行解析
"""

import shutil
import sqlite3
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.config import CONFIG
from src.database import get_session, init_db
from src.models import Report

console = Console()

# 支援的文件類型
SUPPORTED_MIME_TYPES = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}


def _get_tg_db_path() -> Path:
    """取得 tg-archiver 的 DB 路徑"""
    tg_config = CONFIG.get("tg_archiver", {})
    db_path = Path(tg_config.get("db_path", ""))
    if not db_path.exists():
        raise FileNotFoundError(f"找不到 tg-archiver 資料庫: {db_path}")
    return db_path


def _get_tg_media_dir() -> Path:
    """取得 tg-archiver 的 media 目錄"""
    tg_config = CONFIG.get("tg_archiver", {})
    media_dir = Path(tg_config.get("media_dir", ""))
    if not media_dir.exists():
        raise FileNotFoundError(f"找不到 tg-archiver media 目錄: {media_dir}")
    return media_dir


def _get_synced_filenames() -> set[str]:
    """取得 broker-reports DB 中所有已存在的檔名"""
    session = get_session()
    try:
        filenames = {r.filename for r in session.query(Report.filename).all()}
        return filenames
    finally:
        session.close()


def _make_dest_filename(row: dict) -> str:
    """根據 tg-archiver 訊息產生目標檔名

    格式: tg_{chat_id}_{message_id}_{原始檔名尾部}
    確保唯一性且可追溯來源
    """
    chat_id = abs(row["chat_id"])  # chat_id 為負數，取絕對值
    message_id = row["message_id"]

    # 從 media_local_path 取得原始副檔名與檔名片段
    media_path = Path(row["media_local_path"])
    ext = media_path.suffix  # .pdf 或 .docx

    # 嘗試取得原始檔名中有意義的部分（去掉 Telegram file_id 前綴）
    stem = media_path.stem
    # tg-archiver 的檔名格式: {file_id}_{original_name}
    # file_id 是一長串 base64，找到第一個 _ 之後的部分
    parts = stem.split("_", 1)
    if len(parts) > 1:
        meaningful = parts[1]
        # 再去掉可能的第二段 file_id hash
        sub_parts = meaningful.split("_", 1)
        if len(sub_parts) > 1 and len(sub_parts[0]) > 30:
            meaningful = sub_parts[1]
    else:
        meaningful = stem

    # 截斷過長的檔名
    if len(meaningful) > 150:
        meaningful = meaningful[:150]

    return f"tg_{chat_id}_{message_id}_{meaningful}{ext}"


def fetch_new_documents(dry_run: bool = False) -> list[dict]:
    """從 tg-archiver 取得尚未同步的文件訊息

    Returns:
        list of dicts with keys: id, message_id, chat_id, chat_title,
        date, media_local_path, media_mime_type, caption, text
    """
    tg_db_path = _get_tg_db_path()
    tg_media_dir = _get_tg_media_dir()
    reports_dir = Path(CONFIG["paths"]["reports_dir"])

    # 取得 broker-reports 中已有的檔名
    init_db()
    synced = _get_synced_filenames()

    # 查詢 tg-archiver 中的文件訊息
    mime_filter = ", ".join(f"'{m}'" for m in SUPPORTED_MIME_TYPES)
    query = f"""
        SELECT id, message_id, chat_id, chat_title, date,
               media_local_path, media_mime_type, caption, text
        FROM messages
        WHERE message_type = 'document'
          AND media_mime_type IN ({mime_filter})
          AND media_local_path IS NOT NULL
        ORDER BY id ASC
    """

    conn = sqlite3.connect(str(tg_db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(query).fetchall()
    finally:
        conn.close()

    new_docs = []
    for row in rows:
        row_dict = dict(row)
        dest_filename = _make_dest_filename(row_dict)

        # 檢查是否已同步（檔名已存在於 broker-reports DB）
        if dest_filename in synced:
            continue

        # 檢查來源檔案是否存在
        src_path = tg_media_dir / row_dict["media_local_path"]
        if not src_path.exists():
            console.print(f"[yellow]  來源檔案不存在，跳過: {src_path}[/yellow]")
            continue

        row_dict["_src_path"] = str(src_path)
        row_dict["_dest_filename"] = dest_filename
        row_dict["_dest_path"] = str(reports_dir / dest_filename)
        new_docs.append(row_dict)

    return new_docs


def sync_documents(dry_run: bool = False, extract: bool = True, limit: int = None) -> int:
    """執行同步：複製新文件到 reports/ 並觸發 ingest

    Args:
        dry_run: 只列出待同步的文件，不實際複製
        extract: 同步後是否自動執行 LLM 擷取
        limit: 限制同步數量

    Returns:
        同步的文件數量
    """
    new_docs = fetch_new_documents()

    if not new_docs:
        console.print("[green]沒有新的文件需要同步[/green]")
        return 0

    if limit:
        new_docs = new_docs[:limit]

    # 印出待同步清單
    table = Table(title=f"待同步文件 ({len(new_docs)} 份)")
    table.add_column("#", style="dim")
    table.add_column("來源群組", style="cyan")
    table.add_column("日期", style="green")
    table.add_column("類型")
    table.add_column("目標檔名")
    table.add_column("說明", max_width=40)

    for i, doc in enumerate(new_docs, 1):
        ext = Path(doc["_dest_filename"]).suffix
        desc = doc.get("caption") or doc.get("text") or ""
        if len(desc) > 40:
            desc = desc[:37] + "..."
        table.add_row(
            str(i),
            doc["chat_title"],
            doc["date"][:10] if doc["date"] else "",
            ext,
            doc["_dest_filename"],
            desc,
        )

    console.print(table)

    if dry_run:
        console.print("[yellow]Dry run 模式，不執行實際同步[/yellow]")
        return 0

    # 執行複製
    reports_dir = Path(CONFIG["paths"]["reports_dir"])
    reports_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    for doc in new_docs:
        src = Path(doc["_src_path"])
        dest = Path(doc["_dest_path"])

        try:
            shutil.copy2(src, dest)
            copied += 1
            console.print(f"[green]  ✓ {doc['_dest_filename']}[/green]")
        except Exception as e:
            console.print(f"[red]  ✗ 複製失敗 {src.name}: {e}[/red]")

    console.print(f"\n[green]同步完成: 複製了 {copied} 份文件到 {reports_dir}[/green]")

    # 觸發 ingest pipeline
    if copied > 0:
        from src.ingest import run_extraction, scan_and_register

        console.print("\n[blue]註冊新檔案到資料庫...[/blue]")
        scan_and_register()

        if extract:
            console.print("[blue]開始執行 LLM 擷取...[/blue]")
            run_extraction(limit=copied)

    return copied


def show_sync_status():
    """顯示同步狀態統計"""
    tg_db_path = _get_tg_db_path()
    tg_media_dir = _get_tg_media_dir()

    # tg-archiver 側統計
    mime_filter = ", ".join(f"'{m}'" for m in SUPPORTED_MIME_TYPES)
    conn = sqlite3.connect(str(tg_db_path))
    try:
        total = conn.execute(f"""
            SELECT COUNT(*) FROM messages
            WHERE message_type = 'document'
              AND media_mime_type IN ({mime_filter})
              AND media_local_path IS NOT NULL
        """).fetchone()[0]

        by_group = conn.execute(f"""
            SELECT chat_title, COUNT(*) as cnt FROM messages
            WHERE message_type = 'document'
              AND media_mime_type IN ({mime_filter})
              AND media_local_path IS NOT NULL
            GROUP BY chat_title
            ORDER BY cnt DESC
        """).fetchall()

        by_type = conn.execute(f"""
            SELECT media_mime_type, COUNT(*) as cnt FROM messages
            WHERE message_type = 'document'
              AND media_mime_type IN ({mime_filter})
              AND media_local_path IS NOT NULL
            GROUP BY media_mime_type
        """).fetchall()
    finally:
        conn.close()

    # 未同步數量
    new_docs = fetch_new_documents()

    console.print("\n[bold]📊 同步狀態[/bold]")
    console.print(f"  tg-archiver 文件總數: [cyan]{total}[/cyan]")
    console.print(f"  已同步: [green]{total - len(new_docs)}[/green]")
    console.print(f"  待同步: [yellow]{len(new_docs)}[/yellow]")

    console.print("\n[bold]來源群組分佈:[/bold]")
    for title, cnt in by_group:
        console.print(f"  {title}: {cnt}")

    console.print("\n[bold]文件類型分佈:[/bold]")
    for mime, cnt in by_type:
        ext = SUPPORTED_MIME_TYPES.get(mime, mime)
        console.print(f"  {ext}: {cnt}")
