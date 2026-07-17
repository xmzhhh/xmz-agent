# 学习日记 08：真实行情多数据源路由

日期：2026-07-17  
版本：开发中  
相关 Issue / PR：Issue #10，PR 待创建

## 本阶段目标

在不修改现有 ``MarketDataProvider`` 协议的前提下，实现一个确定性的组合路由 Provider。
上层只向 ``MarketDataService`` 提交资产代码，路由器负责把明确配置的基金交给 AKShare
Provider，把 ``XAU-CNY-GRAM`` 交给 GoldAPI Provider。

## 当前小阶段已经实现

- 新增 ``UnsupportedMarketDataSymbolError``，表示系统没有为资产配置任何行情路由。
- 新增 ``RoutingMarketDataProvider``，通过构造函数注入基金 Provider、基金代码白名单和
  黄金 Provider。
- 基金代码不能仅凭“六位数字”自动认定，当前显式配置 ``017811``。
- Router 不解析响应、不修改 ``Quote``、不增加缓存，也不执行自动重试或错误数据源切换。
- Router 接管两个子 Provider 的生命周期，关闭操作保持幂等。
- 使用 Fake Provider 验证实际请求轨迹，自动测试不访问真实网络。

## 核心原理（用自己的话）

### 1. 路由与数据适配是两项职责

Router 回答“这个资产应该交给谁”；具体 Provider 回答“怎样从该供应商取得统一 Quote”。
如果 Router 开始解析 AKShare DataFrame 或 GoldAPI JSON，它就会重新耦合所有供应商细节。

### 2. 格式不能代替资产语义

六位数字只能说明代码格式，不能证明它一定是基金，因为基金和股票代码空间可能重叠。
因此当前版本使用显式基金代码白名单，不对未配置代码进行猜测。

### 3. 没有路由和数据源无数据必须分开

``UnsupportedMarketDataSymbolError`` 表示请求尚未交给任何 Provider；
``MarketDataNotFoundError`` 表示已经选择了正确 Provider，但供应商没有返回数据。两种异常的
排查方向不同，不能混为一谈。

### 4. 缓存仍由具体 Provider 管理

基金净值和黄金价格的数据变化速度不同，缓存 TTL 也不同。Router 再增加一层缓存会形成
双层过期规则，并让直接调用 Provider 的路径失去缓存，因此 Router 只转发请求。

## 关键设计与取舍

- 当前选择显式 ``fund_symbols``，避免修改整个行情协议并引入 ``AssetType`` 参数。
- 当前只写基金和黄金两个明确分支，不提前设计通用插件注册表或规则引擎。
- 已选择 Provider 后不捕获其领域异常，保证超时、连接失败和无数据仍能被上层区分。
- ``MarketDataService.get_quotes`` 继续保持串行、遇错即停，本 Issue 不引入部分成功模型。

## 遇到的问题

### 问题 1：学习日记被显示为修改，但 Git diff 为空

- **问题现象**：在创建 Issue #10 的功能分支前，``git status`` 持续显示
  ``docs/learning-journal/07-real-market-data.md`` 被修改，但 ``git diff`` 没有输出；
  ``git update-index --refresh`` 和 ``--really-refresh`` 都提示 ``needs update``。
- **产生原因**：系统 Git 配置启用了 ``core.autocrlf=true``，文件当前使用 LF，Git 索引仍保留
  之前工作区行尾状态对应的文件元数据，形成“逻辑内容一致但状态未刷新”的现象。
- **排查思路**：分别比较 HEAD、index 和工作区文件哈希；三者都为
  ``778ca1924e6dca8a5fdbfedbef6254cd816deb99``。随后使用 ``git ls-files --eol`` 检查行尾，
  并确认 ``git diff`` 与 ``git diff --cached`` 均为空。
- **解决方法**：在 Git CMD 执行
  ``git add --renormalize docs/learning-journal/07-real-market-data.md``，重新应用 Git 的行尾
  规范化并刷新索引状态。复查后工作区恢复干净，没有产生暂存差异。
- **学到的知识**：``git status`` 的 modified 不一定代表业务文本发生变化。遇到 diff 为空时，
  应先比较三个区域的内容哈希并检查行尾规则，而不是直接提交或恢复文件。

### 问题 2：pytest 测试通过但无法写入默认缓存目录

- **问题现象**：首次执行路由测试时 11 项全部通过，但 pytest 报告
  ``PytestCacheWarning``，无法写入项目根目录的 ``.pytest_cache``。
- **产生原因**：旧 ``.pytest_cache`` 目录存在 Windows 访问权限异常，当前进程甚至无法读取其
  ACL；这不是测试断言失败，也不是路由代码创建的目录。
- **排查思路**：先区分“测试失败”和“测试框架辅助缓存失败”，确认 11 个测试已经执行成功，
  再检查目录属性、ACL 和 ``.gitignore``。项目已有被忽略的 ``.codex_tmp/``，可安全保存临时产物。
- **解决方法**：复验时增加
  ``-o cache_dir=.codex_tmp/phase5_pytest_cache``，让 pytest 使用项目内可写且不会进入 Git 的目录。
- **学到的知识**：pytest 缓存只用于加速和记录上次失败，不决定测试结论；但正式验收仍应消除
  警告，避免真正的异常被噪声掩盖。

### 问题 3：完整测试无法创建系统 tmp_path

- **问题现象**：首次执行完整测试时 169 项通过，``test_settings_can_load_a_dotenv_file`` 在夹具
  准备阶段报 ``PermissionError: [WinError 5]``，无法访问
  ``C:\\Users\\xmz\\AppData\\Local\\Temp\\pytest-of-xmz``。
- **产生原因**：pytest 默认系统临时根目录存在历史权限问题；错误发生在测试函数运行前，与
  ``Settings`` 或路由实现无关。
- **排查思路**：根据错误栈确认失败位置是 pytest 的 ``tmp_path`` 夹具，而不是项目代码；同时
  观察其余 169 项均已通过。
- **解决方法**：复验时增加
  ``--basetemp=.codex_tmp/phase5_pytest_tmp_20260717``，完整测试随后 170 项全部通过。
- **学到的知识**：测试基础设施失败与业务断言失败需要分开诊断。``--basetemp`` 可以为当前项目
  提供隔离、可清理且权限明确的临时目录。

### 问题 4：补丁让已有 Python 文件出现混合行尾

- **问题现象**：Ruff 格式检查提示 ``errors.py`` 和 ``data/__init__.py`` 需要重新格式化；差异
  显示原文件 CRLF 行与新增 LF 行混在同一文件中。
- **产生原因**：补丁只插入了局部 LF 文本，而两个已有文件在 Windows 工作区使用 CRLF。
- **排查思路**：使用 ``ruff format --diff`` 查看格式差异，确认只有行尾变化，没有业务代码
  重排或无关模块修改。
- **解决方法**：只对本阶段修改的四个 Python 文件执行 Ruff format，统一行尾和排版；没有批量
  格式化其他项目文件。
- **学到的知识**：Windows 项目进行局部补丁后要运行格式检查；格式化范围应限制在任务相关
  文件，避免产生与功能无关的大面积 diff。

## 测试与评测结果

- 路由针对性测试：11 项全部通过，全程使用 Fake Provider，没有访问真实网络。
- 项目完整 pytest：170 项全部通过；使用项目内 ``--basetemp`` 和 ``cache_dir`` 避开旧目录权限。
- Ruff lint：全部通过。
- Ruff format：54 个 Python 文件格式正确。
- mypy strict：54 个源文件和测试文件没有类型问题。
- 路由覆盖场景：基金单独转发、黄金单独转发、未配置代码、子 Provider 异常原样传播、
  单一 Service 批量顺序、遇错即停、子 Provider 关闭和重复关闭。

## Git / GitHub 新知识

- Issue 和 PR 共用编号，因此 Phase 4 的 PR #9 之后，新功能 Issue 编号为 #10。
- 新功能分支为 ``feat/10-market-data-routing``，从 ``v0.2.0`` 所在的 ``main`` 创建。
- 行尾规范化可以只刷新索引状态；执行后仍需同时检查 staged diff 和工作区状态。

## 本阶段面试题

1. Router、具体 Provider 和 MarketDataService 的职责分别是什么？
2. 为什么不能把所有六位数字代码直接路由为基金？
3. ``UnsupportedMarketDataSymbolError`` 与 ``MarketDataNotFoundError`` 有什么区别？
4. 为什么缓存应该继续保留在具体 Provider 中？
5. 为什么子 Provider 超时后 Router 不应该尝试无关的另一个 Provider？

## 仍然不懂的地方

- 将来同时接入股票后，应该继续使用白名单，还是升级为包含资产类型的查询对象。
- 如果两个子 Provider 在关闭时都失败，组合 Provider 应怎样保留多项清理异常。

## 下一小阶段计划

完成路由核心的自动检查后，增加可在 PyCharm 中直接运行的离线路由验收脚本，并同步更新
README 与架构文档，使代码、演示和文档对 Router 的职责描述保持一致。
