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

## 待继续

- 定义持仓、手工报价、交易流水和买入批次 ORM 模型。
- 生成并审查第一份 Alembic 迁移。
- 实现 SQLAlchemy Repository 和跨 Repository 的事务边界。

## 当前验证结果

- `uv run python -m pytest -q`：244 个测试通过。
- `uv run ruff check .`：通过。
- `uv run mypy`：78 个源文件通过严格类型检查。
- `uv run alembic heads/current/check`：异步迁移环境可以启动，且当前空模型没有遗漏迁移。
- `uv build`：成功生成 `finagent-0.3.0` 的源码包和 wheel。
- `git diff --check`：通过；Git 仅提示 Windows 工作区未来可能进行 LF/CRLF 转换。
