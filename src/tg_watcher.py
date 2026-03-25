"""盤中即時監控：偵測新券商報告 → AI 研判 → Telegram 主動推送

流程:
1. 每 N 秒輪詢 tg-archiver DB，偵測新文件與訊息
2. 擷取文字內容（PDF / DOCX / 純文字）
3. 用 Claude 快速研判：是否有投資價值、關鍵資訊摘要
4. 若有價值 → 透過 Telegram Bot 推送通知給使用者
5. 同步到 broker-reports DB 供後續深度查詢
"""

import json
import logging

# 在模組載入時快照原始環境（移除 Claude Code 標記）
# 這必須在任何其他模組可能清除 env 之前執行
import os as _os  # noqa: E402
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from rich.console import Console

from src.config import CONFIG
from src.pdf_parser import extract_text

_CLEAN_ENV = {k: v for k, v in _os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}

logger = logging.getLogger(__name__)
console = Console()

# ─── 快篩 Prompt ──────────────────────────────────────────

TRIAGE_PROMPT = """你是一個台股券商報告即時研判助手。請快速評估這份報告/訊息的投資價值。

## 評估標準

優先度判定：
- 🔴 HIGH: 評等調升/調降、目標價大幅調整(>10%)、重大事件(併購/財測上修/法說會亮點)、強烈買進/賣出建議
- 🟡 MEDIUM: 有明確目標價與評等、產業趨勢分析、法說會紀要、財報點評
- 🟢 LOW: 一般產業新聞、無明確建議、資訊量不足、非台股相關

## 輸出格式

請輸出 JSON（不要 markdown code block）：
{{
  "priority": "HIGH / MEDIUM / LOW",
  "is_actionable": true/false,
  "stock_code": "台股代號或null",
  "stock_name": "股票名稱或null",
  "broker": "券商名稱或null",
  "rating": "買進/持有/賣出/未評等/null",
  "target_price": 數字或null,
  "alert_title": "一行標題 (20字內，含股票名稱和關鍵動作)",
  "alert_summary": "核心重點摘要 (50-80字，要有具體數字和結論)",
  "reason": "推送理由 (30字內)"
}}

## 報告來源
群組: {chat_title}
檔名: {filename}

## 報告內容
{text}"""

TEXT_TRIAGE_PROMPT = """你是一個台股券商群組訊息即時研判助手。以下是券商群組中的文字訊息，請判斷是否包含有投資價值的即時資訊。

## 評估標準

優先度判定：
- 🔴 HIGH: 即時目標價調整、評等調升/調降、重大利多/利空消息、盤中異動提醒
- 🟡 MEDIUM: 個股點評、產業動態、法人動向、技術面突破
- 🟢 LOW: 閒聊、廣告、一般寒暄、無投資價值的討論

## 輸出格式

請輸出 JSON（不要 markdown code block）：
{{
  "priority": "HIGH / MEDIUM / LOW",
  "is_actionable": true/false,
  "stock_code": "台股代號或null",
  "stock_name": "股票名稱或null",
  "alert_title": "一行標題 (20字內)",
  "alert_summary": "重點摘要 (30-50字)",
  "reason": "推送理由 (20字內)"
}}

## 訊息來源
群組: {chat_title}
發送者: {sender_name}

## 訊息內容
{text}"""


# ─── Telegram 通知 ────────────────────────────────────────


def _send_telegram(bot_token: str, chat_id: int, text: str, parse_mode: str = "HTML"):
    """透過 Telegram Bot API 發送訊息"""
    import urllib.parse
    import urllib.request

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }
    ).encode("utf-8")

    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if not result.get("ok"):
                logger.error("Telegram API 錯誤: %s", result)
            return result
    except Exception as e:
        logger.error("Telegram 發送失敗: %s", e)
        return None


def _format_doc_alert(triage: dict, chat_title: str) -> str:
    """格式化文件報告通知訊息"""
    priority_emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(triage["priority"], "⚪")

    parts = [f"{priority_emoji} <b>{triage.get('alert_title', '新券商報告')}</b>"]

    # 股票資訊行
    info_parts = []
    if triage.get("stock_code"):
        info_parts.append(f"#{triage['stock_code']}")
    if triage.get("stock_name"):
        info_parts.append(triage["stock_name"])
    if triage.get("broker"):
        info_parts.append(f"({triage['broker']})")
    if info_parts:
        parts.append(" ".join(info_parts))

    # 評等與目標價
    rating_parts = []
    if triage.get("rating") and triage["rating"] != "null":
        rating_parts.append(f"評等: {triage['rating']}")
    if triage.get("target_price"):
        rating_parts.append(f"目標價: {triage['target_price']}")
    if rating_parts:
        parts.append(" | ".join(rating_parts))

    # 摘要
    if triage.get("alert_summary"):
        parts.append(f"\n{triage['alert_summary']}")

    # 來源
    parts.append(f"\n<i>📡 {chat_title}</i>")

    return "\n".join(parts)


def _format_text_alert(triage: dict, chat_title: str, sender_name: str) -> str:
    """格式化文字訊息通知"""
    priority_emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(triage["priority"], "⚪")

    parts = [f"{priority_emoji} <b>{triage.get('alert_title', '券商群組訊息')}</b>"]

    if triage.get("stock_code"):
        parts.append(f"#{triage['stock_code']} {triage.get('stock_name', '')}")

    if triage.get("alert_summary"):
        parts.append(f"\n{triage['alert_summary']}")

    parts.append(f"\n<i>📡 {chat_title} — {sender_name}</i>")

    return "\n".join(parts)


# ─── AI 研判 ──────────────────────────────────────────────


def _triage_with_claude(prompt: str, model: str = "claude-sonnet-4-20250514") -> dict | None:
    """用 Claude 快速研判報告價值（透過子進程避免巢狀 Agent SDK 問題）"""
    import subprocess as _sp  # noqa: F811
    import sys as _sys  # noqa: F811
    import tempfile as _tf  # noqa: F811

    project_root = Path(__file__).parent.parent
    input_file = ""
    output_file = ""

    try:
        # 建立暫存檔傳遞資料
        with _tf.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f_in:
            json.dump({"prompt": prompt, "model": model}, f_in, ensure_ascii=False)
            input_file = f_in.name

        output_file = input_file.replace(".json", "_out.json")

        # 在子進程中執行 triage（使用模組載入時快照的 clean env）
        result = _sp.run(
            [_sys.executable, "-m", "src._triage_subprocess", input_file, output_file],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=120,
            env=_CLEAN_ENV,
        )

        if result.returncode != 0:
            logger.error(
                "Triage 子進程失敗 (rc=%d): stdout=%s stderr=%s",
                result.returncode,
                result.stdout[:300],
                result.stderr[:300],
            )
            return None

        # 讀取結果
        with open(output_file, encoding="utf-8") as f:
            resp = json.load(f)

        if resp.get("ok"):
            return resp.get("result")
        else:
            logger.error("Triage 錯誤: %s", resp.get("error"))
            if resp.get("traceback"):
                console.print(f"[dim red]{resp['traceback']}[/dim red]")
            return None

    except _sp.TimeoutExpired:
        logger.error("Triage 子進程超時 (120s)")
        return None
    except Exception as e:
        logger.error("Claude triage 失敗: %s", e)
        return None
    finally:
        # 清理暫存檔
        for fpath in (input_file, output_file):
            try:
                if fpath:
                    Path(fpath).unlink(missing_ok=True)
            except Exception:
                pass


# ─── 核心 Watcher ─────────────────────────────────────────


class ReportWatcher:
    """盤中即時監控 daemon"""

    def __init__(
        self,
        min_priority: str = "MEDIUM",
        poll_interval: int = 30,
        also_watch_text: bool = True,
        triage_model: str = "claude-haiku-4-20250414",
    ):
        tg_config = CONFIG.get("tg_archiver", {})

        self.tg_db_path = tg_config.get("db_path", "")
        self.tg_media_dir = tg_config.get("media_dir", "")
        self.bot_token = tg_config.get("bot_token", "")
        self.notify_chat_id = tg_config.get("notify_chat_id", 0)
        self.reports_dir = CONFIG["paths"]["reports_dir"]

        self.min_priority = min_priority  # HIGH, MEDIUM, LOW
        self.poll_interval = poll_interval
        self.also_watch_text = also_watch_text
        self.triage_model = triage_model

        # 水位線：記錄上次處理到的 message id
        self._checkpoint_file = Path(CONFIG["paths"]["reports_dir"]).parent / ".watcher_checkpoint"
        self._last_id = self._load_checkpoint()

        # 統計
        self.stats = {"checked": 0, "triaged": 0, "notified": 0, "errors": 0}

    def _load_checkpoint(self) -> int:
        """載入上次處理到的位置"""
        if self._checkpoint_file.exists():
            try:
                return int(self._checkpoint_file.read_text().strip())
            except (ValueError, OSError):
                pass
        return 0

    def _save_checkpoint(self, msg_id: int):
        """儲存水位線"""
        self._checkpoint_file.write_text(str(msg_id))
        self._last_id = msg_id

    def _should_notify(self, priority: str) -> bool:
        """判斷是否達到推送門檻"""
        levels = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
        return levels.get(priority, 0) >= levels.get(self.min_priority, 2)

    def _query_new_messages(self) -> list[dict]:
        """從 tg-archiver 取得新訊息"""
        conn = sqlite3.connect(self.tg_db_path)
        conn.row_factory = sqlite3.Row
        try:
            if self.also_watch_text:
                query = """
                    SELECT id, message_id, chat_id, chat_title, date,
                           text, caption, message_type, sender_name,
                           media_local_path, media_mime_type
                    FROM messages
                    WHERE id > ?
                    ORDER BY id ASC
                """
            else:
                query = """
                    SELECT id, message_id, chat_id, chat_title, date,
                           text, caption, message_type, sender_name,
                           media_local_path, media_mime_type
                    FROM messages
                    WHERE id > ?
                      AND message_type = 'document'
                      AND media_mime_type IN ('application/pdf',
                          'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
                    ORDER BY id ASC
                """
            rows = conn.execute(query, (self._last_id,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _process_document(self, msg: dict) -> dict | None:
        """處理文件訊息：複製 + 擷取文字 + AI 研判"""
        media_path = Path(self.tg_media_dir) / msg["media_local_path"]
        if not media_path.exists():
            logger.warning("檔案不存在: %s", media_path)
            return None

        ext = media_path.suffix.lower()
        if ext not in (".pdf", ".docx"):
            return None

        # 複製到 reports/
        chat_id = abs(msg["chat_id"])
        dest_name = f"tg_{chat_id}_{msg['message_id']}_{media_path.stem[:120]}{ext}"
        dest_path = Path(self.reports_dir) / dest_name

        if not dest_path.exists():
            shutil.copy2(media_path, dest_path)

        # 擷取文字
        try:
            text_content, _ = extract_text(str(dest_path), max_pages=5)
        except Exception as e:
            logger.error("文字擷取失敗 %s: %s", dest_name, e)
            return None

        if len(text_content) < 50:
            logger.info("文字太少，跳過: %s", dest_name)
            return None

        # 截斷送 triage (用較短文字加速)
        truncated = text_content[:5000]

        prompt = TRIAGE_PROMPT.format(
            chat_title=msg.get("chat_title", ""),
            filename=media_path.name,
            text=truncated,
        )

        triage = _triage_with_claude(prompt, model=self.triage_model)
        if not triage:
            return None

        triage["_msg"] = msg
        triage["_type"] = "document"
        return triage

    def _process_text_message(self, msg: dict) -> dict | None:
        """處理純文字訊息：AI 快速研判"""
        text = msg.get("text") or msg.get("caption") or ""
        if len(text) < 20:
            return None

        prompt = TEXT_TRIAGE_PROMPT.format(
            chat_title=msg.get("chat_title", ""),
            sender_name=msg.get("sender_name", ""),
            text=text[:3000],
        )

        triage = _triage_with_claude(prompt, model=self.triage_model)
        if not triage:
            return None

        triage["_msg"] = msg
        triage["_type"] = "text"
        return triage

    def _notify(self, triage: dict):
        """發送 Telegram 通知"""
        msg = triage["_msg"]

        if triage["_type"] == "document":
            alert_text = _format_doc_alert(triage, msg.get("chat_title", ""))
        else:
            alert_text = _format_text_alert(triage, msg.get("chat_title", ""), msg.get("sender_name", ""))

        result = _send_telegram(self.bot_token, self.notify_chat_id, alert_text)
        if result and result.get("ok"):
            self.stats["notified"] += 1
            console.print(f"[green]📤 已推送: {triage.get('alert_title', '')}[/green]")
        else:
            console.print("[red]❌ 推送失敗[/red]")

    def poll_once(self) -> int:
        """執行一次輪詢，回傳處理的訊息數"""
        new_msgs = self._query_new_messages()
        if not new_msgs:
            return 0

        processed = 0
        for msg in new_msgs:
            self.stats["checked"] += 1
            msg_type = msg["message_type"]

            try:
                triage = None

                if msg_type == "document" and msg.get("media_mime_type") in (
                    "application/pdf",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ):
                    console.print(f"[cyan]📄 新文件: {msg.get('media_local_path', '')[:60]}[/cyan]")
                    triage = self._process_document(msg)

                elif msg_type == "text" and self.also_watch_text:
                    text = msg.get("text", "")
                    if len(text) >= 20:
                        console.print(f"[cyan]💬 新訊息: {text[:40]}...[/cyan]")
                        triage = self._process_text_message(msg)

                if triage:
                    self.stats["triaged"] += 1
                    priority = triage.get("priority", "LOW")
                    is_actionable = triage.get("is_actionable", False)

                    console.print(f"  → 優先度: {priority} | 可操作: {is_actionable} | {triage.get('alert_title', '')}")

                    if is_actionable and self._should_notify(priority):
                        self._notify(triage)

            except Exception as e:
                self.stats["errors"] += 1
                import traceback

                console.print(f"[red]處理訊息 #{msg['id']} 失敗: {e}[/red]")
                console.print(f"[dim red]{traceback.format_exc()}[/dim red]")

            # 更新水位線
            self._save_checkpoint(msg["id"])
            processed += 1

        return processed

    def run(self, market_hours_only: bool = True):
        """啟動監控 daemon

        Args:
            market_hours_only: 只在盤中時間 (08:30-14:00) 運作
        """
        console.print("[bold green]🚀 盤中監控啟動[/bold green]")
        console.print(f"  輪詢間隔: {self.poll_interval}s")
        console.print(f"  推送門檻: {self.min_priority}")
        console.print(f"  監控文字訊息: {self.also_watch_text}")
        console.print(f"  通知對象: {self.notify_chat_id}")
        console.print(f"  水位線: 從 ID #{self._last_id} 開始")
        console.print(f"  研判模型: {self.triage_model}")
        console.print()

        try:
            while True:
                now = datetime.now()

                # 盤中時間檢查 (08:30 ~ 14:00 台灣時間)
                if market_hours_only:
                    market_open = now.replace(hour=8, minute=30, second=0)
                    market_close = now.replace(hour=14, minute=0, second=0)

                    if now < market_open or now > market_close:
                        # 非盤中時間，等待到下一個開盤
                        if now > market_close:
                            next_open = (now + timedelta(days=1)).replace(hour=8, minute=30, second=0)
                        else:
                            next_open = market_open
                        wait = (next_open - now).total_seconds()
                        console.print(f"[yellow]⏸ 非盤中時間，{next_open.strftime('%H:%M')} 繼續監控[/yellow]")
                        time.sleep(min(wait, 300))  # 最多等 5 分鐘再檢查
                        continue

                count = self.poll_once()
                if count > 0:
                    console.print(
                        f"[dim]  📊 已檢查:{self.stats['checked']} "
                        f"研判:{self.stats['triaged']} "
                        f"推送:{self.stats['notified']} "
                        f"錯誤:{self.stats['errors']}[/dim]"
                    )
                else:
                    ts = now.strftime("%H:%M:%S")
                    console.print(f"[dim]{ts} 無新訊息[/dim]")

                time.sleep(self.poll_interval)

        except KeyboardInterrupt:
            console.print("\n[bold yellow]⏹ 監控停止[/bold yellow]")
            self._print_stats()

    def _print_stats(self):
        """印出統計數據"""
        console.print("\n[bold]📊 本次監控統計:[/bold]")
        console.print(f"  檢查訊息: {self.stats['checked']}")
        console.print(f"  AI 研判:  {self.stats['triaged']}")
        console.print(f"  推送通知: {self.stats['notified']}")
        console.print(f"  錯誤:     {self.stats['errors']}")
