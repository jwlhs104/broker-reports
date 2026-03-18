"""資料庫引擎與 session 管理"""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from src.config import CONFIG
from src.models import Base, CREATE_FTS_TABLE

engine = create_engine(CONFIG["paths"]["db_url"], echo=False)
SessionLocal = sessionmaker(bind=engine)


def init_db():
    """建立所有資料表 (含 FTS5 虛擬表)"""
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(text(CREATE_FTS_TABLE))
        conn.commit()


def get_session():
    """取得 DB session"""
    return SessionLocal()
