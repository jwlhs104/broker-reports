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


def _build_cli_prompt(system_prompt: str, messages: list[dict]) -> str:
    """把 system prompt + 對話歷史組合成 claude CLI 用的 prompt。"""
    conv_parts = [system_prompt, ""]
    for m in messages:
        role_label = "使用者" if m["role"] == "user" else "助手"
        conv_parts.append(f"[{role_label}]\n{m['content']}")
    return "\n\n".join(conv_parts)


async def call_llm(system_prompt: str, messages: list[dict], model: str = "sonnet") -> str:
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
        full_prompt = _build_cli_prompt(system_prompt, messages)
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "-p",
            full_prompt,
            "--model",
            model,
            "--allowedTools",
            "",
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


async def call_llm_stream(system_prompt: str, messages: list[dict], model: str = "sonnet") -> AsyncGenerator[str, None]:
    """串流版 LLM 呼叫，逐 chunk yield 文字。"""
    if LLM_BACKEND == "openai" and llm:
        full_messages = [{"role": "system", "content": system_prompt}] + messages
        stream = llm.chat.completions.create(
            model=MODEL,
            max_tokens=4096,
            messages=full_messages,
            stream=True,
        )
        for chunk in stream:
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content
    else:
        full_prompt = _build_cli_prompt(system_prompt, messages)

        proc = await asyncio.create_subprocess_exec(
            "claude",
            "-p",
            full_prompt,
            "--model",
            model,
            "--allowedTools",
            "",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "NO_COLOR": "1"},
        )

        async for line in proc.stdout:
            text = line.decode().strip()
            if not text:
                continue
            try:
                event = json.loads(text)
                etype = event.get("type", "")

                # 串流 delta: text_delta
                if etype == "stream_event":
                    inner = event.get("event", {})
                    delta = inner.get("delta", {})
                    if delta.get("type") == "text_delta" and "text" in delta:
                        yield delta["text"]

                # 完整結果 fallback
                elif etype == "result" and event.get("result"):
                    yield event["result"]

            except json.JSONDecodeError:
                continue

        await proc.wait()


# ── Pydantic models ──


class DataSourceConfig(BaseModel):
    broker_reports: bool = True
    financial_statements: bool = False
    earnings_presentations: bool = False
    web_search: bool = False


class CustomContext(BaseModel):
    name: str
    content: str


class ChatRequest(BaseModel):
    question: str
    stock_code: str | None = None
    history: list[dict] = []  # [{role, content}]
    data_sources: DataSourceConfig = DataSourceConfig()
    custom_context: list[CustomContext] = []


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


async def extract_search_intent(question: str, history: list[dict]) -> dict:
    """Step 1: 用 LLM 結合上下文理解搜尋意圖。

    回傳 {"stock_codes": [...], "stock_names": [...], "keywords": [...], "resolved_question": "..."}
    """
    intent_prompt = """你是搜尋意圖分析器。根據使用者的對話歷史和最新問題，提取要搜尋的條件。

你必須只回傳 JSON，不要有其他文字。格式如下：
{
  "stock_codes": ["2330", "2454"],
  "stock_names": ["台積電", "聯發科"],
  "keywords": ["目標價", "AI伺服器"],
  "resolved_question": "把指代詞解析後的完整問題"
}

規則：
1. stock_codes: 提取所有提到的台股代碼（4位數字）
2. stock_names: 提取所有提到的股票名稱（含簡稱如「台積」→「台積電」）
3. keywords: 提取搜尋關鍵字（產業、主題、分析角度等）
4. resolved_question: 將「它」「這家公司」「上面那個」等指代詞替換為實際名稱，結合上下文還原完整問題
5. 如果對話歷史中提到某支股票，而最新問題是「那XXX呢？」或「它的目標價？」，要正確識別指代的股票
6. 如果使用者問的是通用問題（如「AI產業趨勢」），stock_codes 可以為空，靠 keywords 搜尋"""

    conv_messages = []
    for h in history[-6:]:
        conv_messages.append({"role": h["role"], "content": h["content"]})
    conv_messages.append({"role": "user", "content": question})

    try:
        raw = await call_llm(intent_prompt, conv_messages, model="haiku")
        # 清理：移除 markdown code block 標記
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        intent = json.loads(cleaned)
        logger.info(f"[Intent] question='{question}' → {intent}")
        return intent
    except (json.JSONDecodeError, Exception) as e:
        logger.warning(f"[Intent] LLM parse failed: {e}, falling back to regex")
        # Fallback: 用原本的 regex 提取
        terms = extract_search_terms(question)
        return {
            "stock_codes": terms["stock_codes"],
            "stock_names": terms["stock_names"],
            "keywords": terms["keywords"],
            "resolved_question": question,
        }


def search_by_intent(intent: dict, stock_code: str | None = None, limit: int = 10) -> list[Report]:
    """Step 2: 根據 LLM 提取的意圖搜尋報告。"""
    seen_ids = set()
    results = []

    def _add(reports, max_count: int = 0):
        added = 0
        for r in reports:
            if r.id not in seen_ids:
                seen_ids.add(r.id)
                results.append(r)
                added += 1
                if max_count and added >= max_count:
                    break

    # 手動指定的 stock_code filter
    if stock_code:
        _add(search_reports(stock_code=stock_code))
        if len(results) >= limit:
            return results[:limit]

    # 從 intent 取得的股票代碼
    codes = intent.get("stock_codes", [])
    names = intent.get("stock_names", [])
    keywords = intent.get("keywords", [])

    # 用股票名稱反查代碼（LLM 可能只給名稱沒給代碼）
    name_map = _get_stock_name_map()
    for name in names:
        code = name_map.get(name)
        if code and code not in codes:
            codes.append(code)

    # 1. 股票代碼搜尋（均勻分配）
    if codes:
        per_stock = max(3, limit // len(codes))
        for code in codes:
            _add(search_reports(stock_code=code), max_count=per_stock)

    # 2. 關鍵字補充搜尋
    for kw in keywords:
        if len(results) >= limit:
            break
        _add(smart_search(kw, limit=limit - len(results)))

    # 3. Fallback: resolved_question 全文搜尋
    if not results:
        resolved = intent.get("resolved_question", "")
        if resolved:
            _add(fulltext_search(resolved, limit=limit))
        if not results:
            _add(smart_search(resolved or "", limit=limit))

    return results[:limit]


@app.post("/api/chat")
async def api_chat(req: ChatRequest):
    """RAG Chat — LLM 意圖提取 → 搜尋報告 → LLM 生成回答"""

    # Step 1: LLM 結合上下文提取搜尋意圖
    intent = await extract_search_intent(req.question, req.history)
    resolved_question = intent.get("resolved_question", req.question)

    # Step 2: 根據意圖搜尋報告
    reports = search_by_intent(intent, req.stock_code, limit=10)

    if not reports:
        # Fallback: 用原始 regex 再搜一次
        reports = search_for_chat(req.question, req.stock_code, limit=10)

    if not reports:
        return ChatResponse(
            answer="找不到相關的券商報告。請嘗試其他關鍵字或股票代碼。",
            sources=[],
        )

    # 限制最多 8 份報告送入 context
    reports = reports[:8]

    # Step 3: 組裝 context
    context_parts = []
    for i, r in enumerate(reports, 1):
        raw = (r.raw_text or "")[:3000]
        context_parts.append(
            f"[報告 {i}] ID={r.id}\n"
            f"券商: {r.broker} | 日期: {r.report_date} | 股票: {r.stock_code} {r.stock_name}\n"
            f"評等: {r.rating} | 目標價: {r.target_price}\n"
            f"摘要: {r.summary}\n"
            f"投資邏輯: {r.investment_thesis}\n"
            f"原文:\n{raw}\n"
        )

    context = "\n---\n".join(context_parts)

    # Step 4: 呼叫 LLM 生成回答
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
            "content": f"以下是相關券商報告：\n\n{context}\n\n使用者問題：{resolved_question}",
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


# ══════════════════════════════════════════════════════════
#  SSE Streaming endpoint
# ══════════════════════════════════════════════════════════
from starlette.responses import StreamingResponse


def _build_sources_from_reports(
    reports: list[Report], answer_text: str, source_data: list[dict] | None = None
) -> list[dict]:
    """從報告 metadata 和（可選的）LLM source_data 建立 sources 列表。"""
    sources = []
    if source_data:
        for s in source_data:
            src_id = s.get("id", 0)
            report_id = s.get("report_id", 0)
            matched = next((r for r in reports if r.id == report_id), None)
            if not matched and 1 <= src_id <= len(reports):
                matched = reports[src_id - 1]
            if matched:
                sources.append(
                    {
                        "id": src_id,
                        "report_id": matched.id,
                        "broker": matched.broker or "",
                        "date": matched.report_date.isoformat() if matched.report_date else "",
                        "stock_code": matched.stock_code or "",
                        "stock_name": matched.stock_name or "",
                        "rating": matched.rating,
                        "target_price": matched.target_price,
                        "summary": matched.summary or "",
                        "excerpt": s.get("excerpt", ""),
                    }
                )
    if not sources:
        for i, r in enumerate(reports, 1):
            if f"[{i}]" in answer_text:
                sources.append(
                    {
                        "id": i,
                        "report_id": r.id,
                        "broker": r.broker or "",
                        "date": r.report_date.isoformat() if r.report_date else "",
                        "stock_code": r.stock_code or "",
                        "stock_name": r.stock_name or "",
                        "rating": r.rating,
                        "target_price": r.target_price,
                        "summary": r.summary or "",
                        "excerpt": r.summary or "",
                    }
                )
    return sources


def _sse_event(event: str, data: str) -> str:
    """格式化一個 SSE event。"""
    lines = data.replace("\n", "\ndata: ")
    return f"event: {event}\ndata: {lines}\n\n"


# ══════════════════════════════════════════════════════════
#  外部資料來源擷取（財報、法說會、Web Search）
# ══════════════════════════════════════════════════════════


async def _call_claude_with_web(prompt: str, model: str = "haiku") -> str:
    """呼叫 Claude CLI 並啟用 WebSearch + WebFetch 工具。

    讓 Claude agent 自行搜尋網路、擷取網頁，回傳整理過的結果。
    """
    proc = await asyncio.create_subprocess_exec(
        "claude",
        "-p",
        prompt,
        "--model",
        model,
        "--allowedTools",
        "mcp__fetch__fetch,WebSearch,WebFetch",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "NO_COLOR": "1"},
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)

    if proc.returncode != 0:
        err = stderr.decode().strip()
        logger.warning(f"[WebAgent] Claude CLI error: {err}")
        return ""

    return stdout.decode().strip()


def _parse_web_result(raw: str) -> tuple[list[str], str]:
    """解析 Claude WebSearch 回傳的 ---SOURCES--- / ---CONTENT--- 格式。

    回傳 (source_urls, content)
    """
    source_urls = []
    content = raw

    if "---SOURCES---" in raw and "---CONTENT---" in raw:
        parts = raw.split("---CONTENT---", 1)
        source_part = parts[0].split("---SOURCES---", 1)[-1].strip()
        source_urls = [line.strip() for line in source_part.split("\n") if line.strip()]
        content = parts[1].strip()
    elif "---CONTENT---" in raw:
        content = raw.split("---CONTENT---", 1)[-1].strip()

    return source_urls, content


async def fetch_financial_statements(stock_codes: list[str]) -> list[dict]:
    """用 Claude WebSearch 搜尋個股財報資料。

    回傳 [{"name": "...", "content": "...", "sources": ["url1", ...]}]
    """
    results = []
    for code in stock_codes[:3]:
        prompt = (
            f"請搜尋台股 {code} 的最新財報資訊。\n\n"
            "搜尋策略：\n"
            f"1. 優先搜尋「{code} 財報」或到財報狗 statementdog.com 查詢\n"
            f"2. 也可搜尋「{code} 營收 EPS 毛利率」等關鍵字\n\n"
            "請回傳以下格式（純文字，不要 markdown code block）：\n"
            "---SOURCES---\n"
            "來源名稱1 | 網址1\n"
            "來源名稱2 | 網址2\n"
            "---CONTENT---\n"
            "整理後的財報重點摘要（包含：最近幾季營收、EPS、毛利率、營益率等關鍵數據）\n"
        )
        try:
            raw = await _call_claude_with_web(prompt)
            if not raw or len(raw) < 50:
                continue

            source_urls, content = _parse_web_result(raw)
            results.append(
                {
                    "name": f"{code} 財報摘要",
                    "content": content[:5000],
                    "sources": source_urls[:5],
                }
            )
        except Exception as e:
            logger.warning(f"[FinancialStatements] Failed for {code}: {e}")
            continue
    return results


async def fetch_earnings_call(stock_codes: list[str]) -> list[dict]:
    """用 Claude WebSearch 搜尋法說會簡報資料。"""
    results = []
    for code in stock_codes[:3]:
        prompt = (
            f"請搜尋台股 {code} 的最新法說會簡報或法說會重點。\n\n"
            "搜尋策略（依優先順序）：\n"
            f"1. 搜尋「{code} 法說會 site:finmoconf.com」\n"
            f"2. 搜尋「{code} 法說會簡報」找公司官網 IR 頁面\n"
            f"3. 搜尋「{code} investor conference」\n\n"
            "請回傳以下格式（純文字，不要 markdown code block）：\n"
            "---SOURCES---\n"
            "來源名稱1 | 網址1\n"
            "來源名稱2 | 網址2\n"
            "---CONTENT---\n"
            "法說會重點整理（包含：管理層展望、營運目標、產能規劃、Q&A 重點等）\n"
        )
        try:
            raw = await _call_claude_with_web(prompt)
            if not raw or len(raw) < 50:
                continue

            source_urls, content = _parse_web_result(raw)
            results.append(
                {
                    "name": f"{code} 法說會摘要",
                    "content": content[:5000],
                    "sources": source_urls[:5],
                }
            )
        except Exception as e:
            logger.warning(f"[EarningsCall] Failed for {code}: {e}")
            continue
    return results


async def fetch_web_search(question: str, stock_codes: list[str]) -> list[dict]:
    """用 Claude WebSearch 搜尋補充資料。"""
    code_str = " ".join(stock_codes[:2]) if stock_codes else ""
    prompt = (
        f"請搜尋以下問題的相關資料作為投資研究的補充：\n\n"
        f"問題：{question}\n"
        f"相關股票：{code_str}\n\n"
        "搜尋範圍：新聞、分析師評論、產業報告、市場數據等。\n\n"
        "請回傳以下格式（純文字，不要 markdown code block）：\n"
        "---SOURCES---\n"
        "來源名稱1 | 網址1\n"
        "來源名稱2 | 網址2\n"
        "---CONTENT---\n"
        "整理後的重點摘要，每個資訊點標註來自哪個來源\n"
    )
    try:
        raw = await _call_claude_with_web(prompt)
        if not raw or len(raw) < 50:
            return []

        source_urls, content = _parse_web_result(raw)
        return [
            {
                "name": "外部網路資料",
                "content": content[:6000],
                "sources": source_urls[:5],
            }
        ]
    except Exception as e:
        logger.warning(f"[WebSearch] Failed: {e}")
        return []


@app.post("/api/chat/stream")
async def api_chat_stream(req: ChatRequest):
    """SSE Streaming RAG Chat — 意圖提取 → 搜尋 → 串流回答"""

    async def event_generator():
        # ── Phase 1: 意圖提取 ──
        intent = await extract_search_intent(req.question, req.history)
        resolved_question = intent.get("resolved_question", req.question)

        # ── Phase 2: 搜尋報告 ──

        reports = search_by_intent(intent, req.stock_code, limit=10)
        if not reports:
            reports = search_for_chat(req.question, req.stock_code, limit=10)

        # 如果有其他資料來源開啟，不因找不到報告就中斷
        has_other_sources = (
            req.data_sources.financial_statements
            or req.data_sources.earnings_presentations
            or req.data_sources.web_search
            or req.custom_context
        )

        if not reports and not has_other_sources:
            yield _sse_event("chunk", "找不到相關的券商報告。請嘗試其他關鍵字或股票代碼。")
            yield _sse_event("sources", "[]")
            yield _sse_event("done", "")
            return

        reports = reports[:8]

        # 先送出報告 metadata（讓前端可以提前渲染 source chips）
        report_meta = []
        for i, r in enumerate(reports, 1):
            report_meta.append(
                {
                    "id": i,
                    "report_id": r.id,
                    "broker": r.broker or "",
                    "date": r.report_date.isoformat() if r.report_date else "",
                    "stock_code": r.stock_code or "",
                    "stock_name": r.stock_name or "",
                    "rating": r.rating,
                    "target_price": r.target_price,
                    "summary": r.summary or "",
                    "excerpt": "",
                }
            )
        yield _sse_event("sources_preview", json.dumps(report_meta, ensure_ascii=False))

        # 送出 related 報告用的 stock_codes（讓前端可以同步抓取右側欄）
        # 從報告 + intent 兩處取得股票代碼
        related_codes = list({r.stock_code for r in reports if r.stock_code})
        if not related_codes:
            # 報告不夠時，從 intent 取得股票代碼（給外部資料源用）
            related_codes = intent.get("stock_codes", [])
            if req.stock_code:
                related_codes = [req.stock_code] + [c for c in related_codes if c != req.stock_code]
        report_ids = [r.id for r in reports]
        yield _sse_event(
            "related_hint",
            json.dumps({"stock_codes": related_codes, "exclude_ids": report_ids}, ensure_ascii=False),
        )

        # ── Phase 3: 組裝 context ──
        context_parts = []

        # 3a. 券商報告
        if req.data_sources.broker_reports:
            for i, r in enumerate(reports, 1):
                raw = (r.raw_text or "")[:3000]
                context_parts.append(
                    f"[報告 {i}] ID={r.id}\n"
                    f"券商: {r.broker} | 日期: {r.report_date} | 股票: {r.stock_code} {r.stock_name}\n"
                    f"評等: {r.rating} | 目標價: {r.target_price}\n"
                    f"摘要: {r.summary}\n"
                    f"投資邏輯: {r.investment_thesis}\n"
                    f"原文:\n{raw}\n"
                )

        # 從意圖中取得股票代碼，供外部資料源使用
        stock_codes = related_codes  # 已從 reports 提取

        # 收集外部來源（財報、法說會、外網），供 system prompt 和 SSE 使用
        external_sources = {
            "financial": [],  # [{"name": ..., "content": ..., "sources": [...]}]
            "earnings": [],
            "web": [],
        }

        # 3b. 財報
        if req.data_sources.financial_statements and stock_codes:
            yield _sse_event("chunk", "🔍 正在搜尋財報資料...\n\n")
            try:
                fin_data = await fetch_financial_statements(stock_codes)
                external_sources["financial"] = fin_data
                for j, fd in enumerate(fin_data, 1):
                    src_links = "\n".join(f"  - {s}" for s in fd.get("sources", []))
                    context_parts.append(
                        f"[財報資料 F{j}] {fd['name']}\n參考來源:\n{src_links}\n內容:\n{fd['content']}\n"
                    )
                if fin_data:
                    yield _sse_event("chunk", f"✅ 找到 {len(fin_data)} 筆財報資料\n\n")
                else:
                    yield _sse_event("chunk", "⚠️ 未找到財報資料\n\n")
            except Exception as e:
                logger.warning(f"[FinancialStatements] Error: {e}")
                yield _sse_event("chunk", "⚠️ 財報資料擷取失敗\n\n")

        # 3c. 法說會簡報
        if req.data_sources.earnings_presentations and stock_codes:
            yield _sse_event("chunk", "🔍 正在搜尋法說會資料...\n\n")
            try:
                ec_data = await fetch_earnings_call(stock_codes)
                external_sources["earnings"] = ec_data
                for j, ec in enumerate(ec_data, 1):
                    src_links = "\n".join(f"  - {s}" for s in ec.get("sources", []))
                    context_parts.append(
                        f"[法說會資料 E{j}] {ec['name']}\n參考來源:\n{src_links}\n內容:\n{ec['content']}\n"
                    )
                if ec_data:
                    yield _sse_event("chunk", f"✅ 找到 {len(ec_data)} 筆法說會資料\n\n")
                else:
                    yield _sse_event("chunk", "⚠️ 未找到法說會資料\n\n")
            except Exception as e:
                logger.warning(f"[EarningsCall] Error: {e}")
                yield _sse_event("chunk", "⚠️ 法說會資料擷取失敗\n\n")

        # 3d. 外網資料（Claude WebSearch）
        if req.data_sources.web_search:
            yield _sse_event("chunk", "🔍 正在搜尋外部網路資料...\n\n")
            try:
                web_data = await fetch_web_search(resolved_question, stock_codes)
                external_sources["web"] = web_data
                for j, wd in enumerate(web_data, 1):
                    src_links = "\n".join(f"  - {s}" for s in wd.get("sources", []))
                    context_parts.append(
                        f"[外部資料 W{j}] {wd['name']}\n參考來源:\n{src_links}\n內容:\n{wd['content']}\n"
                    )
                if web_data:
                    yield _sse_event("chunk", "✅ 找到外部網路資料\n\n")
                else:
                    yield _sse_event("chunk", "⚠️ 未找到相關外部資料\n\n")
            except Exception as e:
                logger.warning(f"[WebSearch] Error: {e}")
                yield _sse_event("chunk", "⚠️ 外部資料擷取失敗\n\n")

        # 送出外部來源 metadata 給前端（用於渲染來源連結區塊）
        if any(external_sources.values()):
            yield _sse_event(
                "external_sources",
                json.dumps(
                    {
                        k: [{"name": d["name"], "sources": d.get("sources", [])} for d in v]
                        for k, v in external_sources.items()
                        if v
                    },
                    ensure_ascii=False,
                ),
            )

        # 3e. 使用者匯入的自訂資料
        if req.custom_context:
            for j, ctx in enumerate(req.custom_context, 1):
                context_parts.append(f"[使用者匯入資料 U{j}] 來源: {ctx.name}\n內容:\n{ctx.content[:5000]}\n")

        context = "\n---\n".join(context_parts)

        # 根據資料來源開關調整 system prompt
        active_sources = []
        if req.data_sources.broker_reports:
            active_sources.append("券商報告")
        if req.data_sources.financial_statements:
            active_sources.append("財務報表")
        if req.data_sources.earnings_presentations:
            active_sources.append("法說會簡報")
        if req.data_sources.web_search:
            active_sources.append("外部網路資料")
        sources_note = "、".join(active_sources) if active_sources else "券商報告"

        # 根據啟用的外部來源，動態加入區塊格式指引
        section_rules = ""
        has_fin = bool(external_sources.get("financial"))
        has_ec = bool(external_sources.get("earnings"))
        has_web = bool(external_sources.get("web"))

        if has_fin or has_ec or has_web:
            section_rules = "\n\n### 回答結構要求\n回答正文之後，請依序加上以下區塊（僅限有資料的）：\n"
            if has_fin:
                section_rules += (
                    "\n**📊 財報摘要**\n"
                    "- 用 `## 📊 財報摘要` 作為標題\n"
                    "- 列出關鍵財務數據（營收、EPS、毛利率等）\n"
                    "- 區塊最後用「📎 資料來源：」列出所有參考來源的超連結，格式：[來源名稱](URL)\n"
                    "- 來源 URL 在 context 中的「參考來源」欄位\n"
                )
            if has_ec:
                section_rules += (
                    "\n**🎤 法說會重點**\n"
                    "- 用 `## 🎤 法說會重點` 作為標題\n"
                    "- 列出管理層展望、營運目標、產能規劃、Q&A 重點等\n"
                    "- 區塊最後用「📎 資料來源：」列出所有參考來源的超連結，格式：[來源名稱](URL)\n"
                    "- 來源 URL 在 context 中的「參考來源」欄位\n"
                )
            if has_web:
                section_rules += (
                    "\n**🌐 外部資料補充**\n"
                    "- 用 `## 🌐 外部資料補充` 作為標題\n"
                    "- 整理外部搜尋到的補充資訊\n"
                    "- 區塊最後用「📎 資料來源：」列出所有參考來源的超連結，格式：[來源名稱](URL)\n"
                )

        system_prompt = (
            f"你是專業的台股券商報告研究助手。根據提供的{sources_note}內容回答使用者問題。\n\n"
            "規則：\n"
            "1. 回答中每個論點必須標註來源，格式為 [n]，n 是報告編號\n"
            "2. 不同券商觀點有衝突時，並列呈現並標註各自來源\n"
            "3. 如果報告資料不足以回答，明確說明\n"
            "4. 回答使用繁體中文\n"
            "5. 來源連結請使用 markdown 超連結格式 [名稱](URL)，不要只貼裸網址\n"
            "6. 回答結尾用 JSON 格式附上 sources 陣列，格式如下：\n"
            "<!--SOURCES_JSON-->\n"
            '[\n  {"id": 1, "report_id": 報告ID, "excerpt": "引用的原文段落50-150字"},\n  ...\n]\n'
            "<!--/SOURCES_JSON-->\n"
            "每個被引用的報告都要有一個 source entry，excerpt 是你引用該報告時對應的原文段落。"
            f"{section_rules}"
        )

        messages = []
        for h in req.history[-6:]:
            messages.append({"role": h["role"], "content": h["content"]})
        messages.append(
            {
                "role": "user",
                "content": f"以下是相關券商報告：\n\n{context}\n\n使用者問題：{resolved_question}",
            }
        )

        # ── Phase 4: 串流回答 ──

        full_answer = []
        buffer = ""
        in_sources_block = False

        async for chunk in call_llm_stream(system_prompt, messages):
            buffer += chunk

            # 偵測 SOURCES_JSON 開始標記
            if "<!--SOURCES_JSON-->" in buffer and not in_sources_block:
                # 把標記前面的文字送出
                before = buffer.split("<!--SOURCES_JSON-->")[0]
                if before:
                    yield _sse_event("chunk", before)
                    full_answer.append(before)
                in_sources_block = True
                buffer = buffer.split("<!--SOURCES_JSON-->", 1)[1]
                continue

            if in_sources_block:
                # 在 sources block 裡，不送出，繼續累積
                continue

            # 正常文字 → 送出
            yield _sse_event("chunk", buffer)
            full_answer.append(buffer)
            buffer = ""

        # 處理剩餘 buffer
        if not in_sources_block and buffer:
            yield _sse_event("chunk", buffer)
            full_answer.append(buffer)

        # ── Phase 5: 解析 sources 並送出 ──
        answer_text = "".join(full_answer).strip()
        source_data = None

        if in_sources_block:
            json_text = buffer.split("<!--/SOURCES_JSON-->")[0].strip()
            try:
                source_data = json.loads(json_text)
            except json.JSONDecodeError:
                pass

        # Fallback: LLM 沒用 <!--SOURCES_JSON--> 標記，
        # 但在正文尾端直接輸出了裸 JSON array
        if source_data is None and not in_sources_block:
            # 嘗試找文末的 JSON array
            match = re.search(
                r'\[\s*\{[^{]*?"report_id"\s*:.*\]\s*$',
                answer_text,
                re.DOTALL,
            )
            if match:
                try:
                    source_data = json.loads(match.group(0))
                    # 從 answer_text 移除這段 JSON
                    answer_text = answer_text[: match.start()].strip()
                except json.JSONDecodeError:
                    pass

        sources = _build_sources_from_reports(reports, answer_text, source_data)
        yield _sse_event("sources", json.dumps(sources, ensure_ascii=False))
        yield _sse_event("done", "")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx/cloudflare 不要 buffer
        },
    )


@app.get("/api/stats")
async def api_stats():
    """統計資訊"""
    session = get_session()
    total = session.query(Report).count()
    done = session.query(Report).filter(Report.extraction_status == "done").count()
    session.close()
    return {"total": total, "done": done}


# ══════════════════════════════════════════════════════════
#  Related Reports endpoint
# ══════════════════════════════════════════════════════════


@app.get("/api/related")
async def api_related(
    stock_codes: str = "",
    exclude_ids: str = "",
    limit: int = 15,
):
    """取得高關聯的報告，用於右側欄"""

    if not stock_codes:
        return []

    codes = [c.strip() for c in stock_codes.split(",") if c.strip()]
    exclude = set()
    if exclude_ids:
        exclude = {int(x.strip()) for x in exclude_ids.split(",") if x.strip().isdigit()}

    session = get_session()
    seen_ids = set(exclude)
    results = []

    try:
        # 1. 直接用股票代碼搜尋報告
        for code in codes:
            reports = (
                session.query(Report)
                .filter(Report.stock_code == code)
                .filter(Report.extraction_status == "done")
                .order_by(Report.report_date.desc())
                .limit(limit)
                .all()
            )
            for r in reports:
                if r.id not in seen_ids:
                    seen_ids.add(r.id)
                    results.append((r, 1.0))

        # 2. 搜尋 mentioned_stocks 包含這些代碼的報告
        for code in codes:
            mentioned = (
                session.query(Report)
                .filter(Report.extraction_status == "done")
                .filter(Report.mentioned_stocks.contains(code))
                .order_by(Report.report_date.desc())
                .limit(10)
                .all()
            )
            for r in mentioned:
                if r.id not in seen_ids:
                    seen_ids.add(r.id)
                    results.append((r, 0.6))

        # 3. 計算 is_first_coverage
        # 取得每個 broker+stock_code 的最早報告日期
        first_coverage_cache = {}
        all_codes_in_results = set()
        for r, _ in results:
            if r.stock_code and r.broker:
                all_codes_in_results.add(r.stock_code)

        if all_codes_in_results:
            from sqlalchemy import func as sqlfunc

            first_dates = (
                session.query(Report.stock_code, Report.broker, sqlfunc.min(Report.report_date))
                .filter(Report.stock_code.in_(list(all_codes_in_results)))
                .filter(Report.extraction_status == "done")
                .group_by(Report.stock_code, Report.broker)
                .all()
            )
            for sc, br, min_date in first_dates:
                first_coverage_cache[(sc, br)] = min_date

    finally:
        session.close()

    # 組裝回傳結果
    output = []
    for r, relevance_score in results:
        is_first = False
        if r.stock_code and r.broker:
            first_date = first_coverage_cache.get((r.stock_code, r.broker))
            if first_date and r.report_date and r.report_date == first_date:
                is_first = True

        tags = []
        if relevance_score >= 0.8:
            tags.append("關聯高")
        if r.page_count and r.page_count >= 20:
            tags.append("頁數多")
        if is_first:
            tags.append("初次覆蓋")

        output.append(
            {
                "id": r.id,
                "stock_code": r.stock_code or "",
                "stock_name": r.stock_name or "",
                "broker": r.broker or "",
                "date": r.report_date.isoformat() if r.report_date else "",
                "rating": r.rating,
                "target_price": r.target_price,
                "page_count": r.page_count,
                "relevance_score": relevance_score,
                "is_first_coverage": is_first,
                "tags": tags,
                "summary": (r.summary or "")[:150],
            }
        )

    return output[:limit]


# ══════════════════════════════════════════════════════════
#  Import endpoints (file upload + URL fetch)
# ══════════════════════════════════════════════════════════

# 暫存匯入的資料（session-scoped, 記憶體中）
_import_store: dict[str, dict] = {}

from fastapi import File as _File  # noqa: E402
from fastapi import UploadFile as _UploadFile  # noqa: E402


@app.post("/api/import/file")
async def api_import_file(file: _UploadFile = _File(...)):
    """上傳檔案並提取文字內容"""
    import hashlib as _hashlib
    import tempfile as _tempfile

    content = await file.read()
    file_id = _hashlib.md5(content).hexdigest()[:12]

    filename = file.filename or "uploaded"
    ext = Path(filename).suffix.lower()

    text = ""
    if ext == ".pdf":
        # 嘗試用 src/pdf_parser 提取
        try:
            with _tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            from src.pdf_parser import extract_text

            text = extract_text(tmp_path)
            Path(tmp_path).unlink(missing_ok=True)
        except Exception as e:
            text = f"[PDF 文字提取失敗: {e}]"
    elif ext in (".txt", ".md", ".csv", ".json"):
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            text = content.decode("big5", errors="replace")
    else:
        text = f"[不支援的檔案格式: {ext}]"

    _import_store[file_id] = {"name": filename, "content": text}

    return {
        "id": file_id,
        "name": filename,
        "preview": text[:200] + ("..." if len(text) > 200 else ""),
        "char_count": len(text),
    }


@app.post("/api/import/url")
async def api_import_url(req: dict):
    """從 URL 擷取文字內容"""
    import hashlib as _hashlib

    url = req.get("url", "").strip()
    if not url:
        return {"error": "URL is required"}

    try:
        import httpx as _httpx

        async with _httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            html = resp.text

        # 簡單的 HTML → 文字（去 tag）
        import re as _re

        text = _re.sub(r"<script[^>]*>.*?</script>", "", html, flags=_re.DOTALL)
        text = _re.sub(r"<style[^>]*>.*?</style>", "", text, flags=_re.DOTALL)
        text = _re.sub(r"<[^>]+>", " ", text)
        text = _re.sub(r"\s+", " ", text).strip()

        file_id = _hashlib.md5(url.encode()).hexdigest()[:12]
        # 取 domain 作為名稱
        from urllib.parse import urlparse

        domain = urlparse(url).netloc or url[:30]
        name = f"{domain}"

        _import_store[file_id] = {"name": name, "content": text[:50000]}

        return {
            "id": file_id,
            "name": name,
            "preview": text[:200] + ("..." if len(text) > 200 else ""),
            "char_count": min(len(text), 50000),
        }
    except Exception as e:
        return {"error": f"無法擷取該網址: {str(e)}"}
