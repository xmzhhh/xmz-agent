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

## 第二小阶段：Repository 与统一事务

### 本小阶段目标

- 定义 `ConversationRepository` 与 `MemoryRepository` 协议，让上层不依赖 SQLAlchemy；
- 实现会话 CRUD、自动编号的消息追加、消息窗口读取和工具调用 JSON 往返；
- 实现长期记忆新增、组合条件查询、状态更新、硬删除和审计事件追加；
- 使用 `SqlAlchemyMemoryUnitOfWork` 让会话消息、记忆正文和事件一起提交或回滚；
- 使用临时 SQLite 验证重启恢复、ACTIVE 唯一约束、来源解绑和删除审计保留。

### Repository 和 Unit of Work 各自负责什么

Repository 像一个“只会存取某类资料的管理员”：会话仓库只处理会话和消息，记忆仓库只处理
长期记忆和审计事件。它们会把 ORM Row 转成严格领域模型，也会把数据库约束错误转换成稳定的
领域异常，但不会自己决定哪些内容值得记忆。

Unit of Work 像一个“整笔操作的总开关”。例如模型提出一条候选偏好时，系统需要同时保存来源
消息、候选正文和 `candidate_created` 事件。三步都成功才调用一次 `commit`；任一步失败，退出
工作单元时全部回滚，数据库不会出现“候选已经存在但找不到来源或审计”的半成品。

### 为什么消息序号由仓库分配

调用 Agent 的代码只知道下一条消息内容，不应该先查询数据库再自己猜序号。会话仓库在同一事务
中读取当前最大序号并加一，Memory Unit of Work Factory 使用进程内异步锁串行化 SQLite 事务，
数据库的 `(session_id, sequence_number)` 唯一约束再作为最后防线。这一设计同时把并发细节挡在
Agent 外面，并保持消息窗口可以按整数序号稳定恢复。

### 为什么记忆事务没有直接合并资产面板事务

记忆和资产面板使用同一个 `DatabaseManager` 与 SQLite 文件，但分别拥有 Memory 和 Dashboard
Unit of Work。候选确认不需要访问持仓仓库，买卖流水也不应意外修改聊天记录。保持两个窄事务
边界能降低模块耦合；下一阶段只读资产工具会在应用服务层组合两类能力，而不是制造一个包含所有
Repository 的“万能事务对象”。

### JSON 如何安全往返

工具调用、结构化记忆值和事件详情包含嵌套数据，因此数据库以 JSON 文本保存。Repository 使用
固定键排序、紧凑分隔符和 UTF-8 中文生成稳定文本；读取时先解析 JSON，再交给 `ToolCall`、
`ConversationMessage`、`MemoryItem` 或 `MemoryEvent` 重新校验。若数据库内容损坏或根节点类型错误，
系统明确抛出 `PersistenceError`，不会用空列表或空对象掩盖数据问题。

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

### 3. ORM 字符串没有转换为 MemoryActor 枚举

- 问题现象：Repository 初版运行 Ruff 正常，但 mypy 报告构造 `MemoryEvent` 时，`actor` 传入了
  `str`，而领域模型要求 `MemoryActor`。
- 产生原因：ORM 为便于 SQLite 约束检查把枚举保存为字符串；从 Row 回到领域模型时漏掉了显式
  枚举转换。
- 排查思路：定位 mypy 指向的构造参数，对比同一转换函数中的 `MemoryEventType(row.event_type)`。
- 解决方法：统一改为 `MemoryActor(row.actor)`，让非法数据库值在边界处立即失败。
- 学到的知识：静态类型检查可以发现测试数据未必覆盖的边界泄漏；ORM 字符串不是领域枚举，转换
  方向的每个字段都要对称处理。

### 4. 测试辅助函数把 UUID 标注成 object

- 问题现象：18 个目标测试全部通过，但 mypy 报告测试辅助函数传给 `MemoryItem.id` 的参数是
  `object`，不是 `UUID`。
- 产生原因：为了表示“任意生成的 ID”写了过宽的 `object` 类型，运行时虽然传入的都是 UUID，
  静态类型系统却无法证明这一点。
- 排查思路：根据 mypy 行号检查辅助函数签名，而不是修改已经正确的领域模型类型。
- 解决方法：把辅助函数参数精确标注为 `UUID` 并补充对应导入。
- 学到的知识：测试代码也属于受维护的工程代码；过宽类型会削弱 mypy 对测试构造数据的保护。

### 5. git diff --check 显示 LF 将转换为 CRLF 的提示

- 问题现象：`git diff --check` 退出码为 0，但对四个已有文件提示工作区中的 LF 下次可能转换为
  Windows CRLF。
- 产生原因：Windows Git 的行尾转换设置与这些文件当前的工作区行尾形式不同；提示不代表存在
  多余空格、冲突标记或无效补丁。
- 排查思路：同时检查命令退出码和实际输出，确认没有 `trailing whitespace` 等 diff 错误，并且
  不对整个仓库做与本任务无关的行尾重写。
- 解决方法：保留项目当前 Git 行尾策略，本阶段不批量格式化文件；`git diff --check` 实际通过。
- 学到的知识：警告和失败要通过退出码及具体内容区分；为了消除无害提示而改写全仓库会制造大量
  无关 diff，违反外科手术式修改原则。

## 当前验证结果

- `uv run python -m pytest -q`：280 个测试通过；其中本小阶段目标测试 18 个通过。
- `uv run ruff check .`：通过。
- `uv run mypy`：106 个源文件通过严格类型检查。
- Alembic：临时空数据库可以升级到 `20260824_01`、降级到 base、再次升级，并且
  metadata 与迁移保持一致。
- `uv build`：成功生成当前 `0.3.0` 开发基线的源码包和 wheel；阶段结束时再统一更新为
  `0.3.1`。
- `git diff --check`：通过；仅出现 Windows 行尾转换提示，没有空白错误。
- 测试过程未访问真实行情、模型接口和个人 SQLite 数据库。

## 下一小阶段

实现 `MemoryService` 的候选创建、用户确认、拒绝、冲突替换、TTL 过期和审计详情白名单，使
Repository 之上的状态迁移由确定性 Python 规则管理，而不是由大模型直接改数据库状态。
