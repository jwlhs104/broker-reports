"""Broker Reports — NotebookLM-style Web UI"""

import os
import sys

# 讓 src/ 可 import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import json
import logging
import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from src.database import get_session
from src.models import Report
from src.search import fulltext_search, search_reports, smart_search
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

app = FastAPI(title="Broker Reports UI")


# ── Middleware: 確保 PDF 可在 iframe 中嵌入（Cloudflare tunnel 相容）──
class PdfEmbedMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        # 移除可能阻擋 iframe 嵌入的標頭
        if request.url.path.endswith("/pdf") or response.headers.get("content-type", "").startswith("application/pdf"):
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
            response.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
            response.headers["Content-Disposition"] = "inline"
        return response


app.add_middleware(PdfEmbedMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

WEB_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
templates = Jinja2Templates(directory=WEB_DIR / "templates")

# ── LLM 後端選擇 ──
# 模式 1: Claude CLI（訂閱制，不需要 API key）→ LLM_BACKEND=claude-cli
# 模式 2: OpenAI-compatible API（需要 API key）→ LLM_BACKEND=openai
LLM_BACKEND = os.environ.get("LLM_BACKEND", "claude-cli")

llm = None
MODEL = ""

if LLM_BACKEND == "openai":
    from openai import OpenAI

    llm = OpenAI(
        base_url=os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1"),
        api_key=os.environ.get("LLM_API_KEY", ""),
    )
    MODEL = os.environ.get("LLM_MODEL", "anthropic/claude-sonnet-4")


async def call_llm(system_prompt: str, messages: list[dict]) -> str:
    """統一 LLM 呼叫介面"""
    if LLM_BACKEND == "openai" and llm:
        full_messages = [{"role": "system", "content": system_prompt}] + messages
        response = llm.chat.completions.create(
            model=MODEL,
            max_tokens=4096,
            messages=full_messages,
        )
        return response.choices[0].message.content
    else:
        # Claude CLI 模式：用訂閱制，透過 subprocess 呼叫
        # 把 system prompt 和 user message 組合成一個 prompt
        user_msg = messages[-1]["content"] if messages else ""
        full_prompt = f"{system_prompt}\n\n{user_msg}"

        # claude -p "prompt" --model sonnet 直接輸出結果
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "-p",
            full_prompt,
            "--model",
            "sonnet",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "NO_COLOR": "1"},
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

        if proc.returncode != 0:
            err = stderr.decode().strip()
            logger.error(f"Claude CLI error: {err}")
            raise RuntimeError(f"Claude CLI failed: {err}")

        return stdout.decode().strip()


# ── Pydantic models ──


class ChatRequest(BaseModel):
    question: str
    stock_code: str | None = None
    history: list[dict] = []  # [{role, content}]


class Source(BaseModel):
    id: int
    report_id: int
    broker: str
    date: str
    stock_code: str
    stock_name: str
    rating: str | None = None
    target_price: float | None = None
    summary: str | None = None
    excerpt: str  # 被引用的原文段落


class ChatResponse(BaseModel):
    answer: str
    sources: list[Source]


# ── Helper: Report → dict ──


def report_to_dict(r: Report) -> dict:
    return {
        "id": r.id,
        "stock_code": r.stock_code or "",
        "stock_name": r.stock_name or "",
        "broker": r.broker or "",
        "date": r.report_date.isoformat() if r.report_date else "",
        "rating": r.rating,
        "target_price": r.target_price,
        "summary": r.summary or "",
        "investment_thesis": r.investment_thesis or "",
        "topics": r.topics_list,
        "quality_score": r.quality_score,
        "raw_text": r.raw_text or "",
    }


# ── Routes ──


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/search")
async def api_search(
    q: str = "",
    stock_code: str = "",
    broker: str = "",
    limit: int = 20,
):
    """搜尋報告"""
    if stock_code:
        results = search_reports(stock_code=stock_code, broker=broker or None)
    elif q:
        results = smart_search(q, limit=limit)
    else:
        results = search_reports()

    return [
        {
            "id": r.id,
            "stock_code": r.stock_code,
            "stock_name": r.stock_name,
            "broker": r.broker,
            "date": r.report_date.isoformat() if r.report_date else "",
            "rating": r.rating,
            "target_price": r.target_price,
            "summary": r.summary,
            "topics": r.topics_list,
            "quality_score": r.quality_score,
        }
        for r in results[:limit]
    ]


@app.get("/api/report/{report_id}")
async def api_report_detail(report_id: int):
    """取得報告 metadata + 原文（hover preview 用）"""
    session = get_session()
    r = session.query(Report).filter(Report.id == report_id).first()
    session.close()
    if not r:
        return {"error": "not found"}
    d = report_to_dict(r)
    # 加上是否有 PDF 可預覽
    fp = Path(r.file_path) if r.file_path else None
    d["has_pdf"] = fp is not None and fp.exists() and fp.suffix.lower() == ".pdf"
    return d


@app.get("/api/report/{report_id}/pdf")
async def api_report_pdf(report_id: int):
    """提供原始 PDF 檔案供瀏覽器內嵌預覽"""
    session = get_session()
    r = session.query(Report).filter(Report.id == report_id).first()
    session.close()
    if not r or not r.file_path:
        return {"error": "not found"}
    fp = Path(r.file_path)
    if not fp.exists():
        return {"error": "file not found"}
    return FileResponse(
        fp,
        media_type="application/pdf",
        filename=fp.name,
        headers={"Content-Disposition": "inline"},
    )


def _build_stock_name_map() -> dict[str, str]:
    """建立 stock_name → stock_code 對照表（從已完成的報告中取得）。"""
    session = get_session()
    rows = (
        session.query(Report.stock_code, Report.stock_name)
        .filter(Report.extraction_status == "done")
        .filter(Report.stock_code.isnot(None))
        .filter(Report.stock_name.isnot(None))
        .distinct()
        .all()
    )
    session.close()
    return {name: code for code, name in rows if name and code}


# 在啟動時建立一次，避免每次請求都查 DB
_STOCK_NAME_MAP: dict[str, str] | None = None


def _get_stock_name_map() -> dict[str, str]:
    global _STOCK_NAME_MAP
    if _STOCK_NAME_MAP is None:
        _STOCK_NAME_MAP = _build_stock_name_map()
    return _STOCK_NAME_MAP


def extract_search_terms(question: str) -> dict:
    """從自然語言問句提取搜尋條件。

    回傳 {"stock_codes": [...], "stock_names": [...], "keywords": [...]}
    """
    result = {"stock_codes": [], "stock_names": [], "keywords": []}

    # 1. 提取股票代碼（4 位數字，不要求 word boundary，因為中文字旁沒有 \b）
    #    用 negative lookaround 確保不是更長數字的一部分
    codes = re.findall(r"(?<!\d)(\d{4})(?!\d)", question)
    result["stock_codes"] = list(set(codes))

    # 2. 提取已知股票名稱（從 DB 中的股票名對照）
    name_map = _get_stock_name_map()
    for name, code in name_map.items():
        if name in question and code not in result["stock_codes"]:
            result["stock_codes"].append(code)
            result["stock_names"].append(name)

    # 3. 提取關鍵字：去掉停用詞，保留有意義的詞
    stopwords = {
        "目前",
        "各家",
        "券商",
        "看法",
        "觀點",
        "報告",
        "分析",
        "重點",
        "什麼",
        "哪些",
        "怎麼",
        "如何",
        "請",
        "幫",
        "我",
        "的",
        "有",
        "最新",
        "一下",
        "可以",
        "比較",
        "關於",
        "想",
        "知道",
        "了解",
        "這個",
        "那個",
        "是否",
        "是不是",
        "為什麼",
        "以及",
        "和",
        "與",
    }
    # 切成 2-4 字的 ngram 作為關鍵字
    clean = re.sub(r"\d{4}", "", question)  # 移除股票代碼
    for name in result["stock_names"]:
        clean = clean.replace(name, "")
    # 簡單按照中文常用斷詞：用標點和停用詞分割
    segments = re.split(r"[，。？！、\s]+", clean)
    for seg in segments:
        seg = seg.strip()
        if len(seg) >= 2 and seg not in stopwords:
            result["keywords"].append(seg)

    return result


def search_for_chat(question: str, stock_code: str | None = None, limit: int = 10) -> list[Report]:
    """多路搜尋：從問句提取條件，合併多種搜尋結果。

    多支股票時均勻分配配額，確保每支都有足夠的報告被送入 LLM context。
    """
    seen_ids = set()
    results = []

    def _add(reports, max_count: int = 0):
        """加入報告，max_count=0 表示不限。"""
        added = 0
        for r in reports:
            if r.id not in seen_ids:
                seen_ids.add(r.id)
                results.append(r)
                added += 1
                if max_count and added >= max_count:
                    break

    # 如果使用者手動指定了 stock_code filter
    if stock_code:
        _add(search_reports(stock_code=stock_code))
        if len(results) >= limit:
            return results[:limit]

    # 從問句提取條件
    terms = extract_search_terms(question)
    stock_codes = terms["stock_codes"]

    # 1. 用提取到的股票代碼搜尋（均勻分配配額）
    if stock_codes:
        per_stock = max(3, limit // len(stock_codes))  # 每支至少 3 筆
        for code in stock_codes:
            _add(search_reports(stock_code=code), max_count=per_stock)

    # 2. 用關鍵字做 smart_search（補足剩餘配額）
    for kw in terms["keywords"]:
        if len(results) >= limit:
            break
        _add(smart_search(kw, limit=limit - len(results)))

    # 3. 如果以上都沒結果，用原始問句做 FTS 全文搜尋
    if not results:
        _add(fulltext_search(question, limit=limit))

    # 4. 還是沒有就用整個問句做 smart_search
    if not results:
        _add(smart_search(question, limit=limit))

    return results[:limit]


@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    """RAG Chat — 搜尋相關報告 → Claude 回答並標註 source"""

    # Step 1: 多路搜尋相關報告
    reports = search_for_chat(req.question, req.stock_code, limit=10)

    if not reports:
        return ChatResponse(
            answer="找不到相關的券商報告。請嘗試其他關鍵字或股票代碼。",
            sources=[],
        )

    # 限制最多 8 份報告送入 context
    reports = reports[:8]

    # Step 2: 組裝 context
    context_parts = []
    for i, r in enumerate(reports, 1):
        raw = (r.raw_text or "")[:3000]  # 每份報告截取前 3000 字
        context_parts.append(
            f"[報告 {i}] ID={r.id}\n"
            f"券商: {r.broker} | 日期: {r.report_date} | 股票: {r.stock_code} {r.stock_name}\n"
            f"評等: {r.rating} | 目標價: {r.target_price}\n"
            f"摘要: {r.summary}\n"
            f"投資邏輯: {r.investment_thesis}\n"
            f"原文:\n{raw}\n"
        )

    context = "\n---\n".join(context_parts)

    # Step 3: 呼叫 LLM
    system_prompt = """你是專業的台股券商報告研究助手。根據提供的券商報告內容回答使用者問題。

規則：
1. 回答中每個論點必須標註來源，格式為 [n]，n 是報告編號
2. 不同券商觀點有衝突時，並列呈現並標註各自來源
3. 如果報告資料不足以回答，明確說明
4. 回答使用繁體中文
5. 回答結尾用 JSON 格式附上 sources 陣列，格式如下：
<!--SOURCES_JSON-->
[
  {"id": 1, "report_id": 報告ID, "excerpt": "引用的原文段落50-150字"},
  ...
]
<!--/SOURCES_JSON-->
每個被引用的報告都要有一個 source entry，excerpt 是你引用該報告時對應的原文段落。"""

    messages = []
    for h in req.history[-6:]:
        messages.append({"role": h["role"], "content": h["content"]})

    messages.append(
        {
            "role": "user",
            "content": f"以下是相關券商報告：\n\n{context}\n\n使用者問題：{req.question}",
        }
    )

    raw_answer = await call_llm(system_prompt, messages)

    # Step 4: 解析 sources JSON
    sources = []
    answer_text = raw_answer

    if "<!--SOURCES_JSON-->" in raw_answer:
        parts = raw_answer.split("<!--SOURCES_JSON-->")
        answer_text = parts[0].strip()
        json_part = parts[1].split("<!--/SOURCES_JSON-->")[0].strip()
        try:
            source_data = json.loads(json_part)
            for s in source_data:
                src_id = s.get("id", 0)
                report_id = s.get("report_id", 0)
                matched = next((r for r in reports if r.id == report_id), None)
                if not matched and 1 <= src_id <= len(reports):
                    matched = reports[src_id - 1]
                if matched:
                    sources.append(
                        Source(
                            id=src_id,
                            report_id=matched.id,
                            broker=matched.broker or "",
                            date=matched.report_date.isoformat() if matched.report_date else "",
                            stock_code=matched.stock_code or "",
                            stock_name=matched.stock_name or "",
                            rating=matched.rating,
                            target_price=matched.target_price,
                            summary=matched.summary or "",
                            excerpt=s.get("excerpt", ""),
                        )
                    )
        except (json.JSONDecodeError, KeyError):
            pass

    # 如果沒回傳 sources，用報告 metadata 補上
    if not sources:
        for i, r in enumerate(reports, 1):
            if f"[{i}]" in answer_text:
                sources.append(
                    Source(
                        id=i,
                        report_id=r.id,
                        broker=r.broker or "",
                        date=r.report_date.isoformat() if r.report_date else "",
                        stock_code=r.stock_code or "",
                        stock_name=r.stock_name or "",
                        rating=r.rating,
                        target_price=r.target_price,
                        summary=r.summary or "",
                        excerpt=r.summary or "",
                    )
                )

    return ChatResponse(answer=answer_text, sources=sources)


@app.get("/api/stats")
async def api_stats():
    """統計資訊"""
    session = get_session()
    total = session.query(Report).count()
    done = session.query(Report).filter(Report.extraction_status == "done").count()
    session.close()
    return {"total": total, "done": done}
