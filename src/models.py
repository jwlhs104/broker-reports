"""SQLAlchemy ORM 模型"""
import json
from datetime import datetime, date
from sqlalchemy import Column, Integer, String, Float, Date, DateTime, Text, event
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Report(Base):
    __tablename__ = "reports"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # 基本資訊 (初始從檔名解析，LLM 擷取後以 AI 結果覆蓋)
    stock_code = Column(String(10), nullable=True, index=True)
    broker = Column(String(50), nullable=True)
    report_date = Column(Date, nullable=True, index=True)
    filename = Column(String(255), nullable=False, unique=True)
    file_path = Column(String(500), nullable=False)

    # 從 LLM 擷取 — 基本資訊
    stock_name = Column(String(50), nullable=True)
    rating = Column(String(20), nullable=True)
    target_price = Column(Float, nullable=True)
    current_price = Column(Float, nullable=True)
    summary = Column(Text, nullable=True)

    # 從 LLM 擷取 — 智能檢索欄位
    industry = Column(String(50), nullable=True, index=True)       # 主產業分類
    topics = Column(Text, nullable=True)                           # JSON: 主題標籤列表
    mentioned_stocks = Column(Text, nullable=True)                 # JSON: 提及的其他股票代碼
    investment_thesis = Column(Text, nullable=True)                # 核心投資邏輯

    # 從 LLM 擷取 — 報告品質評分
    quality_score = Column(Integer, nullable=True)                 # 品質分數 1-10
    quality_reason = Column(Text, nullable=True)                   # 給分理由

    # 全文檢索
    raw_text = Column(Text, nullable=True)                         # PDF 全文 (供 FTS5)

    # 中繼資料
    extraction_status = Column(String(20), default="pending")  # pending / done / failed
    page_count = Column(Integer, nullable=True)
    raw_text_length = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def topics_list(self) -> list[str]:
        """取得 topics 為 Python list"""
        if not self.topics:
            return []
        return json.loads(self.topics)

    @topics_list.setter
    def topics_list(self, value: list[str]):
        self.topics = json.dumps(value, ensure_ascii=False)

    @property
    def mentioned_stocks_list(self) -> list[str]:
        """取得 mentioned_stocks 為 Python list"""
        if not self.mentioned_stocks:
            return []
        return json.loads(self.mentioned_stocks)

    @mentioned_stocks_list.setter
    def mentioned_stocks_list(self, value: list[str]):
        self.mentioned_stocks = json.dumps(value, ensure_ascii=False)

    def __repr__(self):
        return f"<Report {self.stock_code} {self.report_date} {self.broker}>"


# ── FTS5 虛擬表 (SQLite 全文搜尋) ────────────────────────────

FTS_TABLE_NAME = "reports_fts"

CREATE_FTS_TABLE = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE_NAME}
USING fts5(
    report_id UNINDEXED,
    stock_name,
    summary,
    investment_thesis,
    topics,
    raw_text,
    tokenize='unicode61'
);
"""

REBUILD_FTS_ENTRY = f"""
INSERT OR REPLACE INTO {FTS_TABLE_NAME}(rowid, report_id, stock_name, summary, investment_thesis, topics, raw_text)
VALUES (:rowid, :report_id, :stock_name, :summary, :investment_thesis, :topics, :raw_text);
"""
