"use strict";

/*
 * FinAgent 资产面板浏览器控制器。
 *
 * 本文件只负责四件事：读取表单、调用 /api/v1、把响应格式化为中文界面、绑定交互事件。
 * 所有金融公式都留在 Python PortfolioCalculator 中，前端绝不根据价格自行计算收益或权重。
 */

const API_BASE = "/api/v1";
const JD_GOLD_SYMBOL = "JD-ZS-GOLD";

const state = {
    assets: [],
    holdings: [],
    dashboard: null,
    editingSymbol: null,
    messageTimer: null,
};

const elements = {
    connectionStatus: document.querySelector("#connection-status"),
    refreshButton: document.querySelector("#refresh-button"),
    demoButton: document.querySelector("#demo-button"),
    pageMessage: document.querySelector("#page-message"),
    holdingForm: document.querySelector("#holding-form"),
    holdingFormTitle: document.querySelector("#holding-form-title"),
    holdingSymbol: document.querySelector("#holding-symbol"),
    holdingQuantity: document.querySelector("#holding-quantity"),
    holdingAverageCost: document.querySelector("#holding-average-cost"),
    holdingFeeRate: document.querySelector("#holding-fee-rate"),
    holdingSubmitButton: document.querySelector("#holding-submit-button"),
    cancelEditButton: document.querySelector("#cancel-edit-button"),
    manualPriceForm: document.querySelector("#manual-price-form"),
    manualGoldPrice: document.querySelector("#manual-gold-price"),
    deletePriceButton: document.querySelector("#delete-price-button"),
    manualPriceValue: document.querySelector("#manual-price-value"),
    manualPriceTime: document.querySelector("#manual-price-time"),
    positionsBody: document.querySelector("#positions-body"),
    positionCount: document.querySelector("#position-count"),
};


class ApiError extends Error {
    /** 保存 HTTP 状态码和后端稳定错误代码，便于界面区分“尚未录入”和真正故障。 */
    constructor(status, code, message) {
        super(message);
        this.name = "ApiError";
        this.status = status;
        this.code = code;
    }
}


async function apiRequest(path, options = {}) {
    /** 调用同源 API，并把所有非 2xx 响应转换成统一 ApiError。 */
    const requestOptions = {
        ...options,
        headers: {
            Accept: "application/json",
            ...(options.body ? {"Content-Type": "application/json"} : {}),
            ...(options.headers || {}),
        },
    };
    const response = await fetch(`${API_BASE}${path}`, requestOptions);
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json") ? await response.json() : null;

    if (!response.ok) {
        const error = payload?.error;
        throw new ApiError(
            response.status,
            error?.code || "http_error",
            error?.message || `请求失败（HTTP ${response.status}）`,
        );
    }
    return payload;
}


function formatMoney(value, currency = "CNY") {
    /** 只格式化后端金额，不执行任何加减乘除。 */
    if (value === null || value === undefined) {
        return "—";
    }
    const numericValue = Number(value);
    if (!Number.isFinite(numericValue)) {
        return String(value);
    }
    return new Intl.NumberFormat("zh-CN", {
        style: "currency",
        currency,
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
    }).format(numericValue);
}


function formatNumber(value, maximumFractionDigits = 4) {
    /** 格式化数量或价格；原始精确值仍保留在 API 响应和编辑表单中。 */
    if (value === null || value === undefined) {
        return "—";
    }
    const numericValue = Number(value);
    if (!Number.isFinite(numericValue)) {
        return String(value);
    }
    return new Intl.NumberFormat("zh-CN", {
        minimumFractionDigits: 0,
        maximumFractionDigits,
    }).format(numericValue);
}


function formatPercent(value) {
    /** 后端返回的字段已经采用百分数语义，此处仅追加百分号。 */
    return value === null || value === undefined ? "—" : `${formatNumber(value, 2)}%`;
}


function formatDateTime(value) {
    /** 把带时区 ISO 时间转换为浏览器本地时间。 */
    if (!value) {
        return "—";
    }
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN");
}


function setText(selector, value) {
    /** 使用 textContent 写入外部数据，避免把行情来源等文本当成 HTML 执行。 */
    const element = document.querySelector(selector);
    if (element) {
        element.textContent = value;
    }
    return element;
}


function setSignClass(element, value) {
    /** 正负颜色只用于视觉提示，不参与任何收益计算。 */
    if (!element) {
        return;
    }
    element.classList.remove("is-positive", "is-negative");
    const numericValue = Number(value);
    if (Number.isFinite(numericValue) && numericValue > 0) {
        element.classList.add("is-positive");
    } else if (Number.isFinite(numericValue) && numericValue < 0) {
        element.classList.add("is-negative");
    }
}


function showMessage(message, type = "success", persistent = false) {
    /** 在页面顶部显示操作结果；非持久提示会自动消失。 */
    window.clearTimeout(state.messageTimer);
    elements.pageMessage.textContent = message;
    elements.pageMessage.classList.toggle("message-error", type === "error");
    elements.pageMessage.hidden = false;
    if (!persistent) {
        state.messageTimer = window.setTimeout(() => {
            elements.pageMessage.hidden = true;
        }, 5000);
    }
}


function describeError(error) {
    /** 为网络级故障补充易懂说明，后端业务错误则直接使用稳定 message。 */
    if (error instanceof ApiError) {
        return error.message;
    }
    if (error instanceof TypeError) {
        return "无法连接 FinAgent 服务，请确认资产面板仍在运行。";
    }
    return error instanceof Error ? error.message : "发生未知错误";
}


function setConnectionStatus(mode, online = true) {
    /** 更新顶栏连接状态和 Fake/Real 模式。 */
    elements.connectionStatus.className = `status-badge ${online ? "status-ok" : "status-error"}`;
    elements.connectionStatus.textContent = online
        ? `${mode === "real" ? "Real" : "Fake"} 模式 · 已连接`
        : "连接失败";
    setText("#market-mode", mode || "—");
    elements.demoButton.disabled = !online || mode !== "fake";
    elements.demoButton.title = mode === "real" ? "Real 模式不允许载入匿名演示组合" : "";
}


function renderAssetOptions() {
    /** 下拉框只展示后端目录明确允许录入持仓的资产。 */
    const previousValue = elements.holdingSymbol.value;
    const options = [new Option("请选择资产", "")];
    for (const asset of state.assets.filter((item) => item.is_holding_supported)) {
        options.push(new Option(`${asset.name}（${asset.symbol}）`, asset.symbol));
    }
    elements.holdingSymbol.replaceChildren(...options);
    if (state.assets.some((asset) => asset.symbol === previousValue)) {
        elements.holdingSymbol.value = previousValue;
    }
}


function resetHoldingForm() {
    /** 从编辑模式恢复为新增模式。 */
    state.editingSymbol = null;
    elements.holdingForm.reset();
    elements.holdingFeeRate.value = "0";
    elements.holdingSymbol.disabled = false;
    elements.holdingFormTitle.textContent = "新增持仓";
    elements.holdingSubmitButton.textContent = "新增持仓";
    elements.cancelEditButton.hidden = true;
}


function startEditingHolding(holding) {
    /** 把一条持仓的原始 Decimal 字符串填回表单，不经过浮点数转换。 */
    state.editingSymbol = holding.symbol;
    elements.holdingSymbol.value = holding.symbol;
    elements.holdingSymbol.disabled = true;
    elements.holdingQuantity.value = holding.quantity;
    elements.holdingAverageCost.value = holding.average_cost;
    elements.holdingFeeRate.value = holding.estimated_exit_fee_percent;
    elements.holdingFormTitle.textContent = `编辑 ${holding.symbol}`;
    elements.holdingSubmitButton.textContent = "保存修改";
    elements.cancelEditButton.hidden = false;
    elements.holdingForm.scrollIntoView({behavior: "smooth", block: "center"});
}


function appendValueCell(row, primary, secondary = "", className = "") {
    /** 创建带主值和说明的表格单元格，所有文本都通过 textContent 写入。 */
    const cell = document.createElement("td");
    const strong = document.createElement("strong");
    strong.textContent = primary;
    if (className) {
        strong.classList.add(className);
    }
    cell.append(strong);
    if (secondary) {
        const small = document.createElement("small");
        small.textContent = secondary;
        cell.append(small);
    }
    row.append(cell);
    return strong;
}


function buildActionCell(holding) {
    /** 为持仓绑定编辑和删除动作，事件闭包保留当前持仓对象。 */
    const cell = document.createElement("td");
    const wrapper = document.createElement("div");
    wrapper.className = "table-actions";

    const editButton = document.createElement("button");
    editButton.type = "button";
    editButton.className = "table-action";
    editButton.textContent = "编辑";
    editButton.addEventListener("click", () => startEditingHolding(holding));

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "table-action table-action-danger";
    deleteButton.textContent = "删除";
    deleteButton.addEventListener("click", () => void deleteHolding(holding));

    wrapper.append(editButton, deleteButton);
    cell.append(wrapper);
    return cell;
}


function renderPositions() {
    /** 合并持仓 CRUD 数据与可选估值结果；快照失败时仍允许用户编辑或删除持仓。 */
    elements.positionsBody.replaceChildren();
    elements.positionCount.textContent = `${state.holdings.length} 项持仓`;
    if (state.holdings.length === 0) {
        const row = document.createElement("tr");
        row.className = "empty-row";
        const cell = document.createElement("td");
        cell.colSpan = 9;
        cell.textContent = "当前没有持仓，可以手工新增或载入匿名演示组合。";
        row.append(cell);
        elements.positionsBody.append(row);
        return;
    }

    const valuedBySymbol = new Map(
        (state.dashboard?.portfolio.positions || []).map((position) => [position.symbol, position]),
    );

    for (const holding of state.holdings) {
        const position = valuedBySymbol.get(holding.symbol);
        const row = document.createElement("tr");
        appendValueCell(row, holding.name, `${holding.symbol} · ${holding.asset_type}`);
        appendValueCell(
            row,
            formatNumber(holding.quantity),
            `均价 ${formatMoney(holding.average_cost, holding.currency)}`,
        );

        if (position) {
            appendValueCell(
                row,
                formatMoney(position.current_price, position.currency),
                `市值 ${formatMoney(position.market_value, position.currency)}`,
            );
            const gross = appendValueCell(
                row,
                formatMoney(position.unrealized_pnl, position.currency),
                formatPercent(position.return_percent),
            );
            setSignClass(gross, position.unrealized_pnl);
            appendValueCell(
                row,
                formatMoney(position.estimated_exit_fee, position.currency),
                `到账 ${formatMoney(position.net_liquidation_value, position.currency)}`,
            );
            const net = appendValueCell(
                row,
                formatMoney(position.net_unrealized_pnl, position.currency),
                formatPercent(position.net_return_percent),
            );
            setSignClass(net, position.net_unrealized_pnl);
            appendValueCell(row, formatPercent(position.weight_percent));
            appendValueCell(
                row,
                position.quote_is_delayed ? "延迟行情" : "当前行情",
                `${position.quote_source} · ${formatDateTime(position.quote_as_of)}`,
            );
        } else {
            appendValueCell(row, "等待估值", "请检查必要行情或手工价格");
            appendValueCell(row, "—");
            appendValueCell(row, "—");
            appendValueCell(row, "—");
            appendValueCell(row, "—");
            appendValueCell(row, "快照不可用");
        }
        row.append(buildActionCell(holding));
        elements.positionsBody.append(row);
    }
}


function clearSummary() {
    /** 快照失败时清空旧汇总，防止用户误把过期页面值当成当前结果。 */
    for (const selector of [
        "#total-market-value",
        "#total-net-value",
        "#total-net-pnl",
        "#total-exit-fee",
        "#max-position",
        "#concentration-hhi",
        "#base-currency",
        "#portfolio-as-of",
        "#delayed-data",
    ]) {
        setText(selector, "—");
    }
    setText("#total-net-return", "收益率 —");
    setText("#total-cost", "持仓成本 —");
    setText("#snapshot-time", "组合快照暂不可用");
    renderAssetTypeWeights({});
}


function renderAssetTypeWeights(weights) {
    /** 使用后端权重直接控制进度条宽度，不在浏览器重新计算占比。 */
    const container = document.querySelector("#asset-type-weights");
    container.replaceChildren();
    const entries = Object.entries(weights || {});
    if (entries.length === 0) {
        const empty = document.createElement("p");
        empty.className = "empty-copy";
        empty.textContent = "暂无资产类别权重。";
        container.append(empty);
        return;
    }

    const labels = {fund: "基金", gold: "黄金", stock: "股票", bond: "债券", cash: "现金"};
    for (const [assetType, weight] of entries) {
        const row = document.createElement("div");
        const header = document.createElement("div");
        header.className = "weight-row-header";
        const label = document.createElement("span");
        label.textContent = labels[assetType] || assetType;
        const value = document.createElement("span");
        value.textContent = formatPercent(weight);
        header.append(label, value);

        const track = document.createElement("div");
        track.className = "weight-track";
        const fill = document.createElement("div");
        fill.className = "weight-fill";
        fill.style.width = `${Math.min(100, Math.max(0, Number(weight)))}%`;
        track.append(fill);
        row.append(header, track);
        container.append(row);
    }
}


function renderGoldReference(reference) {
    /** 呈现国际金价的可用、降级或未请求状态。 */
    const status = document.querySelector("#gold-reference-status");
    status.className = "status-badge";

    if (reference?.status === "available") {
        status.textContent = "可用";
        status.classList.add("status-ok");
        setText(
            "#gold-reference-price",
            `${formatMoney(reference.quote.price, reference.quote.currency)} / 克`,
        );
        setText("#gold-reference-source", reference.quote.source);
        setText("#gold-reference-time", formatDateTime(reference.quote.as_of));
        setText("#gold-reference-delay", reference.quote.is_delayed ? "是" : "否");
        return;
    }

    if (reference?.status === "unavailable") {
        status.textContent = "暂不可用";
        status.classList.add("status-error");
        setText("#gold-reference-price", "—");
        setText("#gold-reference-source", reference.message || "国际黄金参考价暂不可用");
        setText("#gold-reference-time", "—");
        setText("#gold-reference-delay", "—");
        return;
    }

    status.textContent = "未请求";
    setText("#gold-reference-price", "—");
    setText("#gold-reference-source", "存在京东黄金持仓时才会查询，仅供比较，不参与持仓估值。");
    setText("#gold-reference-time", "—");
    setText("#gold-reference-delay", "—");
}


function renderDashboard() {
    /** 用后端快照刷新汇总、风险、行情质量和持仓估值。 */
    if (!state.dashboard) {
        clearSummary();
        renderGoldReference(null);
        renderPositions();
        return;
    }

    const portfolio = state.dashboard.portfolio;
    setText("#total-market-value", formatMoney(portfolio.total_market_value));
    setText("#total-net-value", formatMoney(portfolio.total_net_liquidation_value));
    const netPnl = setText("#total-net-pnl", formatMoney(portfolio.total_net_unrealized_pnl));
    setSignClass(netPnl, portfolio.total_net_unrealized_pnl);
    setText("#total-net-return", `收益率 ${formatPercent(portfolio.total_net_return_percent)}`);
    setText("#total-exit-fee", formatMoney(portfolio.total_estimated_exit_fee));
    setText("#total-cost", `持仓成本 ${formatMoney(portfolio.total_cost)}`);
    setText(
        "#max-position",
        portfolio.max_position_symbol
            ? `${portfolio.max_position_symbol} · ${formatPercent(portfolio.max_position_weight_percent)}`
            : "—",
    );
    setText("#concentration-hhi", formatNumber(portfolio.concentration_hhi, 2));
    setText("#base-currency", portfolio.base_currency);
    setText("#portfolio-as-of", formatDateTime(portfolio.as_of));
    setText("#delayed-data", portfolio.has_delayed_data ? "是" : "否");
    setText(
        "#snapshot-time",
        portfolio.as_of ? `组合数据时间 ${formatDateTime(portfolio.as_of)}` : "当前为空仓",
    );
    renderAssetTypeWeights(portfolio.asset_type_weights);
    renderGoldReference(state.dashboard.gold_reference);
    renderPositions();
}


function renderManualPrice(record) {
    /** 展示服务端保存的原始手工价格记录。 */
    if (!record) {
        elements.manualPriceValue.textContent = "尚未录入";
        elements.manualPriceTime.textContent = "—";
        return;
    }
    elements.manualPriceValue.textContent = `${formatMoney(record.price, record.currency)} / 克`;
    elements.manualPriceTime.textContent = formatDateTime(record.recorded_at);
}


async function loadManualPrice() {
    /** 手工价格不存在是正常初始状态，不显示成系统故障。 */
    try {
        const record = await apiRequest(`/manual-prices/${JD_GOLD_SYMBOL}`);
        renderManualPrice(record);
    } catch (error) {
        if (error instanceof ApiError && error.code === "ManualPriceNotFoundError") {
            renderManualPrice(null);
            return;
        }
        throw error;
    }
}


async function refreshDashboardData({announce = false} = {}) {
    /** 先刷新可编辑持仓，再尝试生成估值；必要价格缺失也不会让 CRUD 表格消失。 */
    state.holdings = await apiRequest("/holdings");
    await loadManualPrice();
    try {
        state.dashboard = await apiRequest("/dashboard");
        renderDashboard();
        if (announce) {
            showMessage("资产面板已刷新。", "success");
        }
    } catch (error) {
        state.dashboard = null;
        renderDashboard();
        showMessage(`组合快照暂不可用：${describeError(error)}`, "error", true);
    }
}


async function handleHoldingSubmit(event) {
    /** 根据当前模式调用 POST 或 PUT；代码在编辑时不可改变。 */
    event.preventDefault();
    const payload = {
        quantity: elements.holdingQuantity.value.trim(),
        average_cost: elements.holdingAverageCost.value.trim(),
        estimated_exit_fee_percent: elements.holdingFeeRate.value.trim(),
    };

    elements.holdingSubmitButton.disabled = true;
    try {
        if (state.editingSymbol) {
            await apiRequest(`/holdings/${encodeURIComponent(state.editingSymbol)}`, {
                method: "PUT",
                body: JSON.stringify(payload),
            });
            showMessage(`持仓 ${state.editingSymbol} 已更新。`);
        } else {
            const createdSymbol = elements.holdingSymbol.value;
            await apiRequest("/holdings", {
                method: "POST",
                body: JSON.stringify({symbol: createdSymbol, ...payload}),
            });
            showMessage(
                createdSymbol === JD_GOLD_SYMBOL
                    ? "黄金持仓已新增。请录入京东卖出价后查看完整估值。"
                    : "持仓已新增。",
            );
        }
        resetHoldingForm();
        await refreshDashboardData();
    } catch (error) {
        showMessage(describeError(error), "error", true);
    } finally {
        elements.holdingSubmitButton.disabled = false;
    }
}


async function deleteHolding(holding) {
    /** 删除前由用户明确确认；删除京东黄金时后端会同步清理手工价格。 */
    if (!window.confirm(`确定删除 ${holding.name}（${holding.symbol}）吗？`)) {
        return;
    }
    try {
        await apiRequest(`/holdings/${encodeURIComponent(holding.symbol)}`, {method: "DELETE"});
        if (state.editingSymbol === holding.symbol) {
            resetHoldingForm();
        }
        showMessage(`持仓 ${holding.symbol} 已删除。`);
        await refreshDashboardData();
    } catch (error) {
        showMessage(describeError(error), "error", true);
    }
}


async function handleManualPriceSubmit(event) {
    /** 保存京东可成交卖出价；录入时间完全由服务端生成。 */
    event.preventDefault();
    const submitButton = elements.manualPriceForm.querySelector("button[type='submit']");
    submitButton.disabled = true;
    try {
        await apiRequest(`/manual-prices/${JD_GOLD_SYMBOL}`, {
            method: "PUT",
            body: JSON.stringify({price: elements.manualGoldPrice.value.trim()}),
        });
        elements.manualPriceForm.reset();
        showMessage("京东积存金卖出价已保存。");
        await refreshDashboardData();
    } catch (error) {
        showMessage(describeError(error), "error", true);
    } finally {
        submitButton.disabled = false;
    }
}


async function deleteManualPrice() {
    /** 清除价格后保留黄金持仓，直到用户录入新价格前组合快照不可用。 */
    if (!window.confirm("确定清除当前京东积存金手工卖出价吗？")) {
        return;
    }
    try {
        await apiRequest(`/manual-prices/${JD_GOLD_SYMBOL}`, {method: "DELETE"});
        showMessage("手工卖出价已清除。");
        await refreshDashboardData();
    } catch (error) {
        showMessage(describeError(error), "error", true);
    }
}


async function loadDemoPortfolio() {
    /** 请求后端原子载入匿名数据；已有持仓时由后端返回 409，前端不覆盖。 */
    elements.demoButton.disabled = true;
    try {
        await apiRequest("/demo", {method: "POST"});
        showMessage("匿名演示组合已载入。你可以继续编辑或删除这些数据。");
        await refreshDashboardData();
    } catch (error) {
        showMessage(describeError(error), "error", true);
    } finally {
        const mode = document.querySelector("#market-mode").textContent;
        elements.demoButton.disabled = mode !== "fake";
    }
}


async function refreshFromButton() {
    /** 手工刷新也要捕获网络故障，避免产生未处理的 Promise 拒绝。 */
    elements.refreshButton.disabled = true;
    try {
        await refreshDashboardData({announce: true});
    } catch (error) {
        setConnectionStatus("", false);
        showMessage(describeError(error), "error", true);
    } finally {
        elements.refreshButton.disabled = false;
    }
}


async function initialize() {
    /** 页面启动时读取健康状态和资产目录，再载入当前进程内数据。 */
    try {
        const [health, assets] = await Promise.all([
            apiRequest("/health"),
            apiRequest("/assets"),
        ]);
        state.assets = assets;
        renderAssetOptions();
        setConnectionStatus(health.market_data_mode, true);
        await refreshDashboardData();
    } catch (error) {
        setConnectionStatus("", false);
        showMessage(describeError(error), "error", true);
    }
}


elements.holdingForm.addEventListener("submit", (event) => void handleHoldingSubmit(event));
elements.cancelEditButton.addEventListener("click", resetHoldingForm);
elements.manualPriceForm.addEventListener("submit", (event) => void handleManualPriceSubmit(event));
elements.deletePriceButton.addEventListener("click", () => void deleteManualPrice());
elements.demoButton.addEventListener("click", () => void loadDemoPortfolio());
elements.refreshButton.addEventListener("click", () => void refreshFromButton());

void initialize();
