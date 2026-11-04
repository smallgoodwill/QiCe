# 期策 · 本地期货量化纸面模拟平台

一个可以完整跑通的量化 demo：**导入行情 → 建策略 → 回测 → 模拟盘逐 bar 回放 → 仪表盘 / 诊断 / 问答**，全部落库在本地 SQLite，浏览器只做展示与交互。

不接券商、不发真实委托、不接实时行情。交易页提供「模拟盘 / 实盘」切换：模拟盘即历史行情回放；实盘页签目前只展示接入前置条件，不连接任何柜台，也不会发出委托。

---

## 快速开始

最简单的方式：**双击项目根目录的 `run.bat`**。首次运行会自动装好 `.venv`，然后起服务并自动打开浏览器（`Ctrl+C` 停止）。想换端口就直接带参数：`run.bat 9000`。

命令行方式：

```powershell
# 1) 首次安装（创建 .venv，只装 python-dotenv 一个必需依赖）
.\scripts\install.ps1

# 2) 编辑 .env 填入 API Key（可留空：AI 问答不用就不填，其余功能照常可用）
notepad .env

# 3) 启动（-Port 9000 换端口，-NoOpen 不自动开浏览器，-Verbose 输出调试日志）
.\scripts\run.ps1
```

浏览器打开 <http://127.0.0.1:8765>。健康检查：<http://127.0.0.1:8765/api/health>。`Ctrl+C` 停止。

> 端口已被占用时会直接报错退出，不会静默复用端口（Windows 上 `SO_REUSEADDR` 允许两个实例绑同一端口，会导致请求随机落到不同数据库连接，这里已显式挡住）。

**没有任何行情数据也能立刻跑通全链路**：回测页和交易页都有「载入内置样例行情」按钮，用的是 `data/samples/sample_5m_synthetic.csv`（240 根合成 5 分钟 bar，明确标注非真实行情）。

### 全链路自检

```powershell
.\scripts\smoke_test.ps1
```

它会另起一个临时服务进程（`QICE_DATA_DIR` 指向系统临时目录，**不碰真实账本**），把 55 项断言跑一遍：引擎不变量、建策略、回测、逐 bar 推进模拟盘、仪表盘、诊断、问答状态、以及密钥泄露 / 目录穿越 / 跨站请求四类安全回归。

命令行也能单独回放（与网页端共用同一个引擎，结果必然一致）：

```powershell
python .\scripts\replay.py data\samples\sample_5m_synthetic.csv --fast 9 --slow 21
```

---

## 目录结构

```
.
├─ run.bat                   ★ 一键启动（双击即可；run.bat 9000 换端口）
├─ app/                      后端（纯标准库 + python-dotenv）
│  ├─ config.py              路径、常量、.env 载入（唯一读取配置的地方）
│  ├─ engine.py            ★ 统一策略引擎：行情校验 + 参数归一 + 信号判定 + 撮合记账
│  ├─ indicators.py          纯函数指标（SMA / EMA / ATR / RSI）
│  ├─ backtest.py            回测编排（训练/测试切分、指标汇总）
│  ├─ trading.py             多策略模拟运行（逐 bar 推进、状态视图）
│  ├─ db.py                  SQLite 持久化（策略 / 回测 / 模拟运行）
│  ├─ views.py               仪表盘与诊断的聚合视图
│  ├─ ai.py                  DeepSeek / GLM 问答（Key 只在服务端）
│  ├─ simnow.py              可选 SimNow(CTP) 行情网关（惰性导入 vn.py）
│  ├─ api.py                 HTTP 路由 + 静态资源白名单 + 来源校验
│  └─ server.py              进程入口（日志、绑定端口、优雅退出）
├─ web/                      前端（服务端只从这里读静态文件）
│  ├─ index.html             单页壳 + 样式 + 通用脚本
│  └─ js/{backtest,strategy-library,trading,dashboard,assistant}.js
├─ data/
│  ├─ samples/               内置合成样例行情
│  ├─ legacy/                旧版 JSON 备份（未自动导入，数据源无法核实）
│  └─ runtime/               SQLite 账本与日志（git 忽略）
├─ docs/                     产品方案、策略与运行说明
├─ scripts/
│  ├─ install.ps1            建 .venv + 装依赖（-WithCtp 额外装 CTP 网关）
│  ├─ run.ps1                启动服务（-Port / -Verbose）
│  ├─ smoke_test.ps1/.py     全链路自检
│  └─ replay.py              命令行回放（复用 app.engine）
├─ requirements.txt          运行期必需依赖（只有 python-dotenv）
├─ requirements-ctp.txt      可选：vnpy + vnpy_ctp
└─ .env.example              配置模板
```

---

## 配置（.env）

`.env` 是本机明文，已加入 `.gitignore`，**不要截图或放进共享目录**。服务启动时读取一次，改完需重启。

| 变量 | 说明 |
| --- | --- |
| `AI_PROVIDER` | `deepseek`（默认）或 `glm` |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL` | DeepSeek Key 与模型（默认 `deepseek-flash`） |
| `GLM_API_KEY` / `GLM_MODEL` | 选用 GLM 时填写（默认 `glm-5`） |
| `SIMNOW_USERNAME` / `SIMNOW_PASSWORD` | SimNow 模拟账号（**交易页已移除该入口**，仅供后端 `app/simnow.py` 网关使用） |
| `SIMNOW_BROKER_ID` / `SIMNOW_MD_ADDRESS` / `SIMNOW_TD_ADDRESS` | 经纪商代码、行情前置、交易前置 |
| `SIMNOW_APP_ID` / `SIMNOW_AUTH_CODE` / `SIMNOW_PRODUCT_INFO` | 控制台提供时填写 |

DeepSeek 走官方 OpenAI 兼容接口 `https://api.deepseek.com/chat/completions`，模型标识可能调整，以官方文档为准。

也可以完全不写 `.env`：在「智能问答」页临时填 Key（只存服务进程内存，重启即失效）。交易页已不再提供 SimNow 连接表单，如需该网关请直接调用 `/api/simnow/*`。

---

## 页面与数据链路

| 页面 | 行为 | 写入位置 |
| --- | --- | --- |
| 总览仪表盘 | 策略数、模拟权益、已实现盈亏、最近回测、权益曲线、最近成交 | 读 `trading_runs` / `backtests` |
| 我的策略库 | 新建 / 详细 / 编辑 / 删除策略 | `strategies` |
| 策略回测 | 导入 CSV → 服务端校验并回测 → 保存结果、曲线、逐笔、分段统计 | `backtests` |
| 交易 | 从策略库选策略 + 导入 CSV → 启动模拟运行，逐 bar 自动推进 | `trading_runs` |
| 品种 / 每日诊断 | 基于当前回放的真实行情与成交算 ATR、胜率、回撤、当日信号 | 读 `trading_runs` |
| 交易清单 | 汇总所有运行的模拟成交，可导出 CSV | 读 `trading_runs` |
| 智能问答 | 解释策略 / 指标 / 当前模拟盘状态，不给买卖建议 | 不落库 |

### 业务口径（改代码要同步改这里和页面文案）

- **信号在 bar 收盘确认，下一根 bar 开盘价加减滑点成交**，绝不在信号当根成交。
- 回测在训练/测试切分点**强制平仓**，训练段盈亏不带入测试段。
- 手续费按「每边每手」计，一手来回记两次；`atrMaxPct` 对外用百分数（`0.4` = 0.4%），内部换算成小数。
- 模拟盘每根 bar 只做一件事：用**上一根已收盘 bar** 的信号，在**当前 bar 开盘**撮合。回测就是循环调 `advance()`，模拟盘就是每次 HTTP 请求调一次 —— 所以两者结果必然一致。

---

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 + 计数 |
| GET | `/api/market/sample` | 内置合成行情 |
| GET/POST | `/api/strategies` | 策略列表 / 新建或更新 |
| DELETE | `/api/strategies/<id>` | 删除策略 |
| GET/POST | `/api/backtests` | 回测列表 / 运行并保存 |
| GET | `/api/backtests/<id>` | 单次回测完整结果 |
| GET/POST | `/api/trading/runs` | 运行列表 / 启动运行 |
| GET | `/api/trading/runs/<id>` | 单条运行快照 |
| POST | `/api/trading/runs/<id>/{step,cancel,pause,resume}` | 推进 / 取消 / 暂停 / 继续 |
| DELETE | `/api/trading/runs/<id>` | 删除已完成或已取消的运行 |
| GET | `/api/trading/trades` | 所有运行的模拟成交 |
| GET | `/api/dashboard` · `/api/diagnostics` | 仪表盘 / 诊断 |
| GET | `/api/ai/status` · POST `/api/ai/config` · POST `/api/ai/chat` | 问答 |

---

## 行情 CSV

必需表头 `timestamp,open,high,low,close,volume`，可选 `event_blackout`（`1/true/yes` 标记该 bar 事件禁入）。

- 时间需能被 ISO 8601 解析（如 `2025-02-17T09:00:00+08:00`），**严格递增且不重复**；
- `low ≤ min(open, close) ≤ max(open, close) ≤ high`，数值必须有限，成交量非负；
- 支持 30 – 20,000 根 bar；
- 建议单一合约、统一时区、统一周期，并自行核对夜盘/午休缺口与换月。

---

## 安全边界

本项目虽然只监听 `127.0.0.1`，但浏览器里的任意网页都能向本机端口发请求，因此做了三层防护：

1. **静态根固定为 `web/`**，且只放行白名单后缀；任何以 `.` 开头的路径（`.env` / `.venv` / `.git`）一律 404。旧实现把项目根当静态根，`GET /.env` 能直接下载到 API Key 和 SimNow 密码。
2. **来源校验**：校验 `Host`（防 DNS rebinding）与 `Origin`（防跨站）。
3. **写操作强制 `Content-Type: application/json`**，跨站表单/图片等简单请求无法伪造该头。

API Key 只在服务端使用，`/api/ai/status` 只回传「是否已配置」，永不回传 Key 本身。

---

## 已知边界（不要高估结果）

- 只做**单品种、只做多、固定手数**的简化撮合；不模拟保证金、涨跌停、换月、交易时段、盘口冲击、部分成交与真实交易所费率细则。
- 回测/模拟结果**不代表未来表现**，也不构成任何投资建议。
- 智能问答只解释规则与指标，不生成策略、不预测收益。
- SimNow 连接目前**只连接与订阅行情，不会发送任何委托**。要进入实时策略交易，还需补 5 分钟 bar 聚合、委托/成交/持仓状态机、断线重连与柜台对账。

---

## 本次重构做了什么

| 类别 | 变更 |
| --- | --- |
| 目录 | `outputs/` 平铺结构拆分为 `app/ web/ data/ docs/ scripts/`；删除旧单文件版 `server.py`、`paper.js` 与 pip 临时缓存 |
| 引擎 | 回测与模拟盘原本是**两套独立实现**，已合并为 `app/engine.py` 一条撮合路径。修复了旧回测的 `fast_line[sig-1]` 负索引回绕（会凭空产生"幽灵金叉"）与切分点跳 bar 问题 |
| Bug | `POST /api/trading/runs/<id>/step` 路由分段数判断错误，导致**模拟盘永远不推进**；诊断接口读了不含行情的状态快照，导致**永远显示"没有可分析数据"**；交易清单页引用不存在的元素导致整页脚本中断；SimNow 表单缺「交易前置地址」导致连接按钮直接抛错 |
| 安全 | 修复 `GET /.env` 可下载真实密钥；改为静态白名单 + 拒绝 dotfile + Host/Origin 校验 + 强制 JSON |
| 一致性 | `scripts/replay.py` 不再自带一份引擎（原 ATR 口径用 EMA，与网页端 SMA 不一致），改为复用 `app.engine` |
| 依赖 | 移除代码完全未引用的 `numpy / pandas / ta-lib / loguru / tzlocal`，运行期仅需 `python-dotenv` |
| 自检 | 新增 `scripts/smoke_test.py`：55 项断言覆盖全链路 + 安全回归，跑在临时数据目录上 |

安装 CTP 网关（可选，仅 Windows x64 + Python 3.13 有官方 wheel）：

```powershell
.\scripts\install.ps1 -WithCtp
```

连接使用 `CtpGateway`（CTP），不是 `CtptestGateway`（CTPTEST）。SimNow 普通仿真账号用 CTP 的「实盘」协议模式握手，这不改变账号类型。
