"""智能檢索 — 支援結構化查詢 + 模糊搜尋 + 全文檢索"""
from datetime import date
from typing import Optional
from sqlalchemy import or_, text
from src.database import get_session, engine
from src.models import Report, FTS_TABLE_NAME


def search_reports(
    stock_code: Optional[str] = None,
    broker: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    keyword: Optional[str] = None,
    industry: Optional[str] = None,
    topic: Optional[str] = None,
    rating: Optional[str] = None,
) -> list[Report]:
    """結構化條件搜尋"""
    session = get_session()
    q = session.query(Report).filter(Report.extraction_status == "done")

    if stock_code:
        q = q.filter(Report.stock_code == stock_code)
    if broker:
        q = q.filter(Report.broker.contains(broker))
    if date_from:
        q = q.filter(Report.report_date >= date_from)
    if date_to:
        q = q.filter(Report.report_date <= date_to)
    if industry:
        q = q.filter(Report.industry.contains(industry))
    if topic:
        q = q.filter(Report.topics.contains(topic))
    if rating:
        q = q.filter(Report.rating == rating)
    if keyword:
        q = q.filter(or_(
            Report.stock_name.contains(keyword),
            Report.summary.contains(keyword),
            Report.investment_thesis.contains(keyword),
            Report.topics.contains(keyword),
            Report.broker.contains(keyword),
        ))

    results = q.order_by(Report.report_date.desc()).all()
    session.close()
    return results


def search_by_mentioned_stock(stock_code: str) -> list[Report]:
    """反查：哪些報告提到了某支股票"""
    session = get_session()
    results = (
        session.query(Report)
        .filter(Report.extraction_status == "done")
        .filter(Report.mentioned_stocks.contains(stock_code))
        .order_by(Report.report_date.desc())
        .all()
    )
    session.close()
    return results


def fulltext_search(query: str, limit: int = 50) -> list[Report]:
    """FTS5 全文檢索 — 支援前綴模糊匹配"""
    session = get_session()

    # 為每個 token 加上 * 做前綴匹配，例如 "光通" → "光通*"
    tokens = query.strip().split()
    fts_query = " ".join(f"{t}*" for t in tokens if t)
    if not fts_query:
        session.close()
        return []

    sql = text(f"""
        SELECT report_id FROM {FTS_TABLE_NAME}
        WHERE {FTS_TABLE_NAME} MATCH :query
        ORDER BY rank
        LIMIT :limit
    """)
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, {"query": fts_query, "limit": limit}).fetchall()
    except Exception:
        # FTS5 語法錯誤時 fallback 到原始查詢
        try:
            with engine.connect() as conn:
                rows = conn.execute(sql, {"query": query, "limit": limit}).fetchall()
        except Exception:
            session.close()
            return []

    report_ids = [row[0] for row in rows]
    if not report_ids:
        session.close()
        return []

    results = (
        session.query(Report)
        .filter(Report.id.in_(report_ids))
        .order_by(Report.report_date.desc())
        .all()
    )
    session.close()
    return results


def smart_search(query: str, limit: int = 50) -> list[Report]:
    """智能搜尋：先 topic → 再 industry → 再 keyword → 最後 FTS5，合併去重"""
    seen_ids = set()
    results = []

    def _add(reports):
        for r in reports:
            if r.id not in seen_ids:
                seen_ids.add(r.id)
                results.append(r)

    # 1. topics 精準匹配
    _add(search_reports(topic=query))

    # 2. industry 匹配
    _add(search_reports(industry=query))

    # 3. keyword 匹配 (summary, investment_thesis, stock_name, broker)
    _add(search_reports(keyword=query))

    # 4. FTS5 全文搜尋 (前綴模糊)
    if len(results) < limit:
        _add(fulltext_search(query, limit=limit - len(results)))

    return results[:limit]
