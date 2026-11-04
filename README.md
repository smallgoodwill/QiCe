# 期策：本地期货量化纸面模拟平台

当前版本是可运行的本地项目，数据链路为浏览器 → Python 本机 API → SQLite。模拟交易包含 CSV 历史回放；另外提供可选 SimNow CTP 测试环境连接和实时行情订阅。实时策略下单目前尚未开放。

## 启动

需要 Python 3.13（项目已在当前 Windows/Python 3.13 环境安装 CTP wheel）。首次安装依赖：

```powershell
Set-Location outputs
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

编辑 `outputs/.env` 填入自己的配置后启动：

```powershell
.\run.ps1
```

浏览器打开 `http://127.0.0.1:8765`。服务仅绑定本机回环地址。运行状态可查看 `http://127.0.0.1:8765/api/health`。停止服务按 `Ctrl+C`。

## 本机密钥和账号配置

配置文件：[outputs/.env](.env)。变量模板见 [outputs/.env.example](.env.example)。文件已加入 `.gitignore`，但内容是本机明文，请勿上传、截图或放进共享目录。服务启动时读取一次；修改后需重启服务。

需要填写：

- `AI_PROVIDER`：默认问答服务商，填写 `deepseek` 或 `glm`；默认 `deepseek`。
- `DEEPSEEK_API_KEY`：DeepSeek API Key。
- `DEEPSEEK_MODEL`：DeepSeek 模型，默认 `deepseek-flash`，也可用 `deepseek-v4-pro`。模型名称以 DeepSeek 当前官方文档为准。
- `GLM_API_KEY`：选用 GLM 时填写。聊天里曾经贴出的旧 Key 应先在 GLM 控制台撤销，再填新 Key。
- `SIMNOW_USERNAME`、`SIMNOW_PASSWORD`：SimNow 模拟账号和密码。
- `SIMNOW_BROKER_ID`、`SIMNOW_MD_ADDRESS`、`SIMNOW_TD_ADDRESS`：从 SimNow 当前控制台复制的经纪商代码、行情前置和交易前置。
- `SIMNOW_APP_ID`、`SIMNOW_AUTH_CODE`、`SIMNOW_PRODUCT_INFO`：控制台提供时填写；AppID 会映射到 CTP 的“产品名称”。
- `GLM_MODEL`：选用 GLM 时的模型，默认 `glm-5`。

DeepSeek 接口采用官方 OpenAI 兼容 Chat Completions 地址 `https://api.deepseek.com/chat/completions`。官方当前提供 `deepseek-flash` 和 `deepseek-v4-pro` 等模型；模型标识可能调整，详见 [DeepSeek API 文档](https://api-docs.deepseek.com/api/create-chat-completion/) 和 [模型列表](https://api-docs.deepseek.com/api/list-models/)。

连接表单也可临时提交账号/密码；服务不会将表单内容写入 `.env` 或数据库。若配置了环境变量，表单留空时会使用 `.env` 中的值。

## 当前页面和数据链路

- **我的策略库：**通过 API 创建、详细查看、编辑、删除策略；新建/编辑表单弹窗显示参数。参数保存在 SQLite。
- **策略回测：**必须导入 CSV；服务端校验行情和参数、计算回测并保存可查询的结果。浏览器与后端断开时不会本地伪算。
- **交易：**从策略库选择策略并启动模拟运行；每条策略拥有独立账户和账本，最多同时运行 10 条。可分别取消；有持仓时按最后处理 bar 收盘价模拟平仓。行情逐 bar 推进，浏览器保持打开时自动运行。
- **SimNow 实时行情：**交易页可使用 `.env` 中的 SimNow 测试环境账号和前置地址，填写合约并查看实时 tick。当前只连接和订阅行情，不会发送委托。CTP 凭据不写入数据库；`.env` 会在服务进程退出后仍留在本机磁盘。请勿把服务暴露到公网。
- **仪表盘、交易清单、品种诊断、每日诊断：**从服务端保存的策略运行记录读取；无数据时显示空状态。
- **智能问答：**可选 GLM 或 DeepSeek。它能解释策略、指标和当前模拟盘上下文，不生成策略或代码，也不提供买卖建议。DeepSeek 默认使用官方 OpenAI 兼容 Chat Completions 接口。

## 行情 CSV

需要 CSV 表头：`timestamp,open,high,low,close,volume`。可选 `event_blackout` 字段，值为 `1/true/yes` 时该 bar 标记为禁入。时间需能被 Python ISO 日期解析，严格递增且无重复；OHLC 合法；支持 30 至 20,000 根 bar。建议统一使用 5 分钟 bar、单一合约、同一时区，并检查夜盘/午休时段缺口与合约换月。不要上传无授权行情。

CSV 由浏览器发送给本机服务端进行回测或模拟盘计算，不会转发给 GLM。当前模拟撮合为简化模型，不涵盖真实盘口、部分成交、保证金、涨跌停、交易所费率细则和换月。

## 持久化与旧文件

新数据位于 `work/runtime/qice.sqlite3`，由 SQLite WAL 模式管理。关闭服务不会清除策略、回测结果或模拟盘状态。旧的 `strategies.json`、`backtests.json` 文件保留作历史备份，不自动迁入，因为旧回测数据源无法核实。需要清理或重置时请先备份数据库文件；当前页面提供模拟盘重置，但不提供全库清空按钮。

安装脚本使用 Python 3.13 对应的 `vnpy_ctp` 6.7.11.4 Windows wheel，并跳过可选桌面 GUI 依赖。连接代码使用 `CtpGateway`（CTP），不是 `CtptestGateway`（CTPTEST）。SimNow 普通仿真账号使用 CTP API“实盘”协议模式完成握手；这不会把 SimNow 模拟账号变成实盘账号。只有期货公司明确要求 CTP 穿透式测试时才应使用 CTPTEST/测试模式。若换 Python 版本或操作系统，需按 [vnpy_ctp 安装说明](https://github.com/vnpy/vnpy_ctp)安装对应原生组件。

## 后续进入实时模拟策略交易前

还需实现 5 分钟 bar 聚合、策略触发、委托/成交/持仓状态机、断线重连及柜台对账，再开放 SimNow 测试单操作。实时行情连接成功不等同于策略启动或委托成功。该功能不接真实券商，不构成交易建议或收益承诺。
