# Phase 6 验收手册：模拟持仓管理与资产面板

本手册用于验收 `v0.2.2` 的模拟持仓与资产面板。自动验收固定使用 Fake 数据，不访问 AKShare、
GoldAPI 或百炼；Real 模式与手机局域网验收需要开发者主动执行。

## 1. 自动离线验收

在 PyCharm 新建 Python 运行配置：

```text
脚本路径：C:\Users\xmz\Desktop\xmz-agent\scripts\step07_check_portfolio_dashboard.py
形参：留空
Python 解释器：C:\Users\xmz\Desktop\xmz-agent\.venv\Scripts\python.exe
工作目录：C:\Users\xmz\Desktop\xmz-agent
```

脚本直接在内存中请求 FastAPI 应用，不监听端口，也不会读取 `.env`。通过时应看到：

```text
页面资源：HTML、CSS、JavaScript 均可读取
匿名演示持仓：('017811', 'JD-ZS-GOLD')
组合毛市值：2100.00 CNY
预计到账金额：2091.20 CNY
更新后组合毛市值：2151.00 CNY
真实网络请求：无
资产面板验收：通过
```

## 2. Fake 网页人工验收

在 PyCharm 新建模块运行配置：

```text
模块名称：finagent.cli
形参：dashboard
Python 解释器：C:\Users\xmz\Desktop\xmz-agent\.venv\Scripts\python.exe
工作目录：C:\Users\xmz\Desktop\xmz-agent
```

打开 <http://127.0.0.1:8000>，依次确认：

- 页面顶部显示 `FAKE` 模式；
- 点击“载入匿名演示组合”后出现基金和京东黄金两项持仓；
- 修改数量、持仓均价或预计卖出费率后，后端重新计算毛/净收益；
- 更新京东黄金卖出价后，黄金市值和组合汇总同步变化；
- 删除京东黄金持仓后，关联的手工价格也被清除；
- 停止并重新启动程序后，持仓恢复为空，符合内存仓库边界。

## 3. Real 模式人工验收

Real 模式会联网访问 AKShare 与 GoldAPI，可能消耗 GoldAPI 额度。不要把真实 Key 或持仓提交到 Git。

在本地 `.env` 中配置：

```dotenv
MARKET_DATA_MODE=real
GOLDAPI_API_KEY=你的_GoldAPI_Key
```

然后仍使用 `finagent.cli` 模块和 `dashboard` 形参启动。依次确认：

- 页面顶部显示 `REAL` 模式，匿名演示按钮不可用；
- 新增 `017811` 后，面板显示 AKShare 的最新确认净值、来源、时间和延迟标记；
- 新增 `JD-ZS-GOLD` 后，在未录入京东卖出价前，持仓存在但组合快照明确失败；
- 录入当前京东卖出价后，组合恢复估值；
- GoldAPI 成功时显示国际黄金参考价，失败时只把参考栏标记为不可用，不破坏必要持仓估值；
- Real 模式不调用百炼，因此可以不配置 `LLM_API_KEY`。

验收结束后建议把 `.env` 中的 `MARKET_DATA_MODE` 改回 `fake`，避免日常调试误用真实额度。

## 4. 手机可信局域网验收

当前面板没有登录认证，只能在可信网络短时测试，不能做路由器公网端口映射。

1. 在 PyCharm 把形参改为 `dashboard --host 0.0.0.0 --port 8000`。
2. 启动后确认控制台打印“无认证风险”提示。
3. 在 Windows CMD 运行 `ipconfig`，找到当前无线网卡的 IPv4 地址。
4. 让手机连接同一个可信 Wi-Fi，访问 `http://<电脑IPv4地址>:8000`。
5. 确认页面在手机上无整页横向溢出，并完成一次持仓查看或 Fake 演示操作。
6. 验收完成后停止服务，恢复默认 `127.0.0.1` 绑定。

若手机无法访问，先检查两台设备是否在同一局域网，再检查 Windows 防火墙是否允许本次 Python
进程的专用网络访问；不要为了测试直接关闭全部防火墙。

## 5. 当前验收记录

| 检查项 | 状态 | 说明 |
|---|---|---|
| pytest Fake 自动测试 | 已通过 | 不访问真实数据源 |
| Step 07 离线端到端脚本 | 已通过 | 2026-08-17 由开发者在 PyCharm 运行，退出代码为 0 |
| 本机 Fake 网页交互 | 已通过 | 已完成 CRUD、响应式页面和控制台检查 |
| Real 模式 | 已通过 | 2026-08-17 已验证 AKShare、手工京东卖价与 GoldAPI 参考价 |
| 手机可信局域网 | 已通过 | 2026-08-17 手机成功访问 Fake 面板，移动布局无整页横向溢出 |

Phase 6 的 Fake、Real 和手机人工验收现已全部完成。创建唯一 PR 前，应再次执行完整 pytest、Ruff、
mypy、`git diff --check` 和构建检查，并确认学习日记与实际结果一致。
