# broker-reports 📈

台灣券商研究報告智能分析系統 — 自動蒐集、結構化擷取、MCP 查詢、盤中即時推送。

## 功能概覽

| 模組 | 說明 |
|------|------|
| **報告匯入** | 掃描 PDF / DOCX 券商報告，用 Claude AI 擷取結構化資訊（股票代號、評等、目標價、投資邏輯等） |
| **全文搜尋** | SQLite FTS5 全文索引，支援股票代號、券商、產業、評等、主題等多維度查詢 |
| **MCP Server** | 透過 [Model Context Protocol](https://modelcontextprotocol.io) 對外提供查詢 API，可供 Claude Desktop / Agent 直接呼叫 |
| **Telegram 同步** | 自動從 [tg-archiver](https://github.com/jwlhs104/tg-archiver) 拉取券商群組的新報告 |
| **盤中即時監控** | 偵測新報告 → Claude AI 研判投資價值 → Telegram 主動推送通知 |

## 資料規模

- **7,400+** 份已擷取的結構化報告
- **10+** 家券商來源（元富、群益、摩根士丹利、凱基、永豐、高盛...）
- **半導體、AI 伺服器、光通訊、IC 設計** 等重點產業覆蓋

## 快速開始

### 安裝

```bash
pip install -r requirements.txt
```

### 設定

編輯 `config.yaml`：

```yaml
paths:
  reports_dir: "./reports"       # 報告檔案目錄
  db_url: "sqlite:///reports.db" # 資料庫路徑

llm:
  claude_model: "claude-sonnet-4-20250514"

extraction:
  max_pages: 10
  batch_size: 5

# Telegram 整合（選配）
tg_archiver:
  db_path: "/path/to/tg-archiver/data/archiver.db"
  media_dir: "/path/to/tg-archiver/data/media"
  bot_token: ""          # Telegram Bot Token
  notify_chat_id: 0      # 你的 Telegram User ID
```

### 匯入報告

```bash
# 完整匯入（掃描檔名 + Claude AI 擷取結構化資訊）
python -m scripts.ingest_all

# 只掃描檔名，不呼叫 LLM
python -m scripts.ingest_all --scan-only

# 限制處理數量
python -m scripts.ingest_all --limit 10
```

### 搜尋報告

```bash
# 智能搜尋（主題 → 產業 → 關鍵字 → 全文）
python -m scripts.search_cli 光通訊

# 依股票代號
python -m scripts.search_cli --stock 3081

# 依券商
python -m scripts.search_cli --broker 凱基

# 組合查詢
python -m scripts.search_cli 光通訊 --rating 買進 --from 2025-01-01
```

### 啟動 MCP Server

```bash
# 預設 port 8100
python -m src.server
```

MCP 提供以下工具：

| Tool | 說明 |
|------|------|
| `search_broker_reports` | 多條件搜尋券商報告 |
| `get_report_detail` | 取得單份報告完整內容 |
| `compare_target_prices` | 比較各券商對同一檔股票的目標價 |
| `find_related_reports` | 尋找相關報告 |
| `get_stats` | 資料庫統計概覽 |

## Telegram 整合

### 同步券商群組報告

從 tg-archiver 拉取新蒐集的券商報告：

```bash
# 查看同步狀態
python -m scripts.sync_tg --status

# 執行同步
python -m scripts.sync_tg

# 同步但不擷取（只複製檔案）
python -m scripts.sync_tg --no-extract

# 試跑
python -m scripts.sync_tg --dry-run
```

### 盤中即時監控

新報告進來 → AI 研判投資價值 → 達標則 Telegram 推送：

```bash
# 盤中自動監控（08:30 - 14:00）
python -m scripts.watch_tg

# 全天候監控
python -m scripts.watch_tg --all-hours

# 只推送高優先度
python -m scripts.watch_tg --priority HIGH

# 調整輪詢間隔（秒）
python -m scripts.watch_tg --interval 60

# 測試模式（跑一次就停）
python -m scripts.watch_tg --once --reset --all-hours
```

**優先度判定：**
- 🔴 **HIGH** — 評等調升/調降、目標價大幅調整(>10%)、重大事件
- 🟡 **MEDIUM** — 有明確目標價與評等、產業趨勢分析、財報點評
- 🟢 **LOW** — 一般產業新聞、無明確建議

## 專案架構

```
broker-reports/
├── config.yaml              # 全域設定
├── requirements.txt
├── reports/                  # 券商報告檔案 (PDF/DOCX)
├── reports.db                # SQLite 資料庫 (含 FTS5 全文索引)
├── src/
│   ├── config.py             # 設定載入
│   ├── database.py           # SQLAlchemy ORM + FTS5
│   ├── models.py             # 資料模型
│   ├── filename_parser.py    # 檔名解析 (券商/日期/股票代號)
│   ├── pdf_parser.py         # PDF / DOCX 文字擷取
│   ├── extractor.py          # Claude AI 結構化擷取
│   ├── ingest.py             # 匯入流程編排
│   ├── search.py             # 多維度搜尋引擎
│   ├── server.py             # MCP Server (FastMCP)
│   ├── tg_sync.py            # tg-archiver 同步模組
│   ├── tg_watcher.py         # 盤中即時監控 daemon
│   └── _triage_subprocess.py # Claude AI 研判子進程
├── scripts/
│   ├── ingest_all.py         # CLI: 批次匯入
│   ├── search_cli.py         # CLI: 搜尋報告
│   ├── sync_tg.py            # CLI: Telegram 同步
│   └── watch_tg.py           # CLI: 盤中監控
```

## 技術棧

- **AI**: Claude Agent SDK / Anthropic API — 結構化資訊擷取 & 即時研判
- **資料庫**: SQLite + SQLAlchemy + FTS5 全文索引
- **文件解析**: pdfplumber (PDF) + python-docx (DOCX)
- **API**: FastMCP (Model Context Protocol)
- **通知**: Telegram Bot API

## License

MIT
