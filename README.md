# 期策 · 本地期货量化纸面模拟平台

一个可以完整跑通的量化 demo：**选策略 + 选时间段 → 区间模拟出一段盈亏 → 回测对照 → 仪表盘 / 诊断 / 问答**，全部落库在本地 SQLite，浏览器只做展示与交互。

不接券商、不发真实委托。交易页提供「模拟盘 / 实盘」切换：模拟盘是**按区间一次跑完的策略模拟**（行情可以是服务端合成的演示数据，也可以接本机 CTP 采集的真实分钟 K 线）；实盘页签目前只展示接入前置条件，不连接任何柜台，也不会发出委托。

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

> **重复双击不会报错**：如果端口上已经有一个本服务实例在跑，`run.bat` 会认出它、复用并直接打开浏览器（退出码 3，不当作失败）；只有端口被**别的程序**占用时才提示换端口（退出码 1）。要真正重启，先关掉原来那个黑窗口再运行。

> 端口已被占用时会直接报错退出，不会静默复用端口（Windows 上 `SO_REUSEADDR` 允许两个实例绑同一端口，会导致请求随机落到不同数据库连接，这里已显式挡住）。

**没有任何行情数据也能立刻跑通全链路**：交易页的模拟盘用服务端合成的演示行情，选好时间段点「开始模拟」就出买卖点和盈亏；回测页另有「载入内置样例行情」按钮，用的是 `data/samples/sample_5m_synthetic.csv`（240 根合成 5 分钟 bar，明确标注非真实行情）。

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
│  ├─ simulate.py          ★ 区间模拟：选策略 + 来源 + 时间段一次跑完，输出买卖点与逐笔盈亏
│  ├─ marketdata.py          行情来源：CSV 解析 + 按交易时段确定性合成分钟 K 线
│  ├─ trading.py             多策略逐 bar 回放（旧交互保留，页面已不再入口）
│  ├─ db.py                  SQLite 持久化（策略 / 回测 / 模拟运行）
│  ├─ views.py               仪表盘与诊断的聚合视图
│  ├─ ai.py                  DeepSeek / GLM 问答（Key 只在服务端）
│  ├─ simnow.py              可选 SimNow(CTP) 行情网关（惰性导入 vn.py）
│  ├─ feed.py              ★ 实时行情：tick→分钟 bar 聚合 + SSE 广播（ctp / sim 双驱动）
│  ├─ api.py                 HTTP 路由 + 静态资源白名单 + 来源校验
│  └─ server.py              进程入口（日志、绑定端口、优雅退出）
├─ web/                      前端（服务端只从这里读静态文件）
│  ├─ index.html             单页壳 + 样式 + 通用脚本
│  ├─ js/{backtest,strategy-library,trading,live,dashboard,assistant}.js
│  ├─ js/kline-core.js         ★ K 线绘图内核（样式/指标/时间单调性），live 页与仪表盘组件共用
│  └─ vendor/klinecharts.min.js   K 线图表库（9.8.12 UMD，本地化不走 CDN）
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
| 总览仪表盘 | 策略数、模拟权益、已实现盈亏、最近回测、**实时 K 线组件**、权益曲线、最近成交 | 读 `trading_runs` / `backtests` / `live_bars` |
| 我的策略库 | 新建 / 详细 / 编辑 / 删除策略 | `strategies` |
| 策略回测 | 导入 CSV → 服务端校验并回测 → 保存结果、曲线、逐笔、分段统计 | `backtests` |
| 交易 | 选策略 + 行情来源 + 时间段 → **一次跑完区间模拟**，输出买卖点、逐笔盈亏、持仓时长、按日汇总；右侧可切实盘（未接入） | `trading_runs` |
| 实时 K 线 | CTP(SimNow) 真实行情或内置模拟源 → tick 合成 1/5/15/30/60 分钟 K 线；模拟盘成交以买卖点标在对应价格上 | `live_bars`（仅已收口 bar） |

| 品种 / 每日诊断 | 基于当前回放的真实行情与成交算 ATR、胜率、回撤、当日信号 | 读 `trading_runs` |
| 交易清单 | 汇总所有运行的模拟成交，可导出 CSV | 读 `trading_runs` |
| 智能问答 | 解释策略 / 指标 / 当前模拟盘状态，不给买卖建议 | 不落库 |

### 业务口径（改代码要同步改这里和页面文案）

- **信号在 bar 收盘确认，下一根 bar 开盘价加减滑点成交**，绝不在信号当根成交。
- 回测在训练/测试切分点**强制平仓**，训练段盈亏不带入测试段。
- 手续费按「每边每手」计，一手来回记两次；`atrMaxPct` 对外用百分数（`0.4` = 0.4%），内部换算成小数。
- 模拟盘每根 bar 只做一件事：用**上一根已收盘 bar** 的信号，在**当前 bar 开盘**撮合。回测与区间模拟都是循环调 `advance()` 跑到结束，逐 bar 回放是每次 HTTP 请求调一次 —— 三条路径结果必然一致。

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
| GET | `/api/simulate/options` | 区间模拟的行情来源、可用合约、默认参数与区间上限 |
| POST | `/api/simulate` | **跑一段区间模拟**：策略 + 来源 + 起止日期 → 买卖点 / 逐笔盈亏 / 按日汇总，并落库 |
| GET | `/api/simulate/history` | 区间模拟历史记录 |
| GET | `/api/simulate/<id>` | 取回某次区间模拟的完整结果（含 K 线数据） |
| GET/POST | `/api/trading/runs` | 运行列表 / 启动逐 bar 回放（旧接口，页面不再入口） |
| GET | `/api/trading/runs/<id>` | 单条运行快照 |
| POST | `/api/trading/runs/<id>/{step,cancel,pause,resume}` | 推进 / 取消 / 暂停 / 继续 |
| DELETE | `/api/trading/runs/<id>` | 删除已完成或已取消的运行 |
| GET | `/api/trading/trades` | 所有运行的模拟成交 |
| GET | `/api/dashboard` · `/api/diagnostics` | 仪表盘 / 诊断 |
| GET | `/api/ai/status` · POST `/api/ai/config` · POST `/api/ai/chat` | 问答 |
| GET | `/api/live/status` | 实时行情源状态（驱动、tick 数、最新价） |
| GET | `/api/live/bars?symbol=&period=&limit=` | 取 1m/5m/15m/30m/60m K 线 |
| GET | `/api/live/stream?symbol=` | SSE 推流：`hello` 快照 + 每根 bar 变化 |
| POST | `/api/live/start` · `/api/live/stop` | 启动 / 停止采集（`driver` 为 `ctp` 或 `sim`） |
| GET | `/api/trading/runs/<id>/bars` | 取该运行当时的行情，用于 K 线对齐买卖点 |

---

## 实时 K 线

「实时 K 线」页有两种数据源，都在页面上直接切换：

| 数据源 | 前提 | 延迟 | 用途 |
| --- | --- | --- | --- |
| **CTP（SimNow）** | `.env` 填好 `SIMNOW_*`，或页面点「连接 SimNow 实时行情」 | tick 级 | 看真实期货行情，**只读订阅、不下单** |
| **内置模拟源** | 无 | 可调到 120 倍加速 | 没账号或非交易时段时验证链路 |

- **聚合口径**：tick 按北京时间切 1 分钟窗口，累加 O/H/L/C/V；跨分钟先收口上一根再开新根。5/15/30/60 分钟由 1 分钟合成，只在取数时合并，不重复存储。
- **推送**：SSE（`text/event-stream`），浏览器自带断线重连，服务端 15 秒一次心跳。选 SSE 而不是 WebSocket 是因为只需要单向推送，标准库即可实现、不引入依赖。
- **落库策略**：只把已收口的 bar 写进 `live_bars`（每分钟每合约最多一次写），正在形成的那根只留在内存，避免高频写盘。
- **数据源隔离**：`live_bars.source` 记录每根 bar 来自 `ctp` 还是 `sim`，查询默认只取 `ctp`。模拟源按加速倍数生成 bar，时间戳会跑到当前时刻之后；不隔离的话模拟数据会和真实行情混在一张图上，把坐标轴顶歪、价格画成跳空（这是实际踩到的坑）。v3→v4 迁移里已存在的行无法辨别来源，统一标为 `legacy` 并排除在查询之外——宁可图上少几根，也不能把模拟数据当真实行情。
- **历史不回溯**：CTP 只推实时 tick，接上之后从当前时刻开始攒 K 线，页面对此有明确提示。
- **两处入口**：完整图表在「实时 K 线」页（含周期切换、成交标记、对齐回放）；仪表盘顶部另有一个紧凑版组件，只显示当前合约的 K 线与报价，两者共用 `web/js/kline-core.js` 这一份绘图内核，不会出现口径不一致。
- **买卖点**：每笔成交按 `entryTime` / `exitTime` 映射到 K 线坐标，买点挂在该根最低价下方、卖点挂在最高价上方，带自动避让与边界收敛；一根 bar 不到 2px 时标签退化成圆点，避免文字互相压住。实时页展示的是**实时源**的 K 线，模拟盘跑的是单独回放的区间行情，两者不是同一段数据——实时页会提示，并可「载入某次运行的行情并对齐」。
- **注意**：非交易时段 CTP 不会有新 tick；`IF2503` 这类已交割合约也没有行情，请用当月在交易的合约（如 `IF2612`、`rb2601`）。

---

## 策略模拟盘（选一段时间，一次跑完）

「交易 → 模拟盘」回答的是一个问题：**这条策略在这段时间里赚了多少、买卖点在哪、每笔持了多久。**

不再需要准备 CSV。选三样东西即可：

| 选择项 | 说明 |
| --- | --- |
| 策略 | 策略库里保存的策略；不选则用内置 EMA(9)/EMA(21) 兜底，第一次打开也能直接出结果 |
| 行情来源 | **演示行情**：服务端按交易时段（09:30–11:30、13:00–15:00，各 120 根 1 分钟 bar）确定性合成，同一日期永远生成同一段走势，用于验证策略与界面；**真实采集**：本机 CTP 采集并落库的分钟 K 线，只能覆盖开始采集之后的时间 |
| 时间段 | 「最近 1 / 3 / 5 / 10 个交易日」快捷按钮，或直接选起止日期；单次最多 20 个交易日（约 4,800 根 bar） |

跑完直接给出四块结果：

1. **KPI**：净盈亏、收益率、胜率、盈亏因子、最大回撤、交易笔数（附手续费合计与单笔期望）；
2. **K 线 + 买卖点**：红点买入开仓、绿点卖出平仓，可切「全部 / 近 1 日 / 近 1 小时」，悬停看成交价与时间；
3. **逐笔明细**：开仓时间/价、平仓时间/价、持仓时长、手数、毛盈亏、费用、净盈亏、收益率、持仓期间最大浮盈/浮亏、退出原因，可导出 CSV；
4. **按交易日盈亏**：当日成交笔数、胜/负、手续费、当日与累计盈亏、日终权益。

实现要点：

- 行情来源由服务端决定，**演示数据与真实采集数据永不混用**（演示固定 `IF-DEMO`，真实按合约名查 `live_bars.source='ctp'`）；
- 演示行情会截止到「现在」：当天没收盘就只生成已经发生的分钟，不会出现未来时间的 bar；
- 真实来源根数不足时直接说清楚缺多少、怎么补，不会跑出一个「0 笔成交」的空结果；
- 结果写进 `trading_runs`（`tradePrefix=SIM`），所以仪表盘、交易清单、诊断都能读到，也能在历史列表里点回来重看。

---

## 导入行情 CSV（脚本/回测页仍支持）

页面已不再要求上传 CSV，`web/js/backtest.js` 与 `scripts/replay.py` 仍保留解析能力，格式如下。

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
| 自检 | 新增 `scripts/smoke_test.py`：全链路断言 + 安全回归，跑在临时数据目录上（当前 89 项） |
| 模拟盘 | 从「逐 bar 轮询 + 必填 CSV」改为**区间一次跑完**：策略 + 行情来源（演示合成 / CTP 采集）+ 时间段 → K 线买卖点、逐笔盈亏、持仓时长、按日汇总。新增 `app/simulate.py`、`app/marketdata.synthetic_bars()`、`/api/simulate*` |

安装 CTP 网关（可选，仅 Windows x64 + Python 3.13 有官方 wheel）：

```powershell
.\scripts\install.ps1 -WithCtp
```

连接使用 `CtpGateway`（CTP），不是 `CtptestGateway`（CTPTEST）。SimNow 普通仿真账号用 CTP 的「实盘」协议模式握手，这不改变账号类型。
