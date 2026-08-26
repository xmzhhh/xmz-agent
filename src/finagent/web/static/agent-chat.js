"use strict";

/*
 * FinAgent 持久化 Agent 网页控制器。
 *
 * 使用立即执行函数隔离变量，避免与 dashboard.js 的状态、API 工具和事件名称冲突。本文件只
 * 渲染后端已经确定的会话、消息和结构化记忆，不在浏览器中复制 Agent 编排或金融计算逻辑。
 */
(function initializeAgentModule() {
    const AGENT_API_BASE = "/api/v1";
    const LAST_SESSION_KEY = "finagent.lastAgentSessionId";

    const agentState = {
        sessions: [],
        selectedSessionId: null,
        messages: [],
        candidates: [],
        activeMemories: [],
        busy: false,
        messageTimer: null,
    };

    const agentElements = {
        status: document.querySelector("#agent-message"),
        sessionForm: document.querySelector("#session-form"),
        sessionName: document.querySelector("#session-name"),
        sessionCreateButton: document.querySelector("#session-create-button"),
        sessionList: document.querySelector("#session-list"),
        sessionDeleteButton: document.querySelector("#session-delete-button"),
        sessionMeta: document.querySelector("#chat-session-meta"),
        messages: document.querySelector("#chat-messages"),
        chatForm: document.querySelector("#chat-form"),
        chatInput: document.querySelector("#chat-input"),
        assetSymbols: document.querySelector("#agent-asset-symbols"),
        chatSubmitButton: document.querySelector("#chat-submit-button"),
        memoryRefreshButton: document.querySelector("#memory-refresh-button"),
        candidates: document.querySelector("#memory-candidates"),
        activeMemories: document.querySelector("#active-memories"),
    };


    class AgentApiError extends Error {
        /** 保存后端稳定错误代码，界面不需要解析中文消息来判断错误类型。 */
        constructor(status, code, message) {
            super(message);
            this.name = "AgentApiError";
            this.status = status;
            this.code = code;
        }
    }


    async function agentApiRequest(path, options = {}) {
        /** 调用同源 Agent API，并统一解析后端错误结构。 */
        const response = await fetch(`${AGENT_API_BASE}${path}`, {
            ...options,
            headers: {
                Accept: "application/json",
                ...(options.body ? {"Content-Type": "application/json"} : {}),
                ...(options.headers || {}),
            },
        });
        const contentType = response.headers.get("content-type") || "";
        const payload = contentType.includes("application/json") ? await response.json() : null;
        if (!response.ok) {
            const error = payload?.error;
            throw new AgentApiError(
                response.status,
                error?.code || "http_error",
                error?.message || `请求失败（HTTP ${response.status}）`,
            );
        }
        return payload;
    }


    function describeAgentError(error) {
        /** 把未知浏览器错误安全转换为可展示文本。 */
        if (error instanceof AgentApiError || error instanceof Error) {
            return error.message;
        }
        return "发生未知错误，请稍后重试";
    }


    function showAgentMessage(message, type = "success", persistent = false) {
        /** 在 Agent 区域展示操作结果，避免与资产面板全局提示互相覆盖。 */
        window.clearTimeout(agentState.messageTimer);
        agentElements.status.textContent = message;
        agentElements.status.className = `message message-${type}`;
        agentElements.status.hidden = false;
        if (!persistent) {
            agentState.messageTimer = window.setTimeout(() => {
                agentElements.status.hidden = true;
            }, 5000);
        }
    }


    function readLastSessionId() {
        /** localStorage 只保存会话 UUID，不保存聊天正文或记忆内容。 */
        try {
            return window.localStorage.getItem(LAST_SESSION_KEY);
        } catch (_error) {
            return null;
        }
    }


    function saveLastSessionId(sessionId) {
        /** 浏览器禁用本地存储时安静降级为选择最近会话。 */
        try {
            if (sessionId) {
                window.localStorage.setItem(LAST_SESSION_KEY, sessionId);
            } else {
                window.localStorage.removeItem(LAST_SESSION_KEY);
            }
        } catch (_error) {
            // 会话正文仍保存在 SQLite；localStorage 失败只影响刷新后的默认选中项。
        }
    }


    function formatAgentTime(value) {
        /** 把服务端带时区时间转换为浏览器本地时间。 */
        if (!value) {
            return "—";
        }
        const date = new Date(value);
        return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN");
    }


    function setAgentBusy(isBusy) {
        /** 同一页面一次只提交一个会话或记忆动作，防止用户重复点击。 */
        agentState.busy = isBusy;
        agentElements.sessionCreateButton.disabled = isBusy;
        agentElements.sessionList.disabled = isBusy || agentState.sessions.length === 0;
        agentElements.sessionDeleteButton.disabled = isBusy || !agentState.selectedSessionId;
        agentElements.chatInput.disabled = isBusy || !agentState.selectedSessionId;
        agentElements.assetSymbols.disabled = isBusy || !agentState.selectedSessionId;
        agentElements.chatSubmitButton.disabled = isBusy || !agentState.selectedSessionId;
        agentElements.memoryRefreshButton.disabled = isBusy;
        for (const button of document.querySelectorAll(".memory-actions button")) {
            button.disabled = isBusy;
        }
        agentElements.chatSubmitButton.textContent = isBusy ? "AI 正在思考…" : "发送";
    }


    function renderSessionList() {
        /** 以更新时间倒序渲染后端会话；option 文本始终通过 textContent 设置。 */
        agentElements.sessionList.replaceChildren();
        if (agentState.sessions.length === 0) {
            const emptyOption = document.createElement("option");
            emptyOption.value = "";
            emptyOption.textContent = "暂无会话，请先新建";
            agentElements.sessionList.append(emptyOption);
        } else {
            for (const session of agentState.sessions) {
                const option = document.createElement("option");
                option.value = session.id;
                option.textContent = `${session.title} · ${formatAgentTime(session.updated_at)}`;
                option.selected = session.id === agentState.selectedSessionId;
                agentElements.sessionList.append(option);
            }
        }
        setAgentBusy(agentState.busy);
    }


    function appendTextElement(parent, tagName, text, className = "") {
        /** 创建只含纯文本的元素，集中保证模型和工具内容不会作为 HTML 执行。 */
        const element = document.createElement(tagName);
        element.textContent = text;
        if (className) {
            element.className = className;
        }
        parent.append(element);
        return element;
    }


    function prettyJson(value) {
        /** 美化工具参数和结构化记忆；无法解析时保留原始纯文本。 */
        if (typeof value !== "string") {
            return JSON.stringify(value, null, 2);
        }
        try {
            return JSON.stringify(JSON.parse(value), null, 2);
        } catch (_error) {
            return value;
        }
    }


    function buildToolDetails(summaryText, payload, className) {
        /** 使用 details 折叠技术轨迹，默认让最终自然语言回答保持视觉主次。 */
        const details = document.createElement("details");
        details.className = className;
        appendTextElement(details, "summary", summaryText);
        appendTextElement(details, "pre", prettyJson(payload));
        return details;
    }


    function renderConversationMessage(message) {
        /** 把四类模型协议消息渲染成用户、助手或可折叠工具轨迹。 */
        const article = document.createElement("article");
        article.className = `chat-message chat-message-${message.role}`;

        const roleLabels = {
            user: "你",
            assistant: "FinAgent",
            tool: "工具结果",
        };
        appendTextElement(
            article,
            "span",
            roleLabels[message.role] || message.role,
            "chat-message-role",
        );

        if (message.content) {
            if (message.role === "tool") {
                article.append(
                    buildToolDetails(
                        `查看工具返回 · 调用 ${message.tool_call_id || "未知"}`,
                        message.content,
                        "tool-trace tool-result",
                    ),
                );
            } else {
                appendTextElement(article, "p", message.content, "chat-message-content");
            }
        }

        for (const toolCall of message.tool_calls || []) {
            article.append(
                buildToolDetails(
                    `调用只读工具：${toolCall.name}`,
                    toolCall.arguments,
                    "tool-trace",
                ),
            );
        }
        appendTextElement(
            article,
            "time",
            formatAgentTime(message.created_at),
            "chat-message-time",
        );
        return article;
    }


    function renderMessages() {
        /** 按数据库 sequence_number 顺序重建会话，刷新页面不会丢失工具轨迹。 */
        agentElements.messages.replaceChildren();
        if (!agentState.selectedSessionId) {
            appendTextElement(
                agentElements.messages,
                "p",
                "选择会话后即可开始对话。",
                "empty-copy",
            );
            agentElements.sessionMeta.textContent = "请先新建或选择会话";
            return;
        }

        const selected = agentState.sessions.find(
            (session) => session.id === agentState.selectedSessionId,
        );
        agentElements.sessionMeta.textContent = selected
            ? `${selected.title} · ${agentState.messages.length} 条消息`
            : `${agentState.messages.length} 条消息`;
        if (agentState.messages.length === 0) {
            appendTextElement(
                agentElements.messages,
                "p",
                "这是一个空会话，可以在下方输入第一个问题。",
                "empty-copy",
            );
        } else {
            for (const message of agentState.messages) {
                agentElements.messages.append(renderConversationMessage(message));
            }
        }
        agentElements.messages.scrollTop = agentElements.messages.scrollHeight;
    }


    function memoryScopeLabel(memory) {
        /** 把结构化作用域转换成面向用户的简短说明。 */
        return memory.scope_type === "asset"
            ? `资产：${memory.scope_id}`
            : "全局";
    }


    function makeMemoryActionButton(label, className, action, memoryId) {
        /** 记忆动作必须由独立按钮触发，不能从聊天文本中隐式执行。 */
        const button = document.createElement("button");
        button.type = "button";
        button.className = `button ${className}`;
        button.textContent = label;
        button.addEventListener("click", () => void performMemoryAction(memoryId, action));
        return button;
    }


    function renderMemoryCard(memory, isCandidate) {
        /** 展示结构化字段和显式状态动作，不展示来源消息 UUID 等内部信息。 */
        const article = document.createElement("article");
        article.className = "memory-card";
        const heading = document.createElement("div");
        heading.className = "memory-card-heading";
        appendTextElement(heading, "strong", memory.memory_key);
        appendTextElement(heading, "span", memory.memory_type, "memory-type-badge");
        article.append(heading);
        appendTextElement(
            article,
            "p",
            `${memoryScopeLabel(memory)} · 版本 ${memory.version}`,
            "memory-meta",
        );
        appendTextElement(article, "pre", prettyJson(memory.value), "memory-value");

        const actions = document.createElement("div");
        actions.className = "memory-actions";
        if (isCandidate) {
            actions.append(
                makeMemoryActionButton("确认", "button-primary", "confirm", memory.id),
                makeMemoryActionButton("拒绝", "button-secondary", "reject", memory.id),
            );
        }
        actions.append(
            makeMemoryActionButton("删除", "button-danger", "delete", memory.id),
        );
        article.append(actions);
        return article;
    }


    function renderMemoryList(container, memories, isCandidate) {
        /** 使用统一卡片渲染候选和 ACTIVE 记忆。 */
        container.replaceChildren();
        if (memories.length === 0) {
            appendTextElement(
                container,
                "p",
                isCandidate ? "暂无候选记忆。" : "暂无已确认记忆。",
                "empty-copy",
            );
            return;
        }
        for (const memory of memories) {
            container.append(renderMemoryCard(memory, isCandidate));
        }
    }


    async function loadMemories() {
        /** 并行读取候选与 ACTIVE 记忆，其他历史状态仍可在 Swagger 中查询。 */
        const [candidates, activeMemories] = await Promise.all([
            agentApiRequest("/memories/candidates"),
            agentApiRequest("/memories?status=active"),
        ]);
        agentState.candidates = candidates;
        agentState.activeMemories = activeMemories;
        renderMemoryList(agentElements.candidates, candidates, true);
        renderMemoryList(agentElements.activeMemories, activeMemories, false);
        setAgentBusy(agentState.busy);
    }


    async function loadAssetOptions() {
        /** 从资产目录动态生成作用域选项，避免在 JavaScript 中维护第二份白名单。 */
        const assets = await agentApiRequest("/assets");
        agentElements.assetSymbols.replaceChildren();
        for (const asset of assets.filter((item) => item.is_holding_supported)) {
            const option = document.createElement("option");
            option.value = asset.symbol;
            option.textContent = `${asset.name}（${asset.symbol}）`;
            agentElements.assetSymbols.append(option);
        }
    }


    async function loadMessages() {
        /** 读取当前会话完整历史；未选择会话时只重置空状态。 */
        if (!agentState.selectedSessionId) {
            agentState.messages = [];
            renderMessages();
            return;
        }
        agentState.messages = await agentApiRequest(
            `/agent/sessions/${encodeURIComponent(agentState.selectedSessionId)}/messages`,
        );
        renderMessages();
    }


    async function selectSession(sessionId) {
        /** 切换会话后从 SQLite 读取历史，而不是继续使用旧页面的内存消息。 */
        agentState.selectedSessionId = sessionId || null;
        saveLastSessionId(agentState.selectedSessionId);
        renderSessionList();
        setAgentBusy(agentState.busy);
        await loadMessages();
    }


    async function loadSessions(preferredSessionId = null) {
        /** 刷新会话列表，并优先保留当前、显式指定或上次浏览的会话。 */
        agentState.sessions = await agentApiRequest("/agent/sessions?status=active");
        const requestedId = preferredSessionId
            || agentState.selectedSessionId
            || readLastSessionId();
        const selected = agentState.sessions.find((session) => session.id === requestedId)
            || agentState.sessions[0]
            || null;
        agentState.selectedSessionId = selected?.id || null;
        saveLastSessionId(agentState.selectedSessionId);
        renderSessionList();
        await loadMessages();
    }


    async function handleSessionCreate(event) {
        /** 用户显式提交标题后创建会话，并立即选中新记录。 */
        event.preventDefault();
        const title = agentElements.sessionName.value.trim();
        if (!title) {
            showAgentMessage("请输入会话标题。", "error");
            return;
        }
        setAgentBusy(true);
        try {
            const created = await agentApiRequest("/agent/sessions", {
                method: "POST",
                body: JSON.stringify({title}),
            });
            agentElements.sessionForm.reset();
            await loadSessions(created.id);
            showAgentMessage(`会话“${created.title}”已创建。`);
            agentElements.chatInput.focus();
        } catch (error) {
            showAgentMessage(describeAgentError(error), "error", true);
        } finally {
            setAgentBusy(false);
        }
    }


    async function handleSessionDelete() {
        /** 删除会话前再次确认；长期记忆不会随短期会话被隐式删除。 */
        const current = agentState.sessions.find(
            (session) => session.id === agentState.selectedSessionId,
        );
        if (!current || !window.confirm(`确定删除会话“${current.title}”及其消息吗？`)) {
            return;
        }
        setAgentBusy(true);
        try {
            await agentApiRequest(`/agent/sessions/${encodeURIComponent(current.id)}`, {
                method: "DELETE",
            });
            agentState.selectedSessionId = null;
            saveLastSessionId(null);
            await loadSessions();
            await loadMemories();
            showAgentMessage("会话已删除；已确认长期记忆不会被连带删除。", "success");
        } catch (error) {
            showAgentMessage(describeAgentError(error), "error", true);
        } finally {
            setAgentBusy(false);
        }
    }


    async function handleChatSubmit(event) {
        /** 发送普通请求—响应聊天；成功后重新从 SQLite 恢复权威消息序列。 */
        event.preventDefault();
        if (!agentState.selectedSessionId) {
            showAgentMessage("请先新建或选择会话。", "error");
            return;
        }
        const message = agentElements.chatInput.value.trim();
        if (!message) {
            showAgentMessage("请输入要发送的内容。", "error");
            return;
        }
        const assetSymbols = Array.from(agentElements.assetSymbols.selectedOptions)
            .map((option) => option.value);

        setAgentBusy(true);
        showAgentMessage("AI 正在读取上下文并按需调用只读工具…", "loading", true);
        try {
            const result = await agentApiRequest(
                `/agent/sessions/${encodeURIComponent(agentState.selectedSessionId)}/chat`,
                {
                    method: "POST",
                    body: JSON.stringify({message, asset_symbols: assetSymbols}),
                },
            );
            agentElements.chatForm.reset();
            await loadSessions(agentState.selectedSessionId);
            await loadMemories();
            if (result.memory_warning) {
                showAgentMessage(result.memory_warning, "warning", true);
            } else if (result.memory_candidate) {
                showAgentMessage("回答已保存，并生成了一条等待你确认的长期记忆。", "success");
            } else {
                showAgentMessage("回答和完整工具轨迹已保存。", "success");
            }
        } catch (error) {
            showAgentMessage(describeAgentError(error), "error", true);
            // 主 Agent 失败时后端不会保存半轮消息；重新加载可让界面恢复数据库权威状态。
            await loadMessages().catch(() => undefined);
        } finally {
            setAgentBusy(false);
            agentElements.chatInput.focus();
        }
    }


    async function performMemoryAction(memoryId, action) {
        /** 通过独立端点执行确认、拒绝或删除，聊天内容永远不会直接进入此函数。 */
        if (action === "delete" && !window.confirm("确定永久删除这条记忆正文吗？")) {
            return;
        }
        const requestByAction = {
            confirm: {
                path: `/memories/${encodeURIComponent(memoryId)}/confirm`,
                options: {method: "POST"},
                success: "候选记忆已确认，后续相关对话可以使用它。",
            },
            reject: {
                path: `/memories/${encodeURIComponent(memoryId)}/reject`,
                options: {
                    method: "POST",
                    body: JSON.stringify({reason: "user_decision"}),
                },
                success: "候选记忆已拒绝，不会进入 Agent 上下文。",
            },
            delete: {
                path: `/memories/${encodeURIComponent(memoryId)}`,
                options: {method: "DELETE"},
                success: "记忆正文已删除，最小审计事件仍保留。",
            },
        };
        const request = requestByAction[action];
        if (!request) {
            return;
        }

        setAgentBusy(true);
        try {
            await agentApiRequest(request.path, request.options);
            await loadMemories();
            showAgentMessage(request.success, "success");
        } catch (error) {
            showAgentMessage(describeAgentError(error), "error", true);
        } finally {
            setAgentBusy(false);
        }
    }


    async function handleMemoryRefresh() {
        /** 手工刷新期间禁用所有状态按钮，防止与确认或删除请求重叠。 */
        setAgentBusy(true);
        try {
            await loadMemories();
            showAgentMessage("长期记忆已刷新。");
        } catch (error) {
            showAgentMessage(describeAgentError(error), "error", true);
        } finally {
            setAgentBusy(false);
        }
    }


    async function initializeAgentWorkspace() {
        /** 页面启动时并行加载目录和记忆，再恢复最近使用的 SQLite 会话。 */
        setAgentBusy(true);
        try {
            await Promise.all([loadAssetOptions(), loadMemories()]);
            await loadSessions();
        } catch (error) {
            showAgentMessage(describeAgentError(error), "error", true);
        } finally {
            setAgentBusy(false);
        }
    }


    agentElements.sessionForm.addEventListener("submit", (event) => {
        void handleSessionCreate(event);
    });
    agentElements.sessionList.addEventListener("change", () => {
        void selectSession(agentElements.sessionList.value).catch((error) => {
            showAgentMessage(describeAgentError(error), "error", true);
        });
    });
    agentElements.sessionDeleteButton.addEventListener("click", () => {
        void handleSessionDelete();
    });
    agentElements.chatForm.addEventListener("submit", (event) => {
        void handleChatSubmit(event);
    });
    agentElements.chatInput.addEventListener("keydown", (event) => {
        if (event.ctrlKey && event.key === "Enter") {
            event.preventDefault();
            agentElements.chatForm.requestSubmit();
        }
    });
    agentElements.memoryRefreshButton.addEventListener("click", () => {
        void handleMemoryRefresh();
    });

    void initializeAgentWorkspace();
})();
