# 学习日记 08：真实行情多数据源路由

日期：2026-07-17  
版本：开发中  
相关 Issue / PR：Issue #10，PR 待创建

## 本阶段目标

在不修改现有 ``MarketDataProvider`` 协议的前提下，实现一个确定性的组合路由 Provider。
上层只向 ``MarketDataService`` 提交资产代码，路由器负责把明确配置的基金交给 AKShare
Provider，把 ``XAU-CNY-GRAM`` 交给 GoldAPI Provider。

## 当前阶段已经实现

- 新增 ``UnsupportedMarketDataSymbolError``，表示系统没有为资产配置任何行情路由。
- 新增 ``RoutingMarketDataProvider``，通过构造函数注入基金 Provider、基金代码白名单和
  黄金 Provider。
- 基金代码不能仅凭“六位数字”自动认定，当前显式配置 ``017811``。
- Router 不解析响应、不修改 ``Quote``、不增加缓存，也不执行自动重试或错误数据源切换。
- Router 接管两个子 Provider 的生命周期，关闭操作保持幂等。
- 使用 Fake Provider 验证实际请求轨迹，自动测试不访问真实网络。
- 新增 ``scripts/check_market_data_routing.py``，可在 PyCharm 中直接运行完整离线路由流程。
- 验收脚本通过 ``try/finally`` 只关闭最高层 Service，资源沿 Router 向两个子 Provider 释放。
- 新增脚本自动测试，覆盖成功输出、失败时遇错即停、两个 Provider 仍被关闭和同步入口。
- README 和架构文档已同步 Router、内存白名单、缓存归属与离线验收入口。

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

### 5. 人工验收脚本与自动测试解决不同问题

PyCharm 脚本让开发者直观看到成功的数据流、价格单位和实际请求轨迹；pytest 则能注入空
Provider，稳定验证失败路径、遇错即停和资源关闭。脚本使用 Fake Provider 是为了隔离路由逻辑，
不是重复验证 Phase 4 已经确认的外部 API 可用性。

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

### 问题 5：第一次提交后仓库根目录出现空的未跟踪文件

- **问题现象**：路由核心提交并推送后，``git status --short`` 显示未跟踪文件 ``git``。
- **产生原因**：文件大小为 0，且不是任何项目模块。结合此前 Git CMD 中出现过同类 ``git``、
  ``cd`` 文件，判断是命令输入过程意外创建；没有足够证据确认具体是哪一条命令产生，因此不
  把推测写成确定事实。
- **排查思路**：先读取文件路径、大小和内容，确认它是仓库根目录的零字节普通文件，而不是
  Git 元数据、脚本或用户代码。
- **解决方法**：删除该未跟踪空文件并重新执行 ``git status``；开始第二小阶段前工作区干净。
- **学到的知识**：未跟踪文件也必须先检查内容再删除，不能只根据文件名猜测；Git 提交前要
  使用 ``git status`` 核对任务范围。

### 问题 6：mypy 把验收脚本识别成两个模块

- **问题现象**：脚本和专项测试运行通过，但 mypy 同时把文件识别为
  ``check_market_data_routing`` 与 ``scripts.check_market_data_routing``，报告 source file found
  twice，并把 ``finagent`` 导入误判为缺少类型声明的已安装第三方包。
- **产生原因**：测试通过包路径导入脚本，但原 ``scripts/`` 没有 ``__init__.py``；mypy 扫描
  ``scripts`` 目录时无法为文件确定唯一完整模块名。
- **排查思路**：根据 mypy 的两个候选模块名确认不是函数类型错误，而是包边界不明确；对比
  pytest 能运行但静态分析需要稳定模块路径的差异。
- **解决方法**：新增仅包含中文模块说明的 ``scripts/__init__.py``，明确脚本目录是 Python 包。
  各脚本仍保留 ``if __name__ == "__main__"``，所以 PyCharm 直接运行方式不变。
- **学到的知识**：需要被测试导入的脚本也应拥有明确包身份；``__main__`` 入口解决“何时执行”，
  ``__init__.py`` 解决“以什么模块名导入”，两者职责不同。

## 测试与评测结果

- 路由针对性测试：11 项全部通过，全程使用 Fake Provider，没有访问真实网络。
- 第一小阶段完整 pytest 为 170 项；加入离线脚本测试后最终为 173 项全部通过。完整复验使用
  项目内 ``--basetemp`` 和 ``cache_dir`` 避开旧目录权限。
- Ruff lint：全部通过。
- Ruff format：57 个 Python 文件格式正确。
- mypy strict：57 个源文件、脚本和测试文件没有类型问题。
- 路由覆盖场景：基金单独转发、黄金单独转发、未配置代码、子 Provider 异常原样传播、
  单一 Service 批量顺序、遇错即停、子 Provider 关闭和重复关闭。
- PyCharm 离线脚本：退出代码为 0，输出基金与黄金结果、两个独立请求轨迹，并明确显示没有
  真实网络请求。
- 验收脚本专项测试：3 项全部通过，覆盖成功、失败清理与同步 ``main`` 入口。

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

运行完整 pytest、Ruff 和 mypy，确认新增脚本与文档没有破坏既有功能。随后完成 Issue #10
任务核对、提交第二个原子 commit，并准备 PR 描述与阶段收尾。
