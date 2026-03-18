"""使用 LLM API 從報告文字中擷取結構化資訊（支援 Claude / OpenAI）"""
import json
import anthropic
import openai
from src.config import CONFIG

_claude_client = None
_openai_client = None


def _get_claude_client():
    global _claude_client
    if _claude_client is None:
        _claude_client = anthropic.Anthropic()
    return _claude_client


def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        _openai_client = openai.OpenAI()
    return _openai_client


EXTRACTION_PROMPT = """你是一個台灣券商研究報告分析助手。請從以下研究報告文本中提取結構化資訊。

報告文本:
{text}

請以純 JSON 格式回傳以下欄位 (不要加 markdown 標記):
{{
  "stock_code": "股票代號 (例: 2330、3443)，純數字字串，若無法判斷則填 null",
  "stock_name": "股票名稱 (例: 聯亞)",
  "broker": "出具報告的券商名稱 (例: 凱基、元大、富邦、美林、摩根士丹利)，若無法判斷則填 null",
  "report_date": "報告發布日期，格式為 YYYY-MM-DD (例: 2025-03-11)，從報告內文、頁首頁尾判斷，若無法判斷則填 null",
  "rating": "投資評等 (請標準化為: 買進/持有/賣出/未評等)",
  "target_price": 目標價數字或null,
  "current_price": 現價數字或null,
  "summary": "報告重點摘要 (100-200字繁體中文)",
  "industry": "主產業分類 (例: 光通訊、半導體、金融、鋼鐵、AI伺服器、生技醫療等，只填一個最主要的)",
  "topics": ["報告涉及的產業主題與技術關鍵字，5-15個標籤"],
  "mentioned_stocks": ["報告中提及的其他股票代碼，不含報告主角，例如 2330、3443"],
  "investment_thesis": "核心投資邏輯，一句話說明為什麼要買/賣這檔股票 (50-100字)",
  "quality_score": "報告品質評分 1-10 分整數",
  "quality_reason": "品質評分理由 (50-100字繁體中文)"
}}

評等標準化規則:
- 買進/推薦/優於大盤/Overweight/Buy/Strong Buy/Add -> 買進
- 中立/持有/區間操作/Neutral/Hold/Market Perform -> 持有
- 賣出/減碼/劣於大盤/Underweight/Sell/Reduce -> 賣出
- 若無法判斷 -> 未評等

topics 範例標籤 (不限於此列表):
光通訊、CPO、矽光子、800G、400G、AI伺服器、HBM、先進封裝、CoWoS、
電動車、自駕、ADAS、儲能、太陽能、風電、
半導體設備、晶圓代工、IC設計、記憶體、DRAM、NAND、
5G、低軌衛星、資料中心、雲端、邊緣運算、
生技、新藥、CDMO、醫材、
PCB、ABF載板、CCL、
面板、Mini LED、Micro LED、OLED、
航運、貨櫃、散裝、
金融、壽險、銀行、證券

stock_code 注意事項:
- 這是報告主角的台股代碼，通常在報告標題或頁首出現
- 只填純數字 (例: "3714")，不要包含名稱
- 如果是海外股票或無法判斷，填 null

report_date 注意事項:
- 通常在報告頁首、頁尾、封面出現，格式可能是 2025/03/11、2025年3月11日、March 11, 2025、11 Mar 2025 等
- 請統一轉為 YYYY-MM-DD 格式
- 若有多個日期，選擇報告發布日期 (而非資料截止日或預測日期)
- 若無法判斷則填 null

broker 注意事項:
- 通常在報告頁首、頁尾、浮水印或 logo 附近出現
- 請用繁體中文簡稱 (例: 凱基、元大、富邦、國泰、中信、永豐、玉山、群益、統一、日盛、台新、兆豐、合庫、第一金、華南、彰銀)
- 外資券商也用常見中文名 (例: 美林、摩根士丹利、高盛、瑞銀、花旗、野村、大和、麥格理)
- 若無法判斷則填 null

mentioned_stocks 注意事項:
- 只列股票代碼數字 (例: "2330")，不要包含股票名稱
- 不要包含報告主角的股票代碼
- 如果報告沒有提及其他股票，回傳空陣列 []

quality_score 評分標準 (1-10 分):
- 9-10: 深度研究報告，含獨特觀點、詳細財務模型、完整產業分析、明確催化劑
- 7-8: 有實質分析內容，包含財務預測、產業比較或具體投資邏輯
- 5-6: 一般性報告，資訊量中等，有基本面分析但缺乏深度
- 3-4: 內容偏淺，主要是新聞摘要或簡短評論，分析有限
- 1-2: 幾乎無分析價值，內容極少或多為制式模板

quality_reason 注意事項:
- 請具體說明給分的依據，例如報告的分析深度、數據品質、觀點獨特性等
- 使用繁體中文，50-100字"""


def _call_claude(prompt: str) -> str:
    """呼叫 Claude API"""
    response = _get_claude_client().messages.create(
        model=CONFIG["llm"]["claude_model"],
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def _call_openai(prompt: str) -> str:
    """呼叫 OpenAI API"""
    response = _get_openai_client().chat.completions.create(
        model=CONFIG["llm"]["openai_model"],
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content.strip()


def extract_report_data(text: str) -> dict:
    """呼叫 LLM API 擷取結構化資料"""
    truncated = text[:15000] if len(text) > 15000 else text
    prompt = EXTRACTION_PROMPT.format(text=truncated)

    provider = CONFIG["llm"]["provider"]
    if provider == "openai":
        content = _call_openai(prompt)
    else:
        content = _call_claude(prompt)

    # 嘗試清理可能的 markdown 包裹
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:-1])

    return json.loads(content)
