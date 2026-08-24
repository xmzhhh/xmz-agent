# Phase 8：Agent 结构化记忆与只读资产工具

## 阶段目标

Phase 7 已经让持仓和交易流水跨重启恢复，但当前 `ToolCallingAgent` 的消息仍只保存在进程内，
退出 CLI 后无法继续同一会话。Phase 8 将建立持久化短期会话记忆、用户确认的长期结构化记忆，
并让 Agent 通过只读工具访问资产面板使用的同一 SQLite 数据库。

计划版本：`v0.3.1`。

相关 Issue / PR：Issue #17 / PR 待创建。

## 第一小阶段：记忆领域模型与数据库结构

### 本小阶段目标

- 明确短期会话记忆、长期结构化记忆和资产事实数据的边界；
- 建立会话、消息、记忆条目和记忆事件四类领域模型；
- 新增四张 ORM 表和第二份 Alembic 迁移；
- 使用临时 SQLite 验证迁移循环、UTC 时间、外键和 ACTIVE 记忆唯一约束；
- 不调用百炼、AKShare、GoldAPI，也不迁移开发者的个人数据库。

### 短期记忆为什么也要持久化

“短期”描述的是作用范围，而不是存储介质。短期记忆只服务于一个会话，用于理解代词、恢复近期
消息和保持 assistant 工具请求与 tool 结果的顺序；把它写入 SQLite 后，应用重启仍能继续同一
会话，但其他会话不会默认读取这些消息。

`chat_sessions` 保存会话状态和滚动摘要，`chat_messages` 保存原始消息。摘要通过
`summary_until_sequence` 标记覆盖位置，原始消息不会因为摘要而删除。

### 长期记忆与持仓事实为什么必须分开

长期记忆只保存用户确认的稳定偏好、约束、关注项、目标和反馈。当前持仓数量、基金净值、黄金
价格、浮动收益和交易流水随时间变化，必须继续从 Portfolio、Ledger 和 MarketData 工具读取，
不能复制成容易过期的记忆。这样 Agent 可以“记住用户黄金仓位上限为 30%”，但不能把“当前
持有 1.5 克黄金”当作永久事实。

### 四张表的职责

| 表 | 职责 |
|---|---|
| `chat_sessions` | 会话标题、状态、滚动摘要和摘要覆盖位置 |
| `chat_messages` | user、assistant、tool 消息及工具调用关联字段 |
| `memory_items` | 候选、已生效、被替换、已拒绝和已过期的结构化记忆 |
| `memory_events` | 不保存记忆正文的确认、拒绝、替换、过期和删除审计事件 |

消息表使用 `(session_id, sequence_number)` 唯一约束，防止同一会话出现重复序号。会话删除时
消息随之删除；长期记忆的来源外键使用 `SET NULL`，避免删除聊天历史时偷偷删除用户已经确认的
长期偏好。

### ACTIVE 记忆为什么使用部分唯一索引

同一个范围内，同一 `memory_type + memory_key` 只能有一条 ACTIVE 记忆，否则 Agent 不知道
应该使用“黄金上限 30%”还是“黄金上限 20%”。候选记忆允许同时存在，用户确认新版本时，
MemoryService 会先把旧版本改为 SUPERSEDED，再激活新版本。

SQLite 的唯一约束会把多个 `NULL` 当作不同值，因此全局记忆在 ORM 层把 `scope_id` 统一保存
为空字符串，再使用只针对 `status = 'active'` 的部分唯一索引。领域模型对外仍以 `None` 表达
全局范围，不把数据库实现细节泄漏到业务层。

### 删除记忆后为什么仍保留事件

用户要求删除长期记忆时，`memory_items.value_json` 应被硬删除，避免敏感正文继续存在。审计表
不设置到记忆正文的外键，只保存记忆 UUID、事件类型、操作者和白名单元数据，因此可以证明系统
执行过删除动作，但不能恢复被删除的内容。

## 本阶段问题记录

### 1. Alembic check 认为唯一约束被删除后又新增

- 问题现象：迁移可以成功升级，但 `alembic check` 报告移除
  `uq_chat_messages_session_sequence` 并新增 `session_sequence`。
- 产生原因：ORM 显式填写了简写约束名 `session_sequence`，迁移文件使用项目命名约定生成的完整
  名称；两者约束字段相同，但 Alembic 按名称判断为两个对象。
- 排查思路：读取 autogenerate diff，确认没有字段或唯一性差异，只存在约束名称差异。
- 解决方法：ORM 同样使用稳定完整名称 `uq_chat_messages_session_sequence`，随后
  `alembic check` 不再产生差异。
- 学到的知识：数据库约束的字段一致还不够，ORM metadata 与迁移文件的名称也必须一致，否则
  后续自动迁移可能生成无意义的删除和重建操作。

### 2. 同一次 add_all 写入父会话和子消息触发外键失败

- 问题现象：测试在同一事务中 `add_all(ChatSessionRow, ChatMessageRow)`，SQLite 先收到消息
  INSERT，因父会话尚不存在而触发外键错误。
- 产生原因：两个 ORM Row 没有定义对象 relationship，只存在表级 ForeignKey；不能依赖
  SQLAlchemy 根据 `add_all` 参数顺序自动安排不同 Mapper 的写入顺序。
- 排查思路：根据失败 SQL 确认先执行的是 `chat_messages` INSERT，再对照 ORM 是否存在
  relationship 依赖。
- 解决方法：测试和后续 Repository 都显式先提交会话，再写入消息；没有为了测试方便增加当前
  业务不需要的 relationship。
- 学到的知识：外键可以保护数据库完整性，但不会自动替应用层设计聚合写入顺序；Repository
  仍需要清楚表达父记录和子记录的创建流程。

## 当前验证结果

- `uv run python -m pytest -q`：273 个测试通过。
- `uv run ruff check .`：通过。
- `uv run mypy`：100 个源文件通过严格类型检查。
- Alembic：临时空数据库可以升级到 `20260824_01`、降级到 base、再次升级，并且
  metadata 与迁移保持一致。
- `uv build`：成功生成当前 `0.3.0` 开发基线的源码包和 wheel；阶段结束时再统一更新为
  `0.3.1`。
- 测试过程未访问真实行情、模型接口和个人 SQLite 数据库。

## 下一小阶段

实现 ConversationRepository 与 MemoryRepository，并使用同一个 Unit of Work 提供会话消息追加、
候选记忆保存、查询和状态更新的原子事务边界。
