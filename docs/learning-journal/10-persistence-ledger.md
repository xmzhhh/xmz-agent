# Phase 7：SQLite 持久化与交易账本

## 阶段目标

Phase 6 的持仓和手工报价只存在于进程内存中，服务重启后会清空。本阶段使用 SQLite、
SQLAlchemy 异步 ORM 和 Alembic 建立持久化数据源，并进一步增加交易账本、买入记录、
卖出预估和已实现收益，为下一阶段的 Agent 结构化记忆提供稳定的数据基础。

计划版本：`v0.3.0`。

## 第一小阶段：数据库基础设施

### 本阶段完成内容

- 增加 SQLAlchemy、aiosqlite 和 Alembic 依赖。
- 增加 `DATABASE_PATH`，并把相对路径统一解析到项目根目录。
- 建立共享 AsyncEngine、独立 AsyncSession 工厂和关闭生命周期。
- 为每条 SQLite 连接启用外键约束和 5 秒忙等待超时。
- 建立 Alembic 异步迁移环境，但暂不创建业务表；下一小阶段随 ORM 模型生成首份迁移。
- 使用临时数据库测试连接，不读写用户的真实资产数据库。

### 为什么不能共享一个 AsyncSession

Engine 和 Session 的职责不同：Engine 管理连接池，可以由整个应用共享；Session 保存当前
事务及 ORM 对象状态，属于一次业务操作。若多个 FastAPI 请求共享同一个 Session，一个
请求的回滚、提交或异常可能影响另一个请求。因此应用只共享 `async_sessionmaker`，每次
Repository 操作或业务事务再创建独立 Session。

### 为什么不用 metadata.create_all

`create_all` 只能把当前模型快速创建出来，却没有数据库版本历史，也不能清楚表达字段重命名、
数据迁移和约束变化。本项目使用 Alembic 保存每一次结构变化，使代码版本与数据库版本都能
被审查和回退。

## 第二小阶段：ORM 数据模型与首份迁移

### 当前状态与历史账本为什么要同时保存

资产面板需要快速读取当前数量和平均成本，因此使用 `holding_positions` 保存当前状态；如果
每次刷新页面都从全部交易重新计算，数据越多查询越慢，规则升级也更容易让历史页面结果变化。

另一方面，只保存当前持仓无法回答“什么时候买入、卖出了多少、费用是多少”。因此
`ledger_transactions` 保存不可变的交易事实，`purchase_lots` 保存初始持仓和买入形成的
批次。以后记录交易时，TransactionService 会在同一个数据库事务中同时追加历史并更新当前
状态：任意一步失败就整体回滚。

### 四张业务表

- `holding_positions`：以资产代码为主键，保存数量、平均成本、预计卖出费率、币种和更新时间。
- `manual_prices`：保存用户录入的京东积存金卖出价和录入时间。它允许先于持仓录入，因此不
  外键依赖当前持仓。
- `ledger_transactions`：保存 opening、buy、sell、adjustment 四类流水以及数量、价格、费用、
  现金金额、已实现收益和业务发生时间。历史流水不依赖当前持仓，清仓后仍可查询。
- `purchase_lots`：关联产生批次的初始持仓或买入流水，保存原始数量、剩余数量、取得时间和
  已分摊买入费用的单位成本，为基金先进先出赎回费估算提供数据。

### 数据库约束的作用

Pydantic 和 Service 会在应用入口校验数据，但数据库仍设置正数量、正价格、费率范围、币种、
交易类型和外键约束。原因是将来可能有迁移脚本、后台任务或其他工具直接写库，不能假设所有
数据永远只从一个 API 进入。数据库约束是最后一道防线。

### SQLite 时间为什么需要自定义类型

SQLite 没有真正的带时区时间类型。`UTCDateTime` 在写入时拒绝无时区时间，并统一转换成 UTC
后保存；读取时重新附加 UTC 时区。这样手工价格是否超过 15 分钟、基金批次持有多少天等判断
不会因为电脑时区或夏令时产生歧义。迁移文件只创建标准 `DATETIME`，不依赖应用自定义类型。

## 问题记录

### 1. 新分支后面仍显示 Phase 6 的提交说明

- 问题现象：`git branch -vv` 中，`feat/15-persistence-ledger` 后面显示
  `[Phase 6] 实现模拟持仓管理与资产面板 (#14)`。
- 产生原因：`git branch -vv` 展示的是分支当前指向的最后一条提交，而不是分支描述。
  新分支刚从 `main` 创建，两者自然暂时指向同一个 Phase 6 合并提交。
- 排查思路：同时检查分支名、提交哈希和工作区状态，确认分支已经正确创建且没有未提交文件。
- 解决方法：无需修改；产生第一条 Phase 7 提交后，旁边的文字会自动变成新提交说明。
- 学到的知识：Git 分支本质上是指向某条提交的可移动指针，分支刚创建时不会自动产生新提交。

### 2. 已安装包版本曾停留在 0.1.0

- 问题现象：执行 `uv add` 时，输出显示卸载 `finagent==0.1.0`，再安装项目当前的
  `finagent==0.2.2`。
- 产生原因：项目源码版本已经更新，但虚拟环境中的 editable 安装元数据没有在每次只改版本号时
  自动刷新。
- 排查思路：对比 `pyproject.toml` 的版本和 uv 安装输出，确认不是依赖解析降级。
- 解决方法：本次 `uv add` 重新构建并安装了当前项目；Phase 7 同时把目标版本更新为 `0.3.0`，
  后续执行 `uv sync` 会继续保持一致。
- 学到的知识：源码文件、锁文件和虚拟环境安装元数据是三个不同层次，需要通过 `uv sync` 或
  依赖变更重新同步。

### 3. Windows 读取 alembic.ini 时出现 UnicodeDecodeError

- 问题现象：运行 `uv run alembic heads`、`current` 和 `check` 时，在 Python
  `configparser` 内抛出 `UnicodeDecodeError: 'gbk' codec can't decode byte ...`。
- 产生原因：Alembic 1.19 读取 INI 文件时使用操作系统区域编码；当前 Windows 环境使用
  GBK，而最初创建的 `alembic.ini` 包含 UTF-8 中文注释。
- 排查思路：异常发生在加载迁移环境之前，堆栈指向 `configparser.read(...,
  encoding="locale")`，说明问题不在 SQLite URL 或异步 Engine，而在 INI 文件解码。
- 解决方法：让 `alembic.ini` 只包含 ASCII 字符；需要详细解释的中文注释放在 UTF-8
  Python 文件 `migrations/env.py` 和本学习日记中。
- 学到的知识：Python 源文件的 UTF-8 约定不等于所有配置解析器都会使用 UTF-8。遇到启动早期
  的乱码异常，应先根据堆栈判断是终端显示问题、文件解码问题，还是业务数据本身损坏。

### 4. uv run pytest 收集测试时找不到 scripts 包

- 问题现象：执行完整 `uv run pytest` 时，两个验收脚本测试在收集阶段报
  `ModuleNotFoundError: No module named 'scripts'`，针对数据库文件执行的测试则正常通过。
- 产生原因：Windows 直接启动虚拟环境里的 `pytest.exe` 时，模块搜索路径的首项可能是
  `.venv/Scripts`，项目根目录没有按预期加入导入路径，而现有测试需要从根目录导入
  `scripts` 包。
- 排查思路：错误只发生在导入根目录脚本的旧测试，且尚未执行任何 Phase 7 业务代码，说明
  它不是数据库依赖或迁移造成的回归。
- 解决方法：使用当前 Python 解释器按模块运行 `uv run python -m pytest`；PyCharm 中仍使用
  “Python 测试 → pytest”运行配置，并保持工作目录为项目根目录。
- 学到的知识：`pytest.exe` 和 `python -m pytest` 调用的是同一个测试框架，但两种启动方式
  可能产生不同的模块搜索路径。排查收集失败时应先检查导入边界，而不是修改被测业务代码。

### 5. git push 无法连接 github.com:443

- 问题现象：连续执行 `git push` 分别出现连接超时和 `Connection was reset`，本地分支显示
  比远程领先一个提交。
- 产生原因：GitHub 官方服务正常，DNS 也能解析，但本机直连 GitHub 的 TCP 443 失败。
  Windows 浏览器代理已启用在 `127.0.0.1:7897`，Git 仓库却没有代理配置，因此浏览器可访问、
  Git CMD 无法访问。
- 排查思路：依次确认本地提交、remote 地址、DNS、TCP 443、GitHub Status、Git 代理、Windows
  用户代理，并使用 curl 显式经过该代理验证 GitHub 返回 HTTP 200。
- 解决方法：为当前仓库设置本地 Git 代理 `git config --local http.proxy
  http://127.0.0.1:7897` 后重新推送，不重复 commit。
- 学到的知识：commit 是本地操作，push 才需要网络；push 失败不会丢失本地提交。浏览器代理和
  Git 代理也不是同一份配置，应按网络链路逐层排查。

### 6. Alembic 自动迁移引用了未导入的自定义类型

- 问题现象：`alembic revision --autogenerate` 生成的迁移包含
  `finagent.persistence.models.UTCDateTime()`，却没有生成 `finagent` 导入，直接执行会失败。
- 产生原因：Alembic 能识别 TypeDecorator 的 Python 类型表达式，但不知道该类型希望在历史
  迁移中表现为底层 SQLite `DATETIME`，也不会保证自定义模块导入稳定。
- 排查思路：逐行审查自动生成文件，重点检查导入、建表顺序、类型、约束、索引和 downgrade
  逆序，而不是因为命令成功就直接提交。
- 解决方法：迁移文件改用 `sa.DateTime()`；在 `migrations/env.py` 增加 `render_item` 规则，
  以后遇到 `UTCDateTime` 也生成标准物理类型。
- 学到的知识：autogenerate 只生成候选迁移，不是正确性的证明；自定义类型尤其需要人工审查。

### 7. 自动生成迁移的导入顺序未通过 Ruff

- 问题现象：迁移功能测试全部通过，但 Ruff 对 `sqlalchemy` 和 `alembic` 的导入顺序报告
  `I001`。
- 产生原因：Alembic 模板的默认导入顺序与项目 Ruff/isort 规则不完全一致，手工加入空行后仍
  被认为分组错误。
- 排查思路：把迁移逻辑测试与代码风格检查分开，确认是纯格式问题而不是迁移失败。
- 解决方法：按照 Ruff 要求把两个第三方导入放在同一组并调整顺序。
- 学到的知识：代码生成工具的输出仍要经过项目统一质量门禁；“自动生成”不代表自动合规。

## 第三小阶段：SQLite Repository、Unit of Work 与应用切换

### 本小阶段完成内容

- 实现 `SqlAlchemyHoldingRepository` 和 `SqlAlchemyManualPriceRepository`，把领域对象与 ORM 行
  相互转换，但 Repository 自身不调用 `commit()`。
- 定义 `DashboardUnitOfWork` 协议，让持仓仓库与手工价格仓库共享同一个 AsyncSession；Service
  明确决定何时提交，异常或未提交时自动回滚。
- 为原有内存仓库增加快照恢复能力，使自动测试也能验证真正的“全部成功或全部失败”语义。
- 正式 FastAPI 组合根切换到 SQLite；应用启动时严格检查 Alembic 版本，退出时关闭数据库 Engine。
- 保留显式的内存组合根给自动测试和 Phase 6 离线验收，测试不会读写个人数据库。
- 增加应用重启测试，证明同一 SQLite 文件中的持仓与手工报价在应用关闭后仍能被新应用读取。

### ORM 业务表与 Alembic 迁移分别是什么

ORM 业务表是 Python 类对数据库表的长期映射：代码通过 `HoldingPositionRow` 这类对象读写字段，
SQLAlchemy 再生成 SQL。Alembic 迁移则是数据库结构的版本历史：它按顺序记录“创建哪些表、增加
哪些字段、如何回退”。前者描述代码当前希望看到的结构，后者负责把用户已有数据库安全地一步步
升级到该结构，所以二者不能互相替代。

### 为什么增加 Unit of Work

单个 Repository 只能保证一张表的一次操作。如果删除京东黄金持仓成功、清除对应手工报价失败，
数据库就会留下互相矛盾的状态。Unit of Work 把多个 Repository 放入同一事务：Service 完成所有
操作后才调用 `commit()`，中间任一步异常都会回滚。Repository 因此只负责数据访问，不擅自决定
业务事务何时结束。

### 为什么查询行情前要先结束数据库事务

AKShare 或 GoldAPI 可能等待数秒。如果拿着 SQLite 事务等待网络，会延长连接和锁的占用时间。
Dashboard Service 先在短事务中读出持仓快照，退出事务后再请求行情。行情查询不修改持仓，因此
这种设计既保留了一致的输入快照，也减少了数据库被阻塞的机会。

## 本小阶段问题记录

### 8. 两个内存仓库无法模拟跨仓库回滚

- 问题现象：初版内存 Unit of Work 只提供统一接口；若第二个仓库操作失败，第一个仓库已经产生的
  修改不会自动撤销。
- 产生原因：SQLite Session 自带事务日志，而两个 Python 字典彼此独立，没有数据库回滚能力。
- 排查思路：用“删除京东黄金持仓并同步删除手工报价”作为故障场景，检查异常后两份状态能否一起
  恢复。
- 解决方法：进入内存 Unit of Work 时复制两份仓库快照；没有提交或发生异常时同时恢复快照。
- 学到的知识：实现相同 Protocol 不等于拥有相同语义；Fake 也必须忠实模拟测试所依赖的事务行为。

### 9. SQL Repository 中币种类型没有通过 mypy

- 问题现象：ORM 的 `currency` 字段是字符串，而领域对象要求 `Currency` 枚举，mypy 拒绝直接传入。
- 产生原因：数据库物理层使用字符串便于设置约束和迁移，领域层使用枚举提供更强的类型安全。
- 排查思路：确认数据库约束已经限制合法币种，再检查 ORM 到领域对象的转换边界。
- 解决方法：读取 ORM 行时显式执行 `Currency(row.currency)`，非法历史数据会立即失败而不是静默流入
  计算器。
- 学到的知识：Repository 还是防腐层，必须在这里完成存储类型到领域类型的显式转换。

### 10. 新增模块的导入顺序未通过 Ruff

- 问题现象：功能测试通过，但 `database.py` 和 `composition.py` 被 Ruff 报告 `I001`。
- 产生原因：新增标准库、第三方库和项目内模块后，手工排列没有完全符合项目 isort 规则。
- 排查思路：把行为正确性与代码风格分开验证，使用 Ruff 给出的分类定位导入分组。
- 解决方法：只调整相关文件的导入顺序，不格式化或重构无关代码。
- 学到的知识：测试、静态类型和风格检查覆盖不同风险，不能用其中一项通过代替其他质量门禁。

## 待继续

- 实现交易流水与买入批次 Repository。
- 实现加仓、减仓、卖出试算与已实现收益 TransactionService。
- 将交易能力接入 FastAPI 与网页，同时保留当前持仓修正接口的明确语义。

## 当前验证结果

- `uv run python -m pytest -q`：252 个测试通过。
- `uv run ruff check .`：通过。
- `uv run mypy`：85 个源文件通过严格类型检查。
- Alembic 集成测试：空数据库升级到 `20260817_01`、`check`、降级到 base、再次升级均通过。
- `uv build`：成功生成 `finagent-0.3.0` 的源码包和 wheel。
- `git diff --check`：通过；Git 仅提示 Windows 工作区未来可能进行 LF/CRLF 转换。
