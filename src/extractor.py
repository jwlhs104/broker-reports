"""使用 Claude Agent SDK 從報告文字中擷取結構化資訊"""
import json
import asyncio
import logging
from typing import Optional

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    ResultMessage,
    AssistantMessage,
    TextBlock,
)
from src.config import CONFIG

logger = logging.getLogger(__name__)

# JSON Schema 定義（供 prompt 使用）
FIELDS_SPEC = """
{
  "stock_code": "股票代號，純數字 (例: 2330)，無法判斷填 null",
  "stock_name": "股票名稱 (例: 台積電)，無法判斷填 null",
  "broker": "券商名稱繁中簡稱 (例: 凱基)，無法判斷填 null",
  "report_date": "報告日期 YYYY-MM-DD 格式，無法判斷填 null",
  "rating": "標準化投資評等，僅限: 買進/持有/賣出/未評等",
  "target_price": "目標價 (數字)，無則填 null",
  "current_price": "現價 (數字)，無則填 null",
  "summary": "報告重點摘要 100-200 字繁體中文",
  "industry": "主產業分類 (例: 光通訊、半導體、AI伺服器)",
  "topics": ["產業主題與技術關鍵字 5-15 個標籤"],
  "mentioned_stocks": ["報告中提及的其他股票代碼 (不含主角)"],
  "investment_thesis": "核心投資邏輯 50-100 字",
  "quality_score": "報告品質評分 1-10 (整數)",
  "quality_reason": "品質評分理由 50-100 字繁體中文"
}
"""

EXTRACTION_PROMPT = """你是一個台灣券商研究報告分析助手。請從以下報告文本中提取結構化資訊。

評等標準化規則:
- 買進/推薦/優於大盤/Overweight/Buy/Strong Buy/Add -> 買進
- 中立/持有/區間操作/Neutral/Hold/Market Perform -> 持有
- 賣出/減碼/劣於大盤/Underweight/Sell/Reduce -> 賣出
- 若無法判斷 -> 未評等

stock_code 注意事項:
- 這是報告主角的台股代碼，通常在報告標題或頁首出現
- 只填純數字 (例: "3714")，不要包含名稱
- 如果是海外股票或無法判斷，填 null

report_date 注意事項:
- 通常在報告頁首、頁尾、封面出現
- 請統一轉為 YYYY-MM-DD 格式
- 若有多個日期，選擇報告發布日期

broker 注意事項:
- 用繁體中文簡稱 (例: 凱基、元大、富邦、美林、摩根士丹利)
- 若無法判斷則填 null

quality_score 評分標準 (1-10 分):
- 9-10: 深度研究報告，含獨特觀點、詳細財務模型、完整產業分析
- 7-8: 有實質分析內容，包含財務預測、產業比較
- 5-6: 一般性報告，資訊量中等
- 3-4: 內容偏淺，主要是新聞摘要
- 1-2: 幾乎無分析價值

請輸出以下欄位的 JSON（只輸出 JSON，不要 markdown code block，不要其他文字）：
{fields_spec}

---

報告文本:

{text}"""


async def _extract_async(text: str) -> dict:
    """透過 Claude Agent SDK 擷取結構化資料"""
    truncated = text[:15000] if len(text) > 15000 else text

    prompt = EXTRACTION_PROMPT.format(
        fields_spec=FIELDS_SPEC,
        text=truncated,
    )

    options = ClaudeAgentOptions(
        model=CONFIG["llm"]["claude_model"],
        max_turns=1,
        permission_mode="bypassPermissions",
        allowed_tools=[],  # 不需要使用任何工具
    )

    collected_text = []

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage) and message.result:
            collected_text.append(message.result)
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    collected_text.append(block.text)

    if not collected_text:
        raise ValueError("Claude Agent SDK 未回傳任何結果")

    raw = "\n".join(collected_text).strip()

    # 嘗試清理 markdown code block 包裹
    if raw.startswith("```"):
        lines = raw.split("\n")
        # 移除首行 ```json 和末行 ```
        lines = [l for l in lines if not l.strip().startswith("```")]
        raw = "\n".join(lines).strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("JSON 解析失敗，原始回傳:\n%s", raw[:500])
        raise ValueError(f"Claude 回傳的 JSON 無法解析: {e}") from e


def extract_report_data(text: str) -> dict:
    """呼叫 Claude Agent SDK 擷取結構化資料（同步包裝）"""
    return asyncio.run(_extract_async(text))
