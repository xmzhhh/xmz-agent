# Phase 7 验收手册：SQLite 持久化与交易账本

本手册用于验收 `v0.3.0` 的 SQLite 持久化、期初持仓、买入、FIFO 卖出试算、确认卖出和交易
历史。自动验收固定使用 Fake 行情与系统临时数据库，不读取个人持仓数据库，不访问 AKShare、
GoldAPI 或百炼。

## 1. PyCharm 自动离线验收

在 PyCharm 中选择“运行 → 编辑配置”，新建“Python”运行配置：

```text
名称：Phase 7 持久化与交易账本验收
脚本路径：C:\Users\xmz\Desktop\xmz-agent\scripts\step08_check_persistence_ledger.py
形参：留空
Python 解释器：C:\Users\xmz\Desktop\xmz-agent\.venv\Scripts\python.exe
工作目录：C:\Users\xmz\Desktop\xmz-agent
```

运行配置不需要填写环境变量。脚本会完成以下操作：

1. 在系统临时目录创建一次性 SQLite 文件；
2. 对临时数据库执行正式 Alembic `upgrade head`；
3. 第一份 FastAPI 应用写入持仓快照、期初流水和买入流水；
4. 关闭应用并重新创建 Engine，验证持仓与两笔流水仍然存在；
5. 进行卖出试算，验证试算没有修改数据库；
6. 明确确认卖出，检查剩余持仓与已实现收益；
7. 再次重启应用，验证最终持仓与三笔完整流水仍然存在；
8. 关闭连接并自动删除临时数据库。

通过时应看到类似输出：

```text
=== Phase 7 SQLite 持久化与交易账本离线验收 ===
运行模式：Fake（临时 SQLite，不访问真实数据源）
数据库迁移：已升级到当前 Alembic head
第一进程：期初流水与买入流水已写入 SQLite
第一次重启：持仓 12.00000000 份，流水 2 笔，数据仍然存在
卖出试算：预计到账 15.92 CNY，FIFO 成本 12.00 CNY，数据库持仓仍为 12.00000000 份
确认卖出：剩余 8.00000000 份，累计已实现收益 3.92 CNY
第二次重启：剩余持仓 8.00000000 份，完整流水 3 笔
临时数据库：验收结束后已自动删除
真实网络请求：无
个人数据库修改：无
持久化与交易账本验收：通过
```

## 2. 正式本地数据库迁移

自动脚本只迁移临时数据库。正式资产面板使用的数据库仍要由开发者显式升级，应用不会在启动时
擅自修改数据库结构。

在 Windows CMD 中执行：

```bat
cd /d C:\Users\xmz\Desktop\xmz-agent
uv run alembic upgrade head
```

默认数据库是：

```text
C:\Users\xmz\Desktop\xmz-agent\data\private\finagent.db
```

它已经被 `.gitignore` 排除，不能提交到 GitHub。

## 3. 独立 Fake 网页人工验收

为了不影响日常模拟数据，建议使用专门的验收数据库。

先在 Windows CMD 中迁移该数据库：

```bat
cd /d C:\Users\xmz\Desktop\xmz-agent
set DATABASE_PATH=data\private\phase7-acceptance.db
set MARKET_DATA_MODE=fake
uv run alembic upgrade head
```

然后在 PyCharm 新建模块运行配置：

```text
模块名称：finagent.cli
形参：dashboard
Python 解释器：C:\Users\xmz\Desktop\xmz-agent\.venv\Scripts\python.exe
工作目录：C:\Users\xmz\Desktop\xmz-agent
```

在运行配置的“环境变量”中增加两项：

```text
DATABASE_PATH=data/private/phase7-acceptance.db
MARKET_DATA_MODE=fake
```

启动后访问 <http://127.0.0.1:8000>。

### 3.1 期初持仓

1. 在“录入持仓快照”中选择 `017811`；
2. 数量填写 `10`，持仓均价填写 `3.00`，预计卖出费率填写 `0.5`；
3. 提交后，在“初始化已有持仓”中选择 `017811`；
4. 取得时间选择早于当前时间的日期，建立期初批次；
5. 确认持仓操作栏显示“账本管理”，不再允许通过旧表单直接改数量。

### 3.2 买入或加仓

在“记录买入 / 加仓”中填写：

```text
资产：017811
买入数量：2
成交单价：3.50
实际手续费：0.10
预计卖出费率：0.5
成交时间：当前时间
```

确认后应看到：

- 当前持仓变为 `12` 份；
- 交易记录出现 `opening` 和 `buy`；
- 买入手续费进入该批成本，而不是从收益中丢失。

### 3.3 卖出试算与确认

在“卖出试算与确认”中填写：

```text
资产：017811
卖出数量：4
成交单价：4.00
手续费：0.08
成交时间：不早于上一笔买入
```

第一次点击“先计算卖出结果”，应看到：

```text
卖出金额：16.00
预计到账：15.92
FIFO 成本：12.00
预计已实现收益：3.92
卖后剩余数量：8
```

此时持仓仍应是 `12` 份。修改任一卖出输入后，旧试算区域应自动消失，必须重新试算。

恢复上述参数并重新试算，然后点击“确认并写入卖出流水”。确认后应看到：

- 持仓剩余 `8` 份；
- 交易记录增加一笔 `sell`；
- 累计已实现收益为 `3.92` 元；
- 期初批次优先被消耗，符合 FIFO。

### 3.4 重启持久化

1. 停止 PyCharm 中的 Dashboard；
2. 使用同一个运行配置重新启动；
3. 刷新页面；
4. 确认剩余 `8` 份持仓、三笔交易流水和 `3.92` 元已实现收益仍然存在。

如果重启后为空，优先检查两次运行配置中的 `DATABASE_PATH` 是否完全一致。

## 4. 为什么本阶段不重复 Real 模式验收

Phase 7 的新增能力是 SQLite 和确定性交易账本，不改变 AKShare、GoldAPI 或行情路由。交易价格与
手续费由用户从理财平台确认页手工录入，验收这些规则不需要再次消耗外部 API 额度。

Real 模式行情已经在 Phase 6 验收。若需要回归，可继续参考
[Phase 6 验收手册](PHASE6_ACCEPTANCE.md)，但它不是本阶段每次提交的必做项。

## 5. 当前验收记录

| 检查项 | 状态 | 说明 |
|---|---|---|
| pytest 自动测试 | 已通过 | Fake 行情与 pytest 临时目录，不访问个人数据库 |
| Step 08 离线脚本 | 已通过 | 2026-08-19 完成迁移、两次重启和交易闭环 |
| Ruff | 已通过 | 新脚本和测试无风格错误 |
| mypy | 已通过 | 96 个源文件通过严格类型检查 |
| PyCharm 人工运行 Step 08 | 已通过 | 2026-08-19 由开发者运行，输出符合预期且退出代码为 0 |
| Fake 网页重启验收 | 待开发者按需执行 | 建议使用独立 `phase7-acceptance.db` |

自动验收通过不等于已经执行真实交易。FinAgent 仍然不连接理财平台或券商，不具备自动下单能力。
