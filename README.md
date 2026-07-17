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

## 当前进度

- [x] 第 0 阶段：需求边界、架构和学习路线设计
- [x] 第 1 阶段（第一部分）：最小可运行多轮对话 CLI
- [x] 第 1 阶段（第二部分）：工具抽象层、注册中心和本地教学工具
- [x] 第 1 阶段（第三部分）：Agent 工具调用循环与 CLI 集成
- [x] 第 2 阶段（第一部分）：资产领域模型与投资组合计算引擎
- [x] 第 2 阶段（第二部分）：市场数据协议、假 Provider 与应用级保护
- [x] 第 2 阶段（第三部分）：真实基金净值与国际黄金参考价
- [ ] 第 2 阶段（第四部分）：模拟持仓管理与资产面板
- [ ] 第 3 阶段：记忆与持久化
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
- `scripts/check_real_market_data.py` 可在 PyCharm 中同时完成基金和黄金的真实联网验收；
  pytest 使用假数据和假 HTTP 传输层，不消耗真实 API 额度。
