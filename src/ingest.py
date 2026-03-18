"""報告匯入流程：掃描 → 解析檔名 → 擷取文字 → LLM 結構化 → 建立 FTS 索引"""
import json
import os
from datetime import date
from pathlib import Path
from rich.console import Console
from rich.progress import track
from sqlalchemy import text

from src.config import CONFIG
from src.database import init_db, get_session, engine
from src.models import Report, FTS_TABLE_NAME, REBUILD_FTS_ENTRY
from src.filename_parser import parse_filename
from src.pdf_parser import extract_text
from src.extractor import extract_report_data

console = Console()


def scan_and_register():
    """Phase 1: 掃描 reports 目錄，將新報告註冊到 DB"""
    init_db()
    session = get_session()
    reports_dir = CONFIG["paths"]["reports_dir"]
    pdf_files = list(Path(reports_dir).glob("*.pdf"))

    new_count = 0
    for pdf_path in pdf_files:
        existing = session.query(Report).filter_by(filename=pdf_path.name).first()
        if existing:
            continue

        meta = parse_filename(str(pdf_path))
        report = Report(
            stock_code=meta.stock_code,  # 檔名初步解析，LLM 擷取後會覆蓋
            broker=meta.broker,          # 檔名初步解析，LLM 擷取後會覆蓋
            report_date=meta.report_date,
            filename=meta.filename,
            file_path=str(pdf_path),
            extraction_status="pending",
        )
        session.add(report)
        new_count += 1

    session.commit()
    session.close()
    console.print(f"[green]掃描完成: 發現 {len(pdf_files)} 份 PDF, 新增 {new_count} 份到資料庫[/green]")
    return new_count


def _update_fts(report: Report):
    """將報告資料寫入 FTS5 索引"""
    with engine.connect() as conn:
        conn.execute(text(REBUILD_FTS_ENTRY), {
            "rowid": report.id,
            "report_id": report.id,
            "stock_name": report.stock_name or "",
            "summary": report.summary or "",
            "investment_thesis": report.investment_thesis or "",
            "topics": report.topics or "",
            "raw_text": report.raw_text or "",
        })
        conn.commit()


def run_extraction(limit: int = None):
    """Phase 2: 對 pending 報告執行 LLM 結構化擷取"""
    session = get_session()
    q = session.query(Report).filter_by(extraction_status="pending")
    if limit:
        q = q.limit(limit)
    pending = q.all()

    if not pending:
        console.print("[yellow]沒有待處理的報告[/yellow]")
        return

    max_pages = CONFIG["extraction"]["max_pages"]
    console.print(f"[blue]開始擷取 {len(pending)} 份報告...[/blue]")

    for report in track(pending, description="擷取中..."):
        try:
            # 1. 擷取 PDF 文字
            full_text, page_count = extract_text(report.file_path, max_pages=max_pages)
            report.page_count = page_count
            report.raw_text_length = len(full_text)

            if len(full_text) < 50:
                console.print(f"[yellow]⚠ {report.filename}: 文字太少 (可能是掃描檔)[/yellow]")
                report.extraction_status = "failed"
                session.commit()
                continue

            # 2. LLM 擷取結構化資料
            data = extract_report_data(full_text)

            # AI 判斷的代號、券商、日期優先於檔名解析
            if data.get("stock_code"):
                report.stock_code = data["stock_code"]
            if data.get("broker"):
                report.broker = data["broker"]
            if data.get("report_date"):
                try:
                    report.report_date = date.fromisoformat(data["report_date"])
                except (ValueError, TypeError):
                    pass  # 保留檔名解析的日期

            report.stock_name = data.get("stock_name")
            report.rating = data.get("rating")
            report.target_price = data.get("target_price")
            report.current_price = data.get("current_price")
            report.summary = data.get("summary")
            report.industry = data.get("industry")
            report.investment_thesis = data.get("investment_thesis")
            report.quality_score = data.get("quality_score")
            report.quality_reason = data.get("quality_reason")

            # JSON 欄位
            topics = data.get("topics", [])
            report.topics = json.dumps(topics, ensure_ascii=False) if topics else None

            mentioned = data.get("mentioned_stocks", [])
            report.mentioned_stocks = json.dumps(mentioned, ensure_ascii=False) if mentioned else None

            # 3. 儲存全文供 FTS
            report.raw_text = full_text

            report.extraction_status = "done"
            session.commit()

            # 4. 更新 FTS5 索引
            _update_fts(report)

            console.print(
                f"[green]✓ {report.filename}: "
                f"{report.stock_name} | {report.rating} | "
                f"{report.industry} | "
                f"目標價 {report.target_price}[/green]"
            )

        except Exception as e:
            console.print(f"[red]✗ {report.filename}: {e}[/red]")
            report.extraction_status = "failed"
            session.commit()

    session.close()


def ingest_all(extract: bool = True, limit: int = None):
    """完整匯入流程"""
    scan_and_register()
    if extract:
        run_extraction(limit=limit)
