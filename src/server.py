"""券商報告 MCP Server — 查詢專用"""

from datetime import date

from mcp.server.fastmcp import FastMCP

from src.database import init_db
from src.search import search_by_mentioned_stock, search_reports, smart_search

init_db()

mcp = FastMCP(
    "broker-reports",
    instructions="台灣券商研究報告查詢系統。可搜尋 8000+ 份券商報告的評等、目標價、產業分析、投資邏輯等結構化資訊。",
    host="0.0.0.0",
    port=8100,
)


def _format_report(r) -> dict:
    """將 Report ORM 物件轉為可序列化的 dict"""
    return {
        "report_id": r.id,
        "stock_code": r.stock_code,
        "stock_name": r.stock_name,
        "report_date": str(r.report_date) if r.report_date else None,
        "broker": r.broker,
        "industry": r.industry,
        "rating": r.rating,
        "target_price": r.target_price,
        "current_price": r.current_price,
        "summary": r.summary,
        "investment_thesis": r.investment_thesis,
        "topics": r.topics_list,
        "quality_score": r.quality_score,
    }


def _format_results(results: list, title: str = "") -> str:
    """格式化搜尋結果為可讀文字"""
    if not results:
        return "找不到符合條件的報告。"

    lines = [f"共找到 {len(results)} 筆結果" + (f" — {title}" if title else ""), ""]
    for i, r in enumerate(results, 1):
        tp = f"目標價 {r.target_price}" if r.target_price else "無目標價"
        lines.append(
            f"{i}. [ID:{r.id}] [{r.stock_code or '?'}] {r.stock_name or '?'} | "
            f"{r.report_date} | {r.broker or '?'} | "
            f"{r.rating or '未評等'} | {tp}"
        )
        if r.investment_thesis:
            lines.append(f"   💡 {r.investment_thesis}")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
def search_broker_reports(
    query: str | None = None,
    stock_code: str | None = None,
    broker: str | None = None,
    industry: str | None = None,
    topic: str | None = None,
    rating: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 20,
) -> str:
    """搜尋券商研究報告。

    支援兩種模式:
    1. 智能搜尋: 提供 query 參數，自動搜尋主題、產業、關鍵字、全文
    2. 結構化搜尋: 用 stock_code, broker, industry, topic, rating, date_from/to 精確篩選

    兩種模式可以組合使用。

    Args:
        query: 智能搜尋關鍵字 (例: "光通訊", "AI伺服器", "台積電")
        stock_code: 股票代號 (例: "2330", "3081")
        broker: 券商名稱 (例: "凱基", "元大")
        industry: 產業分類 (例: "光通訊", "半導體")
        topic: 主題標籤 (例: "CPO", "HBM", "CoWoS")
        rating: 投資評等 ("買進", "持有", "賣出")
        date_from: 起始日期 YYYY-MM-DD
        date_to: 結束日期 YYYY-MM-DD
        limit: 最多回傳筆數，預設 20
    """
    d_from = date.fromisoformat(date_from) if date_from else None
    d_to = date.fromisoformat(date_to) if date_to else None

    if query:
        results = smart_search(query, limit=limit)
        # 二次過濾
        if any([stock_code, broker, industry, rating, d_from, d_to]):
            filtered = []
            for r in results:
                if stock_code and r.stock_code != stock_code:
                    continue
                if broker and broker not in (r.broker or ""):
                    continue
                if industry and industry not in (r.industry or ""):
                    continue
                if rating and r.rating != rating:
                    continue
                if d_from and (not r.report_date or r.report_date < d_from):
                    continue
                if d_to and (not r.report_date or r.report_date > d_to):
                    continue
                filtered.append(r)
            results = filtered
    else:
        results = search_reports(
            stock_code=stock_code,
            broker=broker,
            date_from=d_from,
            date_to=d_to,
            industry=industry,
            topic=topic,
            rating=rating,
        )
        results = results[:limit]

    return _format_results(results, query or "結構化搜尋")


@mcp.tool()
def get_report_detail(report_id: int) -> str:
    """取得單份報告的完整資訊，包含結構化摘要與原始 PDF 全文。

    Args:
        report_id: 報告 ID (從 search_broker_reports 結果取得)
    """
    import os

    from src.database import get_session
    from src.models import Report

    session = get_session()
    report = session.query(Report).filter_by(id=report_id).first()
    session.close()

    if not report:
        return f"找不到 report_id={report_id} 的報告。"

    data = _format_report(report)
    lines = [
        f"📄 {data['stock_code']} {data['stock_name']} — {data['broker']}",
        f"日期: {data['report_date']}",
        f"產業: {data['industry']}",
        f"評等: {data['rating']} | 目標價: {data['target_price']} | 現價: {data['current_price']}",
        f"品質: {data['quality_score']}/10",
        f"檔案: {report.filename}",
        f"路徑: {report.file_path}",
        "",
        f"📝 摘要:\n{data['summary']}",
        "",
        f"💡 投資邏輯:\n{data['investment_thesis']}",
        "",
        f"🏷️ 主題: {', '.join(data['topics'])}",
    ]

    # 嘗試取得原始全文
    full_text = None

    # 優先使用 DB 中已存的 raw_text
    if report.raw_text:
        full_text = report.raw_text
    else:
        # 嘗試從原始 PDF 讀取
        file_path = report.file_path
        if file_path and os.path.exists(file_path):
            try:
                from src.pdf_parser import extract_text

                full_text, _ = extract_text(file_path)
            except Exception as e:
                lines.append(f"\n⚠️ 無法讀取原始 PDF: {e}")

    if full_text:
        lines.append("\n" + "=" * 60)
        lines.append("📖 原始報告全文:")
        lines.append("=" * 60)
        lines.append(full_text)

    return "\n".join(lines)


@mcp.tool()
def compare_target_prices(stock_code: str) -> str:
    """彙總各券商對同一檔股票的最新評等與目標價，方便橫向比較。

    Args:
        stock_code: 股票代號 (例: "2330")
    """
    results = search_reports(stock_code=stock_code)
    if not results:
        return f"找不到股票 {stock_code} 的相關報告。"

    # 每個券商只取最新一份
    latest_by_broker = {}
    for r in results:
        broker = r.broker or "未知"
        if broker not in latest_by_broker:
            latest_by_broker[broker] = r

    stock_name = results[0].stock_name or stock_code
    lines = [
        f"📊 {stock_code} {stock_name} — 各券商最新觀點 ({len(latest_by_broker)} 家)",
        "",
    ]

    for broker, r in sorted(latest_by_broker.items()):
        tp = f"目標價 {r.target_price}" if r.target_price else "無目標價"
        lines.append(
            f"  {broker:　<6} | {r.rating or '未評等':　<3} | {tp} | {r.report_date}"
        )
        if r.investment_thesis:
            lines.append(f"  {'':　<6}   💡 {r.investment_thesis}")
        lines.append("")

    # 統計
    prices = [r.target_price for r in latest_by_broker.values() if r.target_price]
    if prices:
        lines.append(
            f"目標價範圍: {min(prices):.1f} ~ {max(prices):.1f} (均值 {sum(prices) / len(prices):.1f})"
        )

    ratings = [r.rating for r in latest_by_broker.values() if r.rating]
    if ratings:
        from collections import Counter

        rc = Counter(ratings)
        lines.append(
            f"評等分佈: {', '.join(f'{k} {v}家' for k, v in rc.most_common())}"
        )

    return "\n".join(lines)


@mcp.tool()
def find_related_reports(stock_code: str, limit: int = 20) -> str:
    """反向查詢：找出所有「提及」某檔股票的報告（報告主角不是該股票，但內文有提到）。
    適合用來發現產業鏈上下游關聯。

    Args:
        stock_code: 股票代號 (例: "2330")
        limit: 最多回傳筆數
    """
    results = search_by_mentioned_stock(stock_code)[:limit]
    return _format_results(results, f"提及 {stock_code} 的報告")


@mcp.tool()
def get_stats() -> str:
    """取得報告資料庫的統計概覽：總數、產業分佈、券商分佈、評等分佈等。"""
    from sqlalchemy import func

    from src.database import get_session
    from src.models import Report

    session = get_session()
    total = (
        session.query(func.count(Report.id))
        .filter_by(extraction_status="done")
        .scalar()
    )
    pending = (
        session.query(func.count(Report.id))
        .filter_by(extraction_status="pending")
        .scalar()
    )
    failed = (
        session.query(func.count(Report.id))
        .filter_by(extraction_status="failed")
        .scalar()
    )

    # 產業 top 10
    industry_counts = (
        session.query(Report.industry, func.count(Report.id))
        .filter(Report.extraction_status == "done", Report.industry.isnot(None))
        .group_by(Report.industry)
        .order_by(func.count(Report.id).desc())
        .limit(10)
        .all()
    )

    # 券商 top 10
    broker_counts = (
        session.query(Report.broker, func.count(Report.id))
        .filter(Report.extraction_status == "done", Report.broker.isnot(None))
        .group_by(Report.broker)
        .order_by(func.count(Report.id).desc())
        .limit(10)
        .all()
    )

    # 評等分佈
    rating_counts = (
        session.query(Report.rating, func.count(Report.id))
        .filter(Report.extraction_status == "done", Report.rating.isnot(None))
        .group_by(Report.rating)
        .all()
    )

    session.close()

    lines = [
        "📊 報告資料庫統計",
        f"已完成: {total} | 待處理: {pending} | 失敗: {failed}",
        "",
        "🏭 產業 Top 10:",
    ]
    for ind, cnt in industry_counts:
        lines.append(f"  {ind}: {cnt} 份")

    lines.append("\n🏢 券商 Top 10:")
    for broker, cnt in broker_counts:
        lines.append(f"  {broker}: {cnt} 份")

    lines.append("\n⭐ 評等分佈:")
    for rating, cnt in rating_counts:
        lines.append(f"  {rating}: {cnt} 份")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="券商報告 MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="傳輸模式: stdio (本地, 預設) 或 http (網路服務)",
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="HTTP 綁定位址 (預設 0.0.0.0)"
    )
    parser.add_argument("--port", type=int, default=8100, help="HTTP 埠號 (預設 8100)")
    args = parser.parse_args()

    if args.transport == "http":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
