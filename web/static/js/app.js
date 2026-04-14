/**
 * ANALYST — Broker Report Research Terminal
 * Chat UI with RAG source citations + conversation history
 */

const $messages = document.getElementById("messages");
const $input = document.getElementById("user-input");
const $sendBtn = document.getElementById("send-btn");
const $sidebar = document.getElementById("sidebar");
const $toggleSidebar = document.getElementById("toggle-sidebar");
const $stockFilter = document.getElementById("stock-filter");
const $statsBadge = document.getElementById("stats-badge");
const $popover = document.getElementById("source-popover");
const $convList = document.getElementById("conversation-list");
const $newChatBtn = document.getElementById("new-chat-btn");
const $sidebarBackdrop = document.getElementById("sidebar-backdrop");

// ══════════════════════════════════════════════════════════
//  Conversation State
// ══════════════════════════════════════════════════════════
const STORAGE_KEY = "analyst_conversations";

let conversations = [];   // [{id, title, preview, updatedAt, messages[]}]
let activeConvId = null;   // 目前對話 ID
let chatHistory = [];      // 送給 API 的 history [{role, content}]
let currentSources = {};   // 顯示用 {msgIdx: [sources]}
let messageIndex = 0;

// ── Init ──
loadConversations();
loadStats();
renderConversationList();
if (conversations.length > 0) {
    switchConversation(conversations[0].id);
} else {
    startNewChat();
}

// ══════════════════════════════════════════════════════════
//  Event Listeners
// ══════════════════════════════════════════════════════════
$sendBtn.addEventListener("click", sendMessage);
$input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});
$input.addEventListener("input", autoResize);
$toggleSidebar.addEventListener("click", toggleSidebar);
$newChatBtn.addEventListener("click", () => { startNewChat(); closeSidebarOnMobile(); });
if ($sidebarBackdrop) $sidebarBackdrop.addEventListener("click", closeSidebarOnMobile);
const $sidebarClose = document.getElementById("sidebar-close");
if ($sidebarClose) $sidebarClose.addEventListener("click", closeSidebarOnMobile);

document.addEventListener("click", (e) => {
    if (!e.target.closest(".source-ref") && !e.target.closest(".source-chip") && !e.target.closest("#source-popover")) {
        hidePopover();
    }
});

window.addEventListener("beforeunload", () => saveCurrentConversation());

// ══════════════════════════════════════════════════════════
//  Sidebar (Mobile)
// ══════════════════════════════════════════════════════════
function isMobile() { return window.innerWidth <= 900; }

function toggleSidebar() {
    $sidebar.classList.toggle("collapsed");
    if ($sidebarBackdrop) {
        $sidebarBackdrop.classList.toggle("active", !$sidebar.classList.contains("collapsed") && isMobile());
    }
}

function closeSidebarOnMobile() {
    if (isMobile()) {
        $sidebar.classList.add("collapsed");
        if ($sidebarBackdrop) $sidebarBackdrop.classList.remove("active");
    }
}

// On mobile, start with sidebar collapsed
if (isMobile()) {
    $sidebar.classList.add("collapsed");
}

// ══════════════════════════════════════════════════════════
//  Conversation Management (localStorage)
// ══════════════════════════════════════════════════════════
function generateId() {
    return Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

function loadConversations() {
    try {
        const raw = localStorage.getItem(STORAGE_KEY);
        if (!raw) { conversations = []; return; }
        const parsed = JSON.parse(raw);
        if (!Array.isArray(parsed)) { conversations = []; return; }
        conversations = parsed;
        conversations.sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
        console.log(`[ANALYST] Loaded ${conversations.length} conversations from localStorage`);
    } catch (e) {
        console.error("[ANALYST] Failed to load conversations:", e);
        conversations = [];
    }
}

/** 精簡 sources 以節省 localStorage 空間 */
function trimSources(sources) {
    if (!Array.isArray(sources)) return [];
    return sources.map(s => ({
        id: s.id,
        report_id: s.report_id,
        broker: s.broker || "",
        date: s.date || "",
        stock_code: s.stock_code || "",
        stock_name: s.stock_name || "",
        rating: s.rating || null,
        target_price: s.target_price || null,
        excerpt: (s.excerpt || "").slice(0, 200),
    }));
}

function saveConversations() {
    try {
        const json = JSON.stringify(conversations);
        localStorage.setItem(STORAGE_KEY, json);
        // 驗證存檔成功
        const check = localStorage.getItem(STORAGE_KEY);
        if (!check || check.length < json.length * 0.9) {
            console.warn("[ANALYST] Save verification warning: stored size mismatch");
        }
        return true;
    } catch (e) {
        console.error("[ANALYST] Failed to save conversations:", e);
        // 嘗試清理舊對話以釋放空間
        if (e.name === "QuotaExceededError" && conversations.length > 3) {
            console.warn("[ANALYST] Quota exceeded, removing oldest conversations...");
            conversations = conversations.slice(0, Math.max(3, Math.floor(conversations.length / 2)));
            try {
                localStorage.setItem(STORAGE_KEY, JSON.stringify(conversations));
                return true;
            } catch (e2) {
                console.error("[ANALYST] Still failed after trimming:", e2);
            }
        }
        return false;
    }
}

function getConv(id) {
    return conversations.find(c => c.id === id);
}

function startNewChat() {
    saveCurrentConversation();
    const conv = {
        id: generateId(),
        title: "新對話",
        preview: "",
        updatedAt: Date.now(),
        messages: [],
    };
    conversations.unshift(conv);
    saveConversations();
    activeConvId = conv.id;
    chatHistory = [];
    currentSources = {};
    messageIndex = 0;
    $messages.innerHTML = "";
    showWelcome();
    renderConversationList();
    console.log(`[ANALYST] New chat created: ${conv.id}`);
}

function switchConversation(id) {
    console.log(`[ANALYST] Switching to conversation: ${id}`);
    closeSidebarOnMobile();

    // 先儲存當前對話
    if (activeConvId && activeConvId !== id) {
        saveCurrentConversation();
    }

    activeConvId = id;
    const conv = getConv(id);
    if (!conv) {
        console.error(`[ANALYST] Conversation not found: ${id}`);
        return;
    }

    // 相容舊格式
    const msgs = conv.messages || conv.history || [];
    console.log(`[ANALYST] Restoring ${msgs.length} messages`);

    // 重建 chatHistory
    chatHistory = msgs.map(m => ({ role: m.role, content: m.content }));
    currentSources = {};
    messageIndex = 0;

    // 清空訊息區
    $messages.innerHTML = "";

    if (msgs.length === 0) {
        showWelcome();
    } else {
        try {
            for (const msg of msgs) {
                if (msg.role === "user") {
                    appendMessageRaw("user", escapeHtml(msg.content));
                } else if (msg.role === "assistant") {
                    const msgIdx = messageIndex;
                    const srcs = msg.sources || [];
                    if (srcs.length > 0) {
                        currentSources[msgIdx] = srcs;
                    }
                    const html = processSourceRefs(msg.content || "", msgIdx);
                    const el = appendMessageRaw("assistant", html);
                    // 渲染 sources bar
                    if (srcs.length > 0 && el) {
                        const contentEl = el.querySelector(".message-content");
                        if (contentEl) {
                            const bar = document.createElement("div");
                            bar.className = "sources-bar";
                            bar.innerHTML = srcs.map(s => `
                                <span class="source-chip" data-msg="${msgIdx}" data-src="${s.id}"
                                      onmouseenter="showPopoverForSource(event, ${msgIdx}, ${s.id})"
                                      onmouseleave="scheduleHidePopover()"
                                      onclick="openSourceReport(${msgIdx}, ${s.id})">
                                    <span class="chip-num">[${s.id}]</span>
                                    ${escapeHtml(s.broker)} ${escapeHtml(s.date)}
                                </span>
                            `).join("");
                            contentEl.appendChild(bar);
                        }
                    }
                }
            }
            console.log(`[ANALYST] Restored ${msgs.length} messages, messageIndex=${messageIndex}`);
        } catch (e) {
            console.error("[ANALYST] Error restoring messages:", e);
            $messages.innerHTML = `<div class="message assistant"><div class="message-content">
                <p style="color:var(--red)">恢復對話時發生錯誤。</p>
            </div></div>`;
        }
    }

    renderConversationList();
    $messages.scrollTop = $messages.scrollHeight;
}

function saveCurrentConversation() {
    if (!activeConvId) return;
    const conv = getConv(activeConvId);
    if (!conv) return;
    conv.updatedAt = Date.now();
    saveConversations();
}

function deleteConversation(id, e) {
    e.stopPropagation();
    conversations = conversations.filter(c => c.id !== id);
    saveConversations();
    if (activeConvId === id) {
        if (conversations.length > 0) {
            switchConversation(conversations[0].id);
        } else {
            startNewChat();
        }
    }
    renderConversationList();
}

function updateConversationMeta(question, answer) {
    const conv = getConv(activeConvId);
    if (!conv) return;
    if (conv.title === "新對話" && question) {
        conv.title = question.length > 30 ? question.slice(0, 30) + "…" : question;
    }
    if (answer) {
        const plain = answer.replace(/[#*_`>\[\]]/g, "").trim();
        conv.preview = plain.length > 60 ? plain.slice(0, 60) + "…" : plain;
    }
    conv.updatedAt = Date.now();
    // 移到最前面
    conversations = conversations.filter(c => c.id !== conv.id);
    conversations.unshift(conv);
    saveConversations();
    renderConversationList();
}

// ══════════════════════════════════════════════════════════
//  Render Conversation List
// ══════════════════════════════════════════════════════════
function renderConversationList() {
    if (conversations.length === 0) {
        $convList.innerHTML = `<div class="conv-empty">尚無對話紀錄</div>`;
        return;
    }

    const now = new Date();
    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const yesterdayStart = todayStart - 86400000;

    const groups = { today: [], yesterday: [], older: [] };
    for (const c of conversations) {
        const t = c.updatedAt || 0;
        if (t >= todayStart) groups.today.push(c);
        else if (t >= yesterdayStart) groups.yesterday.push(c);
        else groups.older.push(c);
    }

    let html = "";
    if (groups.today.length) {
        html += `<div class="conv-group-label">今天</div>`;
        html += groups.today.map(c => convItemHtml(c)).join("");
    }
    if (groups.yesterday.length) {
        html += `<div class="conv-group-label">昨天</div>`;
        html += groups.yesterday.map(c => convItemHtml(c)).join("");
    }
    if (groups.older.length) {
        html += `<div class="conv-group-label">更早</div>`;
        html += groups.older.map(c => convItemHtml(c)).join("");
    }

    $convList.innerHTML = html;
}

function convItemHtml(conv) {
    const active = conv.id === activeConvId ? "active" : "";
    const allMsgs = conv.messages || conv.history || [];
    const msgCount = Math.floor(allMsgs.length / 2);
    const timeStr = formatRelativeTime(conv.updatedAt);
    return `
    <div class="conv-item ${active}" onclick="switchConversation('${conv.id}')">
        <div class="conv-item-main">
            <div class="conv-item-title">${escapeHtml(conv.title)}</div>
            <div class="conv-item-preview">${escapeHtml(conv.preview || "")}</div>
        </div>
        <div class="conv-item-meta">
            <span class="conv-item-time">${timeStr}</span>
            <span class="conv-item-count">${msgCount} 輪</span>
        </div>
        <button class="conv-delete-btn" onclick="deleteConversation('${conv.id}', event)" title="刪除">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
    </div>`;
}

function formatRelativeTime(ts) {
    if (!ts) return "";
    const diff = Date.now() - ts;
    if (diff < 60000) return "剛剛";
    if (diff < 3600000) return Math.floor(diff / 60000) + " 分鐘前";
    if (diff < 86400000) return Math.floor(diff / 3600000) + " 小時前";
    if (diff < 604800000) return Math.floor(diff / 86400000) + " 天前";
    return new Date(ts).toLocaleDateString("zh-TW");
}

// ══════════════════════════════════════════════════════════
//  Welcome Screen
// ══════════════════════════════════════════════════════════
function showWelcome() {
    const div = document.createElement("div");
    div.className = "message assistant welcome-msg";
    div.innerHTML = `
        <div class="message-content">
            <div class="welcome-hero">
                <span class="welcome-tag">AI-Powered Research</span>
                <h2>券商報告<br><em>研究助手</em></h2>
                <p>8,000+ 份台股券商報告即時分析，每個回答都附帶原始來源。</p>
            </div>
            <div class="welcome-prompts">
                <button class="prompt-chip" onclick="fillPrompt('台積電目前各家券商的看法？')">
                    <span class="chip-icon">📊</span>
                    台積電券商共識
                </button>
                <button class="prompt-chip" onclick="fillPrompt('AI 伺服器產業鏈有哪些重點？')">
                    <span class="chip-icon">🔗</span>
                    AI 產業鏈分析
                </button>
                <button class="prompt-chip" onclick="fillPrompt('比較 2330 和 2454 的目標價')">
                    <span class="chip-icon">⚖️</span>
                    個股目標價比較
                </button>
            </div>
        </div>`;
    $messages.appendChild(div);
}

// ══════════════════════════════════════════════════════════
//  Helpers
// ══════════════════════════════════════════════════════════
function escapeHtml(str) {
    if (!str) return "";
    return str.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

function fillPrompt(text) {
    $input.value = text;
    $input.focus();
    autoResize();
    sendMessage();
}

function autoResize() {
    $input.style.height = "auto";
    $input.style.height = Math.min($input.scrollHeight, 120) + "px";
}

async function loadStats() {
    try {
        const res = await fetch("/api/stats");
        const data = await res.json();
        $statsBadge.textContent = `${data.done.toLocaleString()} reports`;
    } catch {
        $statsBadge.textContent = "OFFLINE";
    }
}

// ══════════════════════════════════════════════════════════
//  Send Message
// ══════════════════════════════════════════════════════════
async function sendMessage() {
    const question = $input.value.trim();
    if (!question) return;

    // 移除 welcome
    const welcome = document.querySelector(".welcome-msg");
    if (welcome) {
        welcome.style.transition = "opacity 0.3s, transform 0.3s";
        welcome.style.opacity = "0";
        welcome.style.transform = "translateY(-10px)";
        setTimeout(() => welcome.remove(), 300);
    }

    $input.value = "";
    $input.style.height = "auto";
    $sendBtn.disabled = true;

    // ★ 記住發送時的對話 ID 和 history snapshot
    const sendConvId = activeConvId;
    const historySnapshot = chatHistory.slice(-6);

    appendMessage("user", escapeHtml(question));

    // ★ 立即將 user 訊息存入 conv.messages + chatHistory
    chatHistory.push({ role: "user", content: question });
    const sendConv = getConv(sendConvId);
    if (sendConv) {
        if (!sendConv.messages) sendConv.messages = [];
        sendConv.messages.push({ role: "user", content: question });
        if (sendConv.title === "新對話") {
            sendConv.title = question.length > 30 ? question.slice(0, 30) + "…" : question;
        }
        sendConv.updatedAt = Date.now();
        saveConversations();
        renderConversationList();
    }

    // 建立 assistant 訊息容器（用於串流填充）
    const msgEl = document.createElement("div");
    msgEl.className = "message assistant";
    const contentEl = document.createElement("div");
    contentEl.className = "message-content";
    contentEl.innerHTML = `<div class="stream-status" style="color:var(--text-muted);font-size:13px;display:flex;align-items:center;gap:8px">
        <div class="typing-indicator" style="display:inline-flex"><span></span><span></span><span></span></div>
        <span>正在分析問題...</span>
    </div>
    <div class="stream-text"></div>`;
    msgEl.appendChild(contentEl);
    $messages.appendChild(msgEl);
    $messages.scrollTop = $messages.scrollHeight;

    const statusEl = contentEl.querySelector(".stream-status");
    const textEl = contentEl.querySelector(".stream-text");
    const msgIdx = messageIndex;
    messageIndex++;

    let rawAnswer = "";
    let sources = [];

    try {
        const res = await fetch("/api/chat/stream", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                question,
                stock_code: $stockFilter.value.trim() || null,
                history: historySnapshot,
            }),
        });

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let sseBuffer = "";

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            sseBuffer += decoder.decode(value, { stream: true });

            // 解析 SSE events（以 \n\n 分隔）
            const events = sseBuffer.split("\n\n");
            sseBuffer = events.pop(); // 最後一段可能不完整，保留

            for (const eventBlock of events) {
                if (!eventBlock.trim()) continue;

                let eventType = "";
                let eventData = "";

                for (const line of eventBlock.split("\n")) {
                    if (line.startsWith("event: ")) {
                        eventType = line.slice(7);
                    } else if (line.startsWith("data: ")) {
                        eventData += (eventData ? "\n" : "") + line.slice(6);
                    }
                }

                switch (eventType) {
                    case "status":
                        if (eventData) {
                            statusEl.querySelector("span:last-child").textContent = eventData;
                            statusEl.style.display = "flex";
                        } else {
                            statusEl.style.display = "none";
                        }
                        break;

                    case "sources_preview":
                        // 提前收到報告 metadata
                        try {
                            sources = JSON.parse(eventData);
                            currentSources[msgIdx] = sources;
                        } catch {}
                        break;

                    case "chunk":
                        rawAnswer += eventData;
                        // 即時渲染 markdown（每個 chunk 都重新 parse 整段）
                        if (activeConvId === sendConvId) {
                            textEl.innerHTML = processSourceRefs(rawAnswer, msgIdx);
                            $messages.scrollTop = $messages.scrollHeight;
                        }
                        break;

                    case "sources":
                        // 最終精確的 sources（含 excerpt）
                        try {
                            sources = JSON.parse(eventData);
                            currentSources[msgIdx] = sources;
                        } catch {}
                        break;

                    case "done":
                        break;
                }
            }
        }

        // ── 串流結束：最終渲染 ──
        statusEl.remove();

        // ★ 存到正確的對話
        const targetConv = getConv(sendConvId);
        if (targetConv) {
            if (!targetConv.messages) targetConv.messages = [];
            targetConv.messages.push({
                role: "assistant",
                content: rawAnswer,
                sources: trimSources(sources),
            });
            const plain = rawAnswer.replace(/[#*_`>\[\]]/g, "").trim();
            targetConv.preview = plain.length > 60 ? plain.slice(0, 60) + "…" : plain;
            targetConv.updatedAt = Date.now();
            conversations = conversations.filter(c => c.id !== targetConv.id);
            conversations.unshift(targetConv);
            saveConversations();
        }

        if (activeConvId === sendConvId) {
            // 最終重新渲染（確保 markdown 完整 parse）
            textEl.innerHTML = processSourceRefs(rawAnswer, msgIdx);

            // 渲染 source chips
            if (sources.length > 0) {
                const sourcesBar = document.createElement("div");
                sourcesBar.className = "sources-bar";
                sourcesBar.innerHTML = sources.map(s => `
                    <span class="source-chip" data-msg="${msgIdx}" data-src="${s.id}"
                          onmouseenter="showPopoverForSource(event, ${msgIdx}, ${s.id})"
                          onmouseleave="scheduleHidePopover()"
                          onclick="openSourceReport(${msgIdx}, ${s.id})">
                        <span class="chip-num">[${s.id}]</span>
                        ${escapeHtml(s.broker)} ${escapeHtml(s.date)}
                    </span>
                `).join("");
                contentEl.appendChild(sourcesBar);
            }

            chatHistory.push({ role: "assistant", content: rawAnswer });
            renderConversationList();
            $messages.scrollTop = $messages.scrollHeight;
        } else {
            renderConversationList();
        }

    } catch (e) {
        statusEl?.remove();
        if (activeConvId === sendConvId) {
            textEl.innerHTML = "抱歉，發生錯誤。請稍後再試。";
        }
        console.error("[ANALYST] sendMessage stream error:", e);
    }

    $sendBtn.disabled = false;
    $input.focus();
}

function appendMessage(role, content) {
    const div = document.createElement("div");
    div.className = `message ${role}`;
    div.innerHTML = `<div class="message-content">${content}</div>`;
    $messages.appendChild(div);
    $messages.scrollTop = $messages.scrollHeight;
    if (role === "assistant") messageIndex++;
    return div;
}

function appendMessageRaw(role, content) {
    const div = document.createElement("div");
    div.className = `message ${role}`;
    div.style.animation = "none";
    div.style.opacity = "1";
    div.innerHTML = `<div class="message-content">${content}</div>`;
    $messages.appendChild(div);
    if (role === "assistant") messageIndex++;
    return div;
}

// ══════════════════════════════════════════════════════════
//  Process Markdown + [n] references
// ══════════════════════════════════════════════════════════
function processSourceRefs(text, msgIdx) {
    if (!text) return "";

    try {
        const placeholders = {};
        let safe = text.replace(/\[(\d+)\]/g, (match, num) => {
            const key = `%%SRC_${num}%%`;
            placeholders[key] = parseInt(num);
            return key;
        });

        if (typeof marked !== "undefined" && marked.parse) {
            marked.setOptions({
                breaks: true,
                gfm: true,
                headerIds: false,
                mangle: false,
            });
            let html = marked.parse(safe);

            html = html.replace(/%%SRC_(\d+)%%/g, (match, num) => {
                return `<span class="source-ref"
                              data-msg="${msgIdx}" data-src="${num}"
                              onmouseenter="showPopoverForSource(event, ${msgIdx}, ${parseInt(num)})"
                              onmouseleave="scheduleHidePopover()"
                              onclick="openSourceReport(${msgIdx}, ${parseInt(num)})">${num}</span>`;
            });

            return html;
        } else {
            // marked 未載入時的 fallback
            console.warn("[ANALYST] marked.js not loaded, using plain text fallback");
            let html = escapeHtml(text);
            html = html.replace(/\[(\d+)\]/g, (match, num) => {
                return `<span class="source-ref"
                              data-msg="${msgIdx}" data-src="${num}"
                              onmouseenter="showPopoverForSource(event, ${msgIdx}, ${parseInt(num)})"
                              onmouseleave="scheduleHidePopover()"
                              onclick="openSourceReport(${msgIdx}, ${parseInt(num)})">${num}</span>`;
            });
            return `<p>${html.replace(/\n/g, "<br>")}</p>`;
        }
    } catch (e) {
        console.error("[ANALYST] processSourceRefs error:", e);
        return `<p>${escapeHtml(text)}</p>`;
    }
}

// ══════════════════════════════════════════════════════════
//  Popover
// ══════════════════════════════════════════════════════════
let popoverTimeout = null;

function showPopoverForSource(event, msgIdx, srcId) {
    clearTimeout(popoverTimeout);

    const sources = currentSources[msgIdx] || [];
    const source = sources.find(s => s.id === srcId);
    if (!source) {
        console.warn(`[ANALYST] Source not found: msgIdx=${msgIdx}, srcId=${srcId}`, currentSources);
        return;
    }

    $popover.querySelector(".popover-broker").textContent = source.broker;
    $popover.querySelector(".popover-date").textContent = source.date;
    $popover.querySelector(".popover-stock").textContent = `${source.stock_code} ${source.stock_name}`;

    const ratingEl = $popover.querySelector(".popover-rating");
    ratingEl.textContent = source.rating ? `評等：${source.rating}` : "";
    ratingEl.style.color = source.rating === "買進" ? "var(--green)" :
                           source.rating === "賣出" ? "var(--red)" : "var(--amber)";

    $popover.querySelector(".popover-tp").textContent = source.target_price ? `TP $${source.target_price}` : "";
    $popover.querySelector(".popover-excerpt").textContent = source.excerpt || source.summary || "無摘要";

    const link = $popover.querySelector(".popover-detail-link");
    link.onclick = (e) => {
        e.preventDefault();
        showReportModal(source.report_id);
    };

    const rect = event.target.getBoundingClientRect();
    let top = rect.bottom + 10;
    let left = rect.left - 180;
    if (left < 10) left = 10;
    if (left + 400 > window.innerWidth) left = window.innerWidth - 410;
    if (top + 320 > window.innerHeight) top = rect.top - 330;

    $popover.style.top = top + "px";
    $popover.style.left = left + "px";
    $popover.classList.remove("hidden");
}

function scheduleHidePopover() {
    popoverTimeout = setTimeout(() => hidePopover(), 300);
}

function hidePopover() {
    $popover.classList.add("hidden");
}

$popover.addEventListener("mouseenter", () => clearTimeout(popoverTimeout));
$popover.addEventListener("mouseleave", () => scheduleHidePopover());

/** 點擊 source-ref 或 source-chip → 直接開啟報告 modal */
function openSourceReport(msgIdx, srcId) {
    const sources = currentSources[msgIdx] || [];
    const source = sources.find(s => s.id === srcId);
    if (source && source.report_id) {
        hidePopover();
        showReportModal(source.report_id);
    }
}

// ══════════════════════════════════════════════════════════
//  Report Detail Modal
// ══════════════════════════════════════════════════════════
function ensureModal() {
    let modal = document.getElementById("report-modal");
    if (modal) return modal;
    modal = document.createElement("div");
    modal.id = "report-modal";
    modal.className = "modal-overlay hidden";
    modal.innerHTML = `<div class="modal-content"><div class="modal-inner"></div></div>`;
    document.body.appendChild(modal);
    modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });
    return modal;
}

function ratingBadge(rating) {
    if (!rating) return '<span class="modal-badge neutral">未評等</span>';
    if (rating === "買進") return '<span class="modal-badge buy">BUY</span>';
    if (rating === "賣出") return '<span class="modal-badge sell">SELL</span>';
    return `<span class="modal-badge hold">${rating}</span>`;
}

function formatRawText(text) {
    if (!text) return "<p>無原文內容</p>";
    return text.split(/\n{2,}/).map(para => {
        const trimmed = para.trim();
        if (!trimmed) return "";
        if (trimmed.includes("\t") || /\s{3,}/.test(trimmed)) {
            return `<pre class="raw-table">${trimmed.replace(/</g,"&lt;")}</pre>`;
        }
        const lines = trimmed.split("\n");
        if (lines.length === 1 && lines[0].length < 30 && !/[。，、；：]/.test(lines[0])) {
            return `<h4 class="raw-heading">${lines[0].replace(/</g,"&lt;")}</h4>`;
        }
        return `<p>${trimmed.replace(/</g,"&lt;").replace(/\n/g,"<br>")}</p>`;
    }).join("");
}

async function showReportModal(reportId) {
    hidePopover();
    const modal = ensureModal();
    const inner = modal.querySelector(".modal-inner");

    modal.classList.remove("hidden");
    inner.innerHTML = `
        <div style="text-align:center;padding:60px;color:var(--text-muted)">
            <div class="typing-indicator" style="justify-content:center"><span></span><span></span><span></span></div>
            <div style="margin-top:16px;font-size:13px;letter-spacing:1px">LOADING REPORT</div>
        </div>`;

    try {
        const res = await fetch(`/api/report/${reportId}`);
        const r = await res.json();
        if (r.error) throw new Error(r.error);

        const topics = (r.topics || []).map(t => `<span class="modal-topic">${t}</span>`).join("");
        const tpDisplay = r.target_price ? `$${r.target_price.toLocaleString()}` : "—";

        inner.innerHTML = `
            <button class="modal-close" onclick="closeModal()">&times;</button>

            <div class="modal-hero">
                <div class="modal-hero-left">
                    <div class="modal-stock-code">${r.stock_code}</div>
                    <div class="modal-stock-name">${r.stock_name || ""}</div>
                </div>
                <div class="modal-hero-right">
                    <div class="modal-broker-name">${r.broker}</div>
                    <div class="modal-report-date">${r.date}</div>
                </div>
            </div>

            <div class="modal-kpi-row">
                <div class="modal-kpi-card">
                    <div class="kpi-label">Rating</div>
                    <div class="kpi-value">${ratingBadge(r.rating)}</div>
                </div>
                <div class="modal-kpi-card">
                    <div class="kpi-label">Target Price</div>
                    <div class="kpi-value kpi-tp">${tpDisplay}</div>
                </div>
                <div class="modal-kpi-card">
                    <div class="kpi-label">Quality Score</div>
                    <div class="kpi-value">
                        <span class="kpi-score">${r.quality_score || "—"}</span>
                        <span class="kpi-score-max">/ 10</span>
                    </div>
                </div>
            </div>

            <div class="modal-section">
                <div class="modal-section-title">Summary</div>
                <div class="modal-section-body modal-summary">${r.summary || "無摘要"}</div>
            </div>

            ${r.investment_thesis ? `
            <div class="modal-section">
                <div class="modal-section-title">Investment Thesis</div>
                <div class="modal-section-body modal-thesis">${r.investment_thesis}</div>
            </div>` : ""}

            ${topics ? `
            <div class="modal-section">
                <div class="modal-section-title">Topics</div>
                <div class="modal-topics-row">${topics}</div>
            </div>` : ""}

            <div class="modal-section">
                <div class="modal-tabs">
                    ${r.has_pdf ? `<button class="modal-tab active" data-tab="pdf" onclick="switchTab(this, 'pdf')">PDF Document</button>` : ""}
                    <button class="modal-tab ${r.has_pdf ? '' : 'active'}" data-tab="text" onclick="switchTab(this, 'text')">Extracted Text</button>
                </div>
                ${r.has_pdf ? `
                <div class="modal-tab-content tab-pdf active">
                    <div class="pdf-container">
                        <div class="pdf-toolbar">
                            <button class="pdf-nav-btn" onclick="pdfPrevPage()" title="上一頁">
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"/></svg>
                            </button>
                            <span class="pdf-page-info" id="pdf-page-info">- / -</span>
                            <button class="pdf-nav-btn" onclick="pdfNextPage()" title="下一頁">
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>
                            </button>
                            <div style="flex:1"></div>
                            <button class="pdf-nav-btn" onclick="pdfZoomOut()" title="縮小">−</button>
                            <span class="pdf-page-info" id="pdf-zoom-info">100%</span>
                            <button class="pdf-nav-btn" onclick="pdfZoomIn()" title="放大">+</button>
                            <button class="pdf-nav-btn" onclick="openPdfFullscreen(${r.id})" title="全螢幕">
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                    <polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/>
                                    <line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/>
                                </svg>
                            </button>
                            <a href="/api/report/${r.id}/pdf" target="_blank" class="pdf-nav-btn" title="新分頁開啟">
                                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                                    <path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>
                                </svg>
                            </a>
                        </div>
                        <div class="pdf-canvas-wrap" id="pdf-canvas-wrap">
                            <canvas id="pdf-canvas-${r.id}"></canvas>
                        </div>
                    </div>
                </div>` : ""}
                <div class="modal-tab-content tab-text ${r.has_pdf ? '' : 'active'}">
                    <div class="modal-raw-text">${formatRawText(r.raw_text)}</div>
                </div>
            </div>
        `;

        // 用 PDF.js 渲染 PDF
        if (r.has_pdf) {
            renderPdfWithPdfJs(r.id);
        }

    } catch (e) {
        inner.innerHTML = `
            <button class="modal-close" onclick="closeModal()">&times;</button>
            <div style="text-align:center;padding:60px;color:var(--red)">載入失敗：${e.message}</div>`;
    }
}

function closeModal() {
    const modal = document.getElementById("report-modal");
    if (modal) modal.classList.add("hidden");
    // 清理 PDF 狀態
    _pdfDoc = null;
}

// ══════════════════════════════════════════════════════════
//  PDF.js Renderer
// ══════════════════════════════════════════════════════════
let _pdfDoc = null;
let _pdfPageNum = 1;
let _pdfScale = 1.2;
let _pdfReportId = null;

async function renderPdfWithPdfJs(reportId) {
    _pdfReportId = reportId;
    _pdfPageNum = 1;
    _pdfScale = 1.2;

    const canvas = document.getElementById(`pdf-canvas-${reportId}`);
    if (!canvas) return;

    const wrap = document.getElementById("pdf-canvas-wrap");
    if (wrap) {
        wrap.innerHTML = `<div style="text-align:center;padding:60px;color:var(--text-muted)">
            <div class="typing-indicator" style="justify-content:center"><span></span><span></span><span></span></div>
            <div style="margin-top:12px;font-size:13px">載入 PDF 中...</div>
        </div>`;
    }

    try {
        // 動態載入 PDF.js (ESM)
        const pdfjsLib = await import("https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.9.155/pdf.min.mjs");
        pdfjsLib.GlobalWorkerOptions.workerSrc = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.9.155/pdf.worker.min.mjs";

        const pdfUrl = `/api/report/${reportId}/pdf`;
        _pdfDoc = await pdfjsLib.getDocument(pdfUrl).promise;

        // 重建 canvas
        if (wrap) {
            wrap.innerHTML = `<canvas id="pdf-canvas-${reportId}"></canvas>`;
        }

        updatePageInfo();
        await renderPdfPage();
    } catch (e) {
        console.error("[ANALYST] PDF.js render failed:", e);
        if (wrap) {
            wrap.innerHTML = `
                <div style="text-align:center;padding:40px;color:var(--text-muted)">
                    <p style="margin-bottom:12px">PDF 預覽載入失敗</p>
                    <a href="/api/report/${reportId}/pdf" target="_blank"
                       style="color:var(--accent);text-decoration:underline;font-weight:500">
                        📄 在新分頁開啟 PDF
                    </a>
                </div>`;
        }
    }
}

async function renderPdfPage() {
    if (!_pdfDoc || !_pdfReportId) return;

    const canvas = document.getElementById(`pdf-canvas-${_pdfReportId}`);
    if (!canvas) { console.error("[ANALYST] Canvas not found"); return; }

    const page = await _pdfDoc.getPage(_pdfPageNum);

    // 自動適配容器寬度：取 container 寬度計算 scale
    const wrap = document.getElementById("pdf-canvas-wrap");
    let effectiveScale = _pdfScale;
    if (wrap && !document.fullscreenElement) {
        const baseViewport = page.getViewport({ scale: 1.0 });
        const fitScale = (wrap.clientWidth - 20) / baseViewport.width;
        effectiveScale = fitScale * _pdfScale;
    }

    const viewport = page.getViewport({ scale: effectiveScale });

    // 用 devicePixelRatio 提高清晰度
    const dpr = window.devicePixelRatio || 1;
    canvas.width = viewport.width * dpr;
    canvas.height = viewport.height * dpr;
    canvas.style.width = viewport.width + "px";
    canvas.style.height = viewport.height + "px";

    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    await page.render({ canvasContext: ctx, viewport }).promise;

    updatePageInfo();
    console.log(`[ANALYST] Rendered page ${_pdfPageNum}, scale=${effectiveScale.toFixed(2)}, size=${viewport.width}x${viewport.height}`);
}

function updatePageInfo() {
    const info = document.getElementById("pdf-page-info");
    if (info && _pdfDoc) {
        info.textContent = `${_pdfPageNum} / ${_pdfDoc.numPages}`;
    }
    const zoom = document.getElementById("pdf-zoom-info");
    if (zoom) {
        zoom.textContent = `${Math.round(_pdfScale * 100)}%`;
    }
}

function pdfPrevPage() {
    if (_pdfPageNum <= 1) return;
    _pdfPageNum--;
    renderPdfPage();
}

function pdfNextPage() {
    if (!_pdfDoc || _pdfPageNum >= _pdfDoc.numPages) return;
    _pdfPageNum++;
    renderPdfPage();
}

function pdfZoomIn() {
    _pdfScale = Math.min(3, _pdfScale + 0.2);
    renderPdfPage();
}

function pdfZoomOut() {
    _pdfScale = Math.max(0.4, _pdfScale - 0.2);
    renderPdfPage();
}

// ── Fullscreen toggle：包含 toolbar + canvas ──
function openPdfFullscreen(reportId) {
    // 已在全螢幕 → 退出
    if (document.fullscreenElement || document.webkitFullscreenElement) {
        (document.exitFullscreen || document.webkitExitFullscreen).call(document);
        return;
    }
    // 進入全螢幕
    const container = document.querySelector(".pdf-container");
    if (!container) return;
    if (container.requestFullscreen) {
        container.requestFullscreen();
    } else if (container.webkitRequestFullscreen) {
        container.webkitRequestFullscreen();
    } else {
        window.open(`/api/report/${reportId}/pdf`, "_blank");
    }
}

// 進入/退出全螢幕時重新渲染以適配尺寸
document.addEventListener("fullscreenchange", () => {
    if (_pdfDoc) renderPdfPage();
});

document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
        if (document.fullscreenElement) {
            document.exitFullscreen();
        }
    }
    // 方向鍵翻頁
    if (document.getElementById("report-modal") && !document.getElementById("report-modal").classList.contains("hidden")) {
        if (e.key === "ArrowLeft") pdfPrevPage();
        if (e.key === "ArrowRight") pdfNextPage();
    }
});

function switchTab(btn, tab) {
    const modal = document.getElementById("report-modal");
    if (!modal) return;
    modal.querySelectorAll(".modal-tab").forEach(t => t.classList.remove("active"));
    btn.classList.add("active");
    modal.querySelectorAll(".modal-tab-content").forEach(c => c.classList.remove("active"));
    modal.querySelector(`.tab-${tab}`).classList.add("active");
}

// ══════════════════════════════════════════════════════════
//  Debug: 在 console 檢查 localStorage 狀態
// ══════════════════════════════════════════════════════════
window._debugConversations = function() {
    console.log("Active conv:", activeConvId);
    console.log("Conversations:", conversations.map(c => ({
        id: c.id,
        title: c.title,
        msgCount: (c.messages || []).length,
    })));
    const raw = localStorage.getItem(STORAGE_KEY);
    console.log("localStorage size:", raw ? (raw.length / 1024).toFixed(1) + " KB" : "empty");
    if (raw) {
        const parsed = JSON.parse(raw);
        console.log("localStorage conversations:", parsed.map(c => ({
            id: c.id,
            title: c.title,
            msgCount: (c.messages || []).length,
        })));
    }
};
