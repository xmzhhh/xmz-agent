# FinAgent：可追溯的 AI 投资研究与资产监控助手

> 面向 Agent 开发求职的渐进式实战项目。它帮助用户整理持仓、监控市场、研究事件并生成带证据的风险提示，但不自动交易，也不替代专业投资建议。

## 为什么做这个项目

FinAgent 不只是一个“接上大模型的聊天页面”。最终版本会覆盖：

- 云端大模型与本地模型的统一调用、路由和降级；
- Tool Calling、结构化输出、工作流编排与人工确认；
- 短期记忆、长期记忆、关系型持久化；
- 金融资料 RAG、混合检索、重排、引用和效果评测；
- 上下文裁剪、摘要、缓存和 token 成本管理；
- MCP Server / Client；
- 定时任务、可观测性、自动化测试、Docker 与 CI；
- 历史事件回放和 Agent 评测，而非只展示一段“看起来不错”的回答。

## 最终产品形态

用户手工录入或导入模拟持仓后，系统可以：

1. 展示资产分布、盈亏和集中度；
2. 监控自选股票、基金与黄金，并按规则产生提醒；
3. 汇总新闻和宏观事件，检索证据后分析对持仓的潜在影响；
4. 生成每日/每周投资研究简报，每个事实带来源与数据时间；
5. 在用户设定的风险偏好和约束下给出仓位调整“方案草稿”；
6. 所有高风险建议必须经过人工确认，系统不直接下单；
7. 保存研究过程，并用历史数据回放评估结论质量。

## 项目文档

- [项目设计](docs/PROJECT_DESIGN.md)
- [模块架构](docs/ARCHITECTURE.md)
- [渐进式路线图](docs/ROADMAP.md)
- [Git 与 GitHub 学习路线](docs/GIT_LEARNING.md)
- [学习日记说明与模板](docs/learning-journal/README.md)
- [第 0 阶段学习日记](docs/learning-journal/00-project-kickoff.md)
- [百炼模型 Provider 学习日记](docs/learning-journal/01-bailian-model-provider.md)
- [CLI 多轮对话学习日记](docs/learning-journal/02-cli-chat.md)
- [工具抽象层学习日记](docs/learning-journal/03-tool-foundation.md)
- [Agent 工具调用循环学习日记](docs/learning-journal/04-tool-calling-agent.md)
- [投资组合领域建模学习日记](docs/learning-journal/05-portfolio-domain.md)
- [市场数据抽象层学习日记](docs/learning-journal/06-market-data-abstraction.md)
- [真实基金净值与国际黄金参考价学习日记](docs/learning-journal/07-real-market-data.md)
- [真实行情多数据源路由学习日记](docs/learning-journal/08-market-data-routing.md)
- [模拟持仓管理与资产面板学习日记](docs/learning-journal/09-portfolio-dashboard.md)
- [SQLite 持久化与交易账本学习日记](docs/learning-journal/10-persistence-ledger.md)
- [Phase 6 验收手册](docs/PHASE6_ACCEPTANCE.md)
- [Phase 7 验收手册](docs/PHASE7_ACCEPTANCE.md)

## 当前进度

- [x] 第 0 阶段：需求边界、架构和学习路线设计
- [x] 第 1 阶段（第一部分）：最小可运行多轮对话 CLI
- [x] 第 1 阶段（第二部分）：工具抽象层、注册中心和本地教学工具
- [x] 第 1 阶段（第三部分）：Agent 工具调用循环与 CLI 集成
- [x] 第 2 阶段（第一部分）：资产领域模型与投资组合计算引擎
- [x] 第 2 阶段（第二部分）：市场数据协议、假 Provider 与应用级保护
- [x] 第 2 阶段（第三部分）：真实基金净值与国际黄金参考价
- [x] 第 2 阶段（第四部分）：真实行情多数据源路由
- [x] 第 2 阶段（第五部分）：模拟持仓管理与资产面板
- [x] 第 3 阶段（第一部分）：SQLite 持久化、交易账本与网页交易流程
- [ ] 第 3 阶段（第二部分）：Agent 结构化记忆
- [ ] 第 4 阶段：RAG 与引用
- [ ] 第 5 阶段：上下文工程与可靠工作流
- [ ] 第 6 阶段：本地模型与模型路由
- [ ] 第 7 阶段：MCP
- [ ] 第 8 阶段：研究型多智能体
- [ ] 第 9 阶段：评测、可观测性、部署与作品包装

## 重要边界

- 默认使用模拟或手工录入的持仓；不接券商交易接口。
- AKShare 提供场外基金最新已确认净值，GoldAPI 提供国际黄金人民币克价参考；两者都不代表
  用户在京东金融或蚂蚁财富中可以立即成交的价格。
- 价格、收益和仓位计算由确定性代码完成，不让 LLM 心算。
- 新闻或研报中的观点与可验证事实分开保存。
- 所有报告标注 `as_of` 时间、来源与不确定性。
- API Key、真实持仓和用户隐私数据不得提交到 Git。

## 开发原则

1. 每个阶段都必须可运行、可测试、可演示。
2. 先构建单 Agent，再引入工作流和多 Agent。
3. 业务模块依赖抽象接口，不直接绑定某一家模型或数据源。
4. 每完成一个阶段，补测试、README、学习日记和 Git 标签。
5. 任何新技术都要回答：解决了什么真实问题，如何验证效果？

## 当前真实市场数据能力

- `AkShareFundNavProvider`：查询六位开放式基金代码，当前已用 017811 完成真实验收；返回
  最新已确认单位净值，并明确标记为延迟数据。
- `GoldApiMarketDataProvider`：查询 `XAU-CNY-GRAM` 国际 24K 黄金人民币克价；该价格只
  用于国际行情观察，不能代替京东积存金实际卖出价。
- 两个 Provider 都把外部响应转换为统一 `Quote`，保留来源、带时区时间和延迟属性，并使用
  进程内 TTL 缓存减少重复请求。
- `RoutingMarketDataProvider` 使用显式基金代码集合选择 AKShare，并把
  `XAU-CNY-GRAM` 交给 GoldAPI；未配置代码在访问外部数据源前明确失败。
- 当前基金路由集合在创建 Router 时加载到内存，默认演示代码为 017811；持仓虽然已经持久化，
  但动态扩展资产目录与基金白名单仍属于后续能力，不能通过六位代码格式猜测资产类型。
- `scripts/step05_check_real_market_data.py` 可在 PyCharm 中同时完成基金和黄金的真实联网验收；
  pytest 使用假数据和假 HTTP 传输层，不消耗真实 API 额度。
- `scripts/step06_check_market_data_routing.py` 可在 PyCharm 中离线展示一个 Service 如何把基金和黄金
  分别路由到两个 Fake Provider，并打印可核对的实际请求轨迹。

## 当前模拟持仓与资产面板能力

Phase 6 把持仓仓库、行情路由和确定性计算器连接成了可操作的 FastAPI 应用；Phase 7 当前分支
已经把持仓与手工报价从进程内存切换为 SQLite。当前版本支持：

- 手工新增、修改和删除 `017811` 基金与 `JD-ZS-GOLD` 京东积存金模拟持仓；
- 为京东积存金录入带服务端时间的手工卖出价，超过 15 分钟后拒绝继续估值；
- 展示毛市值、预计卖出费、预计到账金额、净盈亏、净收益率、仓位和 HHI；
- 在 Fake 模式载入固定匿名演示组合，不访问外部网络；
- 在 Real 模式查询 AKShare 基金净值，并把 GoldAPI 国际金价作为独立参考信息；
- 记录期初持仓、买入和卖出流水，使用 FIFO 批次计算已实现收益；
- 卖出必须先调用只读试算，再由用户点击确认按钮写入 SQLite；
- 通过 `/api/v1` 提供持仓、价格、面板、交易账本、演示数据和健康检查 API。

### 在 PyCharm 启动网页

首次启动 Phase 7 版本前，需要在 Windows CMD 中把数据库结构升级到代码要求的版本：

```bat
cd /d C:\Users\xmz\Desktop\xmz-agent
uv run alembic upgrade head
```

该命令会按迁移历史创建 `data/private/finagent.db`。数据库属于本机私有运行数据，已被 Git 忽略，
不应提交到 GitHub。应用启动时只检查结构版本，不会擅自自动迁移；若未执行升级，会给出明确提示。

先确认项目解释器为 `C:\Users\xmz\Desktop\xmz-agent\.venv\Scripts\python.exe`，然后建立 Python
运行配置：

```text
模块名称：finagent.cli
形参：dashboard
工作目录：C:\Users\xmz\Desktop\xmz-agent
```

运行后打开 <http://127.0.0.1:8000>；FastAPI 自动接口文档位于
<http://127.0.0.1:8000/docs>。默认 `MARKET_DATA_MODE=fake`，不需要模型 Key 或 GoldAPI Key，
适合开发和演示。如果需要手机从可信局域网访问，可把形参改成
`dashboard --host 0.0.0.0 --port 8000`，但当前网页没有登录认证，不能暴露到公网。

### 离线验收

在 PyCharm 直接运行 `scripts/step07_check_portfolio_dashboard.py`，可以在不启动端口、不读取 `.env`
且不消耗外部 API 额度的情况下，验证网页资源、匿名组合、组合估值和手工黄金价格更新。Real 模式
和手机局域网验收步骤见 [Phase 6 验收手册](docs/PHASE6_ACCEPTANCE.md)。

在 PyCharm 直接运行 `scripts/step08_check_persistence_ledger.py`，可以在系统临时目录中执行正式
Alembic 迁移，并通过两次服务重启验证期初持仓、买入、卖出试算、确认卖出、交易流水和已实现
收益确实保存到 SQLite。脚本结束后自动删除临时数据库，不读取个人数据库。运行配置和网页人工
验收步骤见 [Phase 7 验收手册](docs/PHASE7_ACCEPTANCE.md)。

### 当前限制

- 正式网页的持仓与手工价格已保存到本地 SQLite，重启后不会清空；离线验收脚本仍使用内存仓库，
  保证每次运行结果固定且不改动个人数据库；
- “录入持仓快照”只用于迁移已有数据；建立期初流水后，该资产由交易账本管理，不能再绕过
  买入/卖出接口直接修改或删除持仓；
- 当前费用使用用户从平台确认页录入的实际或预计金额，不自动抓取蚂蚁财富、京东金融费率；
- 当前不支持补录早于最新流水的历史交易，也不支持撤销或修改已经确认的流水；
- 没有登录、多人隔离、自动交易或券商下单能力；
- `XAU-CNY-GRAM` 只用于国际黄金参考，不能作为持仓录入。
