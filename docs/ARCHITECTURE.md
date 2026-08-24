# 模块架构

## 1. 架构原则

- 业务逻辑不依赖具体模型厂商、数据库或行情供应商。
- LLM 负责理解、规划和生成；数值计算、权限与规则由普通代码负责。
- Agent 只能通过工具访问外部世界，所有工具调用均可记录、重放和测试。
- 先采用模块化单体，等边界稳定后再考虑拆服务。

## 2. 逻辑架构

```mermaid
flowchart LR
    UI["CLI / Web UI"] --> API["FastAPI 应用层"]
    API --> WF["Agent 工作流"]
    WF --> CTX["上下文构建器"]
    WF --> LLM["模型网关"]
    WF --> TOOLS["工具注册表"]
    WF --> MEM["记忆服务"]
    WF --> SAFE["规则与人工确认"]
    TOOLS --> MARKET["行情/基金/黄金适配器"]
    TOOLS --> NEWS["新闻与宏观数据适配器"]
    TOOLS --> PORT["资产与风险计算器"]
    TOOLS --> MCP["MCP Client"]
    CTX --> RAG["检索与重排"]
    MEM --> DB["SQLite → PostgreSQL"]
    RAG --> VDB["本地向量存储 → Qdrant"]
    WF --> OBS["日志 / Trace / 评测"]
```

## 3. 计划中的目录

```text
xmz-agent/
├─ apps/
│  ├─ api/                 # FastAPI 入口
│  ├─ cli/                 # 第一阶段命令行入口
│  └─ web/                 # 后期前端
├─ src/finagent/
│  ├─ core/                # 配置、通用 Schema、异常、事件
│  ├─ llm/                 # ModelProvider 接口、云端/本地适配器、路由
│  ├─ agents/              # Agent 定义与提示词
│  ├─ workflows/           # 可恢复的状态图与人工确认
│  ├─ tools/               # 工具协议、注册表、权限、具体工具
│  ├─ portfolio/           # 持仓、估值、暴露与风险计算
│  ├─ data/                # 行情/新闻/宏观数据源适配器
│  ├─ memory/              # 会话、用户偏好、情景/语义记忆
│  ├─ rag/                 # 采集、切分、索引、检索、重排、引用
│  ├─ context/             # 上下文选择、压缩、预算与缓存
│  ├─ mcp/                 # MCP Server 与 Client
│  ├─ guardrails/          # 输入输出校验、风险规则、批准策略
│  ├─ observability/       # 日志、指标、Trace、成本统计
│  └─ evaluation/          # 数据集、打分器与历史回放
├─ tests/
│  ├─ unit/
│  ├─ integration/
│  ├─ contract/
│  └─ eval/
├─ docs/
│  └─ learning-journal/
├─ data/
│  ├─ samples/             # 可提交的匿名样例
│  └─ private/             # 永不提交的个人数据
├─ scripts/
├─ .github/workflows/
├─ pyproject.toml
├─ .env.example
└─ README.md
```

## 4. 关键接口

后续优先稳定接口，而不是先堆实现：

```python
class ModelProvider(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResponse: ...

class MarketDataProvider(Protocol):
    async def get_quote(self, symbol: str) -> Quote: ...

class PortfolioCalculator:
    def calculate(
        self,
        holdings: Sequence[Holding],
        quotes: Sequence[Quote],
    ) -> PortfolioSnapshot: ...

class Retriever(Protocol):
    async def search(self, query: SearchQuery) -> list[Evidence]: ...

class MemoryStore(Protocol):
    async def save(self, memory: MemoryItem) -> None: ...
    async def search(self, query: MemoryQuery) -> list[MemoryItem]: ...
```

这样可以独立替换模型、行情源、数据库和向量库，并对每个模块做假实现和契约测试。

## 5. 推荐技术栈

| 层 | 起步方案 | 进阶方案 | 学习目的 |
|---|---|---|---|
| 语言与工程 | Python、uv、Pydantic、pytest | Ruff、mypy、pre-commit | 类型、依赖、测试与代码质量 |
| 接口与 UI | CLI | FastAPI、Streamlit 或轻量 Web 前端 | API 设计、流式输出、异步编程 |
| Agent | 自写最小 ReAct 循环 | LangGraph 状态工作流 | 先懂原理，再学可靠编排 |
| 模型 | 一种 OpenAI-compatible 云 API | 多供应商网关、Ollama、本地模型 | 结构化输出、路由、降级、成本 |
| 数据库 | SQLite + SQLAlchemy | PostgreSQL + Alembic | 持久化、事务、迁移 |
| RAG | 本地向量索引 | Qdrant 混合检索 + 重排 | ingestion、召回、引用、评测 |
| 任务 | 手动触发 | APScheduler；需要时再引入队列 | 定时简报、失败重试、幂等性 |
| 协议 | 普通 Python 工具 | 官方 MCP Python SDK | 工具发现与跨应用复用 |
| 可观测性 | 结构化日志 | OpenTelemetry / Langfuse 类平台 | Trace、延迟、token、成本与错误 |
| 交付 | 本地运行 | Docker Compose、GitHub Actions | 可复现环境和自动化质量门禁 |

框架不是越多越好。主线只选一套，其他框架作为对比实验写进学习日记。

## 6. 数据与安全约束

- `Quote` 至少包含 `symbol`、`price`、`currency`、`as_of`、`source` 和 `is_delayed`。
- 过期或来源不明的数据不得被表述为实时事实。
- 原始文档、切分片段、引用和生成结论使用不同数据表。
- 调仓建议使用结构化 Schema，并经过规则检查与人工批准节点。
- MCP 工具采用最小权限、参数白名单、超时和审计日志。
- 个人持仓、Key、数据库文件和模型缓存加入 `.gitignore`。

## 7. 已实现的投资组合领域层

`src/finagent/portfolio/` 是独立的纯领域模块，不依赖 LLM、行情 SDK 或数据库：

```text
Holding + Quote
      │ Pydantic 校验
      ▼
PortfolioCalculator
      │ Decimal 确定性计算
      ▼
ValuedHolding + PortfolioSnapshot
```

- `models.py`：定义持仓、行情、单项估值和组合快照，并拒绝 float、负数与无时区行情。
- `rounding.py`：集中规定金额和百分比使用 `ROUND_HALF_UP` 保留两位。
- `calculator.py`：计算成本、市值、盈亏、收益率、权重、类别分布和 HHI。
- `errors.py`：区分重复持仓、重复行情、行情缺失和币种不匹配。

当前没有汇率换算，计算器要求全部持仓和行情使用同一基准币种。组合 `as_of` 取最旧
行情时间，避免用一条较新的行情掩盖其他资产的数据陈旧问题。

## 8. 已实现的市场数据抽象层

`src/finagent/data/` 把通用应用规则与具体供应商调用分开：

```text
CLI / Agent / Portfolio 应用
            │ 批量代码
            ▼
    MarketDataService
      │ 超时、顺序、新鲜度
      ▼
   MarketDataProvider 协议
      ├─ FakeMarketDataProvider → Quote
      ├─ AkShareFundNavProvider → 自有 QuoteCache → Quote
      ├─ GoldApiMarketDataProvider → 自有 QuoteCache → Quote
      └─ RoutingMarketDataProvider（组合 Provider）
            ├─ 显式基金白名单 → AkShareFundNavProvider
            └─ XAU-CNY-GRAM → GoldApiMarketDataProvider
```

- `base.py`：定义最小异步 Provider 协议与资产代码规范化。
- `fake.py`：从内存返回确定性行情，可模拟延迟、缺失和关闭状态。
- `service.py`：统一管理单请求超时、批量顺序、重复代码和行情年龄。
- `errors.py`：隔离缺失、超时、连接、限流、无效响应和陈旧行情异常。
- `cache.py`：使用单调时钟实现进程内 TTL 缓存，只保存已经校验的统一 `Quote`。
- `akshare.py`：在线程中执行 AKShare 同步调用，把开放式基金 DataFrame 转换为每日确认净值。
- `goldapi.py`：使用 httpx 异步请求 XAU/CNY，并把 24K 金价转换为人民币/克。
- `routing.py`：按显式基金代码集合和黄金常量选择子 Provider，不解析响应、不增加第二层缓存，
  也不把子 Provider 的超时或无数据错误改写成路由错误。
- `diagnostics.py`：独立检查两个真实数据源；PyCharm 入口位于
  `scripts/step05_check_real_market_data.py`。
- `scripts/step06_check_market_data_routing.py`：使用两个 Fake Provider 离线展示
  `Service → Router → 子 Provider` 数据流和请求轨迹，不读取 API Key。

当前批量请求采用串行策略，优先保证免费数据源限流友好和错误顺序确定。若后续选定的
真实供应商提供批量端点或允许并发，只需调整 Service/Provider 调度，不改变投资组合
计算器。AKShare 基金净值固定标记为延迟数据；GoldAPI 只表示国际黄金参考价，不能替代
京东积存金实际卖出价。外部响应无效时明确失败，不使用假行情静默降级。

Router 不能仅凭六位数字推断基金，因为基金和股票代码可能重叠。当前由应用在构造 Router
时传入已确认的基金代码集合，并在内存中保存为不可变 ``frozenset``；进程重启时会重新构造。
Phase 7 已完成持仓持久化，但资产目录仍是显式白名单；后续支持动态资产目录时，才从已确认的
资产元数据生成路由集合，不能直接根据六位代码猜测。缓存仍属于具体 Provider，因为基金净值和
黄金价格需要不同 TTL，Router 只负责确定性转发和子 Provider 生命周期。

## 9. 已实现的模拟持仓、资产面板与持久化边界

Phase 6 在既有领域层和行情层之上增加应用服务与 Web 边界；Phase 7 使用 Unit of Work 和
SQLAlchemy Repository 替换正式应用的内存状态，同时保留同一套 Service 与 API：

```mermaid
flowchart TD
    Browser["Jinja2 页面 + 原生 JavaScript/CSS"] --> API["FastAPI /api/v1"]
    CLI["finagent dashboard"] --> Server["Uvicorn"]
    Server --> API
    API --> Dashboard["PortfolioDashboardService"]
    API --> Transactions["TransactionService"]
    Dashboard --> UOW["DashboardUnitOfWork"]
    Transactions --> UOW
    UOW --> Holdings["SqlAlchemyHoldingRepository"]
    UOW --> ManualPrices["SqlAlchemyManualPriceRepository"]
    UOW --> Ledger["SqlAlchemyLedgerTransactionRepository"]
    UOW --> Lots["SqlAlchemyPurchaseLotRepository"]
    Holdings --> SQLite["SQLite finagent.db"]
    ManualPrices --> SQLite
    Ledger --> SQLite
    Lots --> SQLite
    Dashboard --> Market["MarketDataService"]
    Market --> Provider["Fake Provider 或 Real Router"]
    Dashboard --> Calculator["PortfolioCalculator"]
    Provider --> Required["017811 必要基金净值"]
    Provider --> Reference["XAU-CNY-GRAM 可选参考价"]
    ManualPrices --> Gold["JD-ZS-GOLD 必要手工卖出价"]
```

### 9.1 各层职责

- `portfolio/`：保存资产、持仓和估值模型，并用 `Decimal` 完成纯确定性计算；不知道 HTTP、仓库或
  行情供应商。
- `dashboard/`：定义资产目录、仓库协议、Unit of Work 协议和 `PortfolioDashboardService`；负责
  编排持仓状态、事务边界、必要行情、可选黄金参考价和计算器。
- `persistence/`：实现 SQLAlchemy ORM、SQLite Repository 和 Unit of Work；同一次业务写入共享
  一个 AsyncSession，只有全部成功才提交。
- `web/app.py`：只负责请求校验、调用 Service、Decimal 字符串序列化和 HTTP 错误映射；不实现
  金融公式。
- `web/composition.py`：作为组合根，根据 `MARKET_DATA_MODE` 装配 Fake 或 Real Provider；正式
  应用让 Dashboard Service 和 TransactionService 共享同一个 SQLite Unit of Work 工厂，测试与
  离线验收仍可显式装配内存实现。
- `web/templates/` 与 `web/static/`：作为 API 客户端展示结果，不在 JavaScript 中重复收益公式。
- `cli.py`：把 `finagent dashboard` 参数交给 Uvicorn；默认仅监听 `127.0.0.1`，开放局域网时提示
  无认证风险。

### 9.2 完整查询流程

```text
浏览器 GET /api/v1/dashboard
        ↓
FastAPI 调用 PortfolioDashboardService.get_dashboard()
        ↓
在短事务中从 SQLite 读取持仓和手工价格快照
        ↓
关闭数据库事务，不占用连接等待外部接口
        ↓
MarketDataService 查询基金必要行情
        ↓
PortfolioCalculator 计算毛/净口径、权重和 HHI
        ↓
存在京东黄金时，独立查询可降级的国际黄金参考价
        ↓
FastAPI 把 Decimal 序列化为字符串后返回网页
```

必要基金行情或京东手工价格缺失时，整个组合快照失败，避免展示残缺资产。国际黄金价格只用于参考，
其 Provider 失败时组合仍然成功，并把参考栏标记为 `unavailable`。空仓直接返回自洽快照，不访问任何
Provider。

### 9.3 状态与生命周期边界

正式应用使用 `SqlAlchemyDashboardUnitOfWorkFactory`：每次 Service 操作创建独立 AsyncSession，
持仓与手工价格写入必须在同一事务中成功后才提交，异常时整体回滚。应用启动时检查
`alembic_version` 是否为当前要求的迁移版本，但不会自动修改用户数据库；结构落后时提示执行
`uv run alembic upgrade head`。FastAPI lifespan 退出时依次关闭 MarketDataService、Router、真实
HTTP Provider 和数据库 Engine，释放 HTTP 与 SQLite 连接池。

`InMemoryDashboardUnitOfWorkFactory` 只保留给自动测试和离线验收。它用状态快照模拟提交与回滚，
从而让同一个 `PortfolioDashboardService` 可以在不修改业务代码的情况下切换存储实现。这正是
Repository 与 Unit of Work 抽象解决的问题。

### 9.4 API 与错误边界

正式 API 使用 `/api/v1`，金融数值统一传输为十进制字符串。主要错误映射如下：

| 场景 | HTTP 状态码 |
|---|---:|
| 不支持的资产、非法参数 | 422 |
| 重复持仓、演示冲突、手工价缺失或过期、账本状态冲突 | 409 |
| 持仓不存在 | 404 |
| 必要行情超时 | 504 |
| 其他必要行情不可用 | 503 |

这个边界保证领域异常不会泄漏为难以理解的 500，同时也不会把数据源失败伪装成正常行情。

## 10. 已实现的交易账本领域服务

Phase 7 的 `TransactionService` 在网页之外先建立可独立测试的交易业务边界：

```text
BuyRequest / SellRequest
        ↓
TransactionService（金额舍入、FIFO、业务校验）
        ↓
SqlAlchemy Unit of Work
        ├── holding_positions：当前状态投影
        ├── ledger_transactions：不可变交易事实
        ├── purchase_lots：各批剩余数量与单位成本
        └── manual_prices：黄金清仓时清除旧手工价
```

### 10.1 金额与批次口径

- 买入金额为 `数量 × 确认单价`，实际支出为买入金额加手续费；手续费计入该批单位成本。
- 卖出金额为 `数量 × 确认单价`，实际到账为卖出金额减手续费；已实现收益等于实际到账减去
  FIFO 批次成本。
- 数量、单价和单位成本最多保存 8 位小数；人民币金额使用 `ROUND_HALF_UP` 统一保留两位。
- 手续费使用平台显示的预计或最终金额，不硬编码产品费率。费率可能依持有时间和渠道变化，
  最终确认结果比通用规则更可靠。
- 卖出试算是只读操作；只有用户明确确认后调用 `record_sell`，才会更新数据库。

### 10.2 期初持仓与历史边界

Phase 6 创建的持仓只有数量和平均成本，没有原始买入流水。系统不会伪造历史交易；第一次通过
交易服务操作前，必须调用 `initialize_opening_position`，由用户提供取得时间并创建明确标记的
`opening` 流水和首个批次。当前版本只允许按时间顺序追加交易，插入更早历史需要重放之后全部
FIFO 结果，留待后续专门的账本重建能力处理。

交易写入共享同一个 AsyncSession。流水、批次和持仓任何一步失败，整个 Unit of Work 回滚；
清仓时持仓被删除，但历史流水和已归零批次继续保留用于审计。

### 10.3 FastAPI 与网页交易流程

交易账本通过同一 `/api/v1` 暴露，不让浏览器直接访问 Repository：

| API | 用途 | 是否写数据库 |
|---|---|---|
| `POST /transactions/opening` | 把已有持仓快照登记为期初 FIFO 批次 | 是 |
| `POST /transactions/buy` | 记录买入，追加流水和批次并更新持仓投影 | 是 |
| `POST /transactions/sell-preview` | 根据当前批次计算到账、成本和预计已实现收益 | 否 |
| `POST /transactions/sell` | 用户确认后重新校验并正式卖出 | 是 |
| `GET /transactions` | 查询按发生时间排序的不可变流水 | 否 |
| `GET /transactions/realized-pnl` | 汇总已确认卖出的已实现收益 | 否 |

网页中的卖出表单不能直接调用确认接口。它先保存原始十进制字符串请求并展示后端试算；用户修改
任何字段都会清除试算，只有点击试算结果中的“确认并写入卖出流水”才提交同一份请求。服务端不会
信任旧试算结果，而是依据数据库最新批次重新计算，因此即使两个页面同时操作，也不会用过期批次
静默落账。

持仓快照 CRUD 仅用于录入尚未建立账本的历史起点。一旦某个代码已经存在流水，API 会抛出
`LedgerManagedHoldingError` 并返回 409，网页把操作栏改为“账本管理”。这个限制防止用户绕过
交易流水直接改数量，造成当前持仓、FIFO 批次和审计历史互相矛盾。

`scripts/step08_check_persistence_ledger.py` 使用一次性 SQLite 文件验收这条完整链路。它先通过
Alembic 创建正式表结构，再让三份先后启动的 FastAPI 应用连接同一个文件，分别完成写入、重启
读取、卖出和再次重启读取。只有重新创建 Engine 后仍能恢复持仓、批次结果和流水，才能证明数据
来自持久化存储，而不是某个进程内对象尚未被释放。脚本固定使用 Fake 行情，结束后删除临时文件。

## 11. Agent 结构化记忆的数据基础

Phase 8 前两个小阶段已经建立记忆领域模型、数据库结构和事务化 Repository，尚未让模型自动
写入长期记忆：

```text
chat_sessions ──< chat_messages
      │                 │
      └────来源──── memory_items ──版本替换──> memory_items
                              │
                              └── memory_events（不保存正文的审计事件）

MemoryService（确定性状态机）
      ↓ 依赖协议
MemoryUnitOfWork
      ├── ConversationRepository
      └── MemoryRepository
              ↓
      同一个 AsyncSession / SQLite 事务
```

- `chat_sessions` 与 `chat_messages` 构成单会话短期记忆；会话可以跨重启恢复，但不会默认注入
  其他会话。
- `memory_items` 只保存候选或用户确认的偏好、约束、关注项、目标和反馈；当前持仓、行情和
  收益仍由只读资产工具实时查询。
- 同一范围和键最多存在一条 ACTIVE 记忆。确认冲突版本时先把旧值标为 SUPERSEDED，不能
  静默覆盖。
- `memory_events` 不外键依赖记忆正文，使用户硬删除 value 后仍能保留不含敏感内容的操作审计。
- 会话摘要只是短期上下文压缩，不会绕过确认流程自动升级为长期记忆。
- `ConversationRepository.append_message` 在事务内分配连续序号，并由会话唯一约束兜底；调用方
  不需要理解数据库编号。
- `SqlAlchemyMemoryUnitOfWork` 让消息、记忆正文和审计事件一起提交或回滚，但不会把资产面板
  Repository 混入记忆服务边界。两个模块只复用同一个 `DatabaseManager` 和 SQLite 文件。
- Repository 使用稳定 JSON 保存工具调用、记忆值和审计详情；读取时重新经过 Pydantic 领域模型
  校验，损坏数据会明确失败，不会静默变成空内容。
- 模型输出只能解析为不含 `status` 的 `MemoryCandidateCreate`。候选必须来源于真实 user 消息，
  经过凭据字段过滤后才能保存；只有显式用户确认方法能够产生 ACTIVE 状态。
- 确认同身份新候选时，旧 ACTIVE 与新版本在一个事务中分别迁移为 SUPERSEDED 和 ACTIVE；
  `version` 与 `supersedes_id` 保留完整历史，即使上一版本已自然过期也不会从 version 1 重算。
- TTL 从候选创建的服务端时间开始计算；`expires_at <= now` 即视为到期。读取 Agent 可用记忆时
  会先把到期 ACTIVE 项迁移为 EXPIRED，因此候选、拒绝、过期和删除内容都不会进入上下文。
- 敏感过滤递归检查结构化字段名与明显凭据标签，错误消息不回显正文。审计事件只允许版本号、
  状态、原因代码、是否过期和关联 UUID 等白名单字段，不接收模型自由文本。

### 11.1 会话窗口与滚动摘要

`ConversationService` 把数据库中的原始消息划分为完整对话轮次：

```text
user
  ├── assistant 最终回答
  └── assistant tool_calls
          ├── tool result 1
          ├── tool result 2
          └── assistant 最终回答
```

摘要只能覆盖最老的完整轮次，不能截断 assistant 工具请求和对应 tool 结果。近期消息数量是软上限；
若一个完整工具轮次本身超过上限，系统会多保留整轮，而不是制造无法发送给模型的孤立 tool 消息。
原始 `chat_messages` 永不因摘要而删除，`summary_until_sequence` 只负责防止摘要与近期窗口重复注入。

滚动摘要通过 `ConversationSummarizer` 协议隔离。`ModelConversationSummarizer` 使用统一
`ModelProvider`、temperature 0 和 `tool_choice=none`；自动测试使用 Fake Summarizer，不访问百炼。
摘要模型调用发生在数据库事务之外，写回前比较旧摘要和覆盖序号；若另一任务已经推进摘要，则
拒绝旧结果覆盖新状态。

### 11.2 上下文组装顺序

`ContextAssembler` 生成的模型消息顺序固定为：

```text
固定 system prompt
  → 已确认长期记忆 system 数据块（可选）
  → 当前会话滚动摘要 system 数据块（可选）
  → summary_until_sequence 之后的原始 user/assistant/tool 消息
```

长期记忆数据块明确声明 value 是用户确认的数据而不是高优先级指令，并且不包含数据库 UUID、
审计事件或候选记忆。未指定资产代码时只注入全局记忆；显式传入资产代码后才加入对应资产范围
记忆。近期原始消息转换成基础 `Message` 后才交给 Provider，不暴露数据库 ID、序号和时间。

### 11.3 持久化 Agent 的整轮提交

`PersistentToolCallingAgent` 把 ContextAssembler 生成的历史接入模型—工具—模型循环，但不会在
推理过程中逐条写数据库：

```text
ContextAssembler
  → 旧上下文 + 本轮 user
  → 模型 / ToolRegistry 循环（内存 working copy）
  → 最终 assistant 回答
  → ConversationService.commit_turn
  → 同一事务保存本轮 user / assistant / tool / final assistant
```

模型失败、步数超限或提交中任意消息写入失败时，本轮消息全部不保存。可纠正的 ToolError 会先
作为结构化 tool 消息反馈模型；只有模型最终完成回答后，这段纠错轨迹才随完整轮次落库。

同一 Agent 实例使用每会话 `asyncio.Lock` 串行化请求，不阻塞其他会话；提交时再比较上下文读取时
的 `session.updated_at`。如果其他进程已推进会话，旧上下文结果会以
`ConversationConflictError` 失败，不会接到已经变化的历史之后。当前注册中心只允许无副作用的
教学工具，下一小阶段再加入共享 Phase 7 SQLite 的只读资产查询工具。

### 11.4 只读资产工具白名单

持久化 Agent 通过独立的 `create_read_only_asset_tool_registry` 查询 Phase 7 已有事实，不直接访问
SQLAlchemy，也不把 Dashboard 或 Transaction Service 的写方法动态暴露给模型：

```text
PersistentToolCallingAgent
  → ToolRegistry（只含三个 get_* 工具）
      ├── get_portfolio_snapshot
      │     → PortfolioDashboardService → 行情 + PortfolioCalculator
      ├── get_holding_record
      │     → PortfolioDashboardService → HoldingRepository
      └── get_transaction_ledger_summary
            → TransactionService → LedgerTransactionRepository
```

组合工具返回当前估值、仓位和行情质量；持仓工具只返回 SQLite 中的数量、均价和费率；账本工具
返回计数、现金流、费用、已实现收益以及最多 20 条最近流水。所有金融值使用十进制字符串。

账本工具不会向模型发送交易 UUID、数据库创建时间或自由文本备注。工具注册中心不包含持仓 CRUD、
买入、卖出试算、确认卖出或自动交易能力。自动测试在执行工具前后比较持仓与流水，确保查询没有
改变 SQLite 状态；Real 模式下组合工具仍可能只读访问外部行情源。
