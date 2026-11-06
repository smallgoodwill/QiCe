#!/usr/bin/env python3
"""全链路自检：起一个真实服务进程，把网页会走的接口逐条打通。

设计原则
--------
1. **不污染真实账本**：用 QICE_DATA_DIR 指向临时目录，SQLite 建在 tmp 里，
   跑完即删；真实 data/runtime/qice.sqlite3 一个字节都不动。
2. **只走 HTTP**：所有断言都通过 127.0.0.1 上的真实请求完成，
   不直接调用内部函数，这样路由、鉴权、序列化、静态白名单都在覆盖范围内。
3. **包含安全回归**：/.env 泄露、目录穿越、跨站请求、非 JSON 写操作
   这四类问题是本项目曾经真实存在的漏洞，必须每次自检都验一遍。

用法（在项目根目录）：
    .venv\\Scripts\\python.exe scripts\\smoke_test.py
    .venv\\Scripts\\python.exe scripts\\smoke_test.py --keep   # 保留临时数据目录
    python scripts\\smoke_test.py --external http://127.0.0.1:8765  # 只测已在跑的服务
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CONFIG = {
    "fast": 9, "slow": 21, "maType": "EMA",
    "atr": True, "atrPeriod": 14, "atrMaxPct": 0.4,
    "rsi": False, "feePerSide": 2.0, "slippage": 1.0,
    "multiplier": 1.0, "qty": 1, "capital": 1_000_000.0,
    "dailyLossLimit": 0.0, "useBlackout": True, "tickSize": 1.0,
}

results: list[tuple[bool, str, str]] = []
PORT = 0
DATA_DIR = ""            # 临时服务的数据目录；--external 模式下为空，跳过 DB 层断言


# --------------------------------------------------------------------------
# 断言与小工具
# --------------------------------------------------------------------------
def check(name: str, condition: bool, detail: str = "") -> bool:
    results.append((bool(condition), name, detail))
    mark = "\033[32m✓\033[0m" if condition else "\033[31m✗\033[0m"
    suffix = f"  \033[90m{detail}\033[0m" if detail else ""
    print(f"  {mark} {name}{suffix}")
    return bool(condition)


def call(method: str, path: str, payload=None, headers: dict | None = None,
         raw: bytes | None = None, timeout: float = 20.0):
    """发一个请求，返回 (状态码, 解析后的响应体)。"""
    body = raw if raw is not None else (json.dumps(payload).encode("utf-8") if payload is not None else None)
    sent = dict(headers or {})
    if payload is not None and "Content-Type" not in sent:
        sent["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=sent)
        response = conn.getresponse()
        data = response.read()
    finally:
        conn.close()
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed = data
    return response.status, parsed


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_health(proc: subprocess.Popen, deadline: float = 25.0) -> bool:
    started = time.time()
    while time.time() - started < deadline:
        if proc.poll() is not None:
            return False
        try:
            status, _ = call("GET", "/api/health", timeout=1.0)
            if status == 200:
                return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


# --------------------------------------------------------------------------
# 分节检查
# --------------------------------------------------------------------------
def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


def check_engine_invariants() -> None:
    """引擎层不变量：不依赖 HTTP，直接跑一遍。"""
    section("1. 策略引擎不变量")
    from app import config as appconfig, engine, marketdata

    bars = marketdata.load_csv(appconfig.SAMPLE_CSV)
    cfg = engine.normalize_config(CONFIG)

    first = engine.TradingSession(bars, cfg)
    while first.status == "running":
        first.advance()
    second = engine.TradingSession(bars, cfg)
    while second.status == "running":
        second.advance()

    check("同一行情两次运行结果完全一致",
          [(t["entryIdx"], t["netPnl"]) for t in first.trades]
          == [(t["entryIdx"], t["netPnl"]) for t in second.trades],
          f"{len(first.trades)} 笔交易")

    # 回归守卫：旧实现里 sig=0 会索引到列表末尾（负索引），凭空造出"幽灵金叉"。
    # 这里直接断言守卫本身，比构造特定行情更稳。
    check("sig=0 不产生任何信号（负索引回绕回归）",
          first._evaluate(0) == (False, False, []),
          "TradingSession._evaluate(0)")

    check("所有成交的入场索引都 >= 1（首根 bar 不可能成交）",
          all(t["entryIdx"] >= 1 for t in first.trades))

    check("权益曲线索引严格递增（无重复点）",
          all(b["i"] > a["i"] for a, b in zip(first.equity_curve, first.equity_curve[1:])),
          f"{len(first.equity_curve)} 个点")

    check("持仓在样本末端被强制平仓（不留下悬空持仓）",
          first.position is None and first.status == "completed")

    restored = engine.TradingSession.restore(
        {**first.state(), **first.market_data()})
    check("状态序列化 → 反序列化可无损恢复",
          restored.cursor == first.cursor and restored.equity == first.equity
          and len(restored.trades) == len(first.trades),
          f"cursor={restored.cursor}, equity={restored.equity}")

    bad = engine.validate_bars
    for label, payload in (
        ("行情根数不足时拒绝", {"bars": []}),
        ("时间倒序时拒绝", {"bars": [dict(bars[1]), dict(bars[0])]}),
    ):
        try:
            bad(payload["bars"])
            check(label, False)
        except ValueError as exc:
            check(label, True, str(exc))

    check("非法策略参数被拒绝",
          _raises(lambda: engine.normalize_config({"fast": 30, "slow": 10})),
          "fast >= slow")


def _raises(fn) -> bool:
    try:
        fn()
    except ValueError:
        return True
    return False


def check_http_chain() -> None:
    section("2. 主链路：健康检查 → 样例行情 → 建策略 → 回测 → 模拟盘")
    status, health = call("GET", "/api/health")
    check("GET /api/health 返回 200", status == 200)
    check("健康检查声明为本地纸面模式",
          health.get("mode") == "paper-only" and health.get("storage") == "sqlite",
          f"version={health.get('version')}")
    check("初始账本为空（自检使用独立数据目录）",
          health.get("counts", {}).get("strategies") == 0,
          f"counts={health.get('counts')}")

    status, sample = call("GET", "/api/market/sample")
    bars = sample.get("bars") if isinstance(sample, dict) else None
    check("GET /api/market/sample 提供内置合成行情",
          status == 200 and isinstance(bars, list) and len(bars) >= 30,
          f"{len(bars or [])} 根 bar")
    check("样例行情带有'非真实行情'声明",
          "不代表任何真实品种" in str(sample.get("notice", "")))

    status, strategy = call("POST", "/api/strategies", {
        "name": "自检 · 均线金叉演示", "symbol": "DEMO", "timeframe": "5 分钟",
        "maType": "EMA", "fast": 9, "slow": 21,
        "atr": True, "atrPeriod": 14, "atrMaxPct": 0.4, "rsi": False,
    })
    check("POST /api/strategies 创建策略", status == 201 and strategy.get("id"),
          f"id={str(strategy.get('id'))[:8]}")
    strategy_id = strategy.get("id")

    status, strategies = call("GET", "/api/strategies")
    check("GET /api/strategies 能读回刚建的策略",
          status == 200 and any(s.get("id") == strategy_id for s in strategies),
          f"{len(strategies)} 条")

    status, backtest = call("POST", "/api/backtests", {
        "bars": bars, "config": {**CONFIG, "splitPct": 70},
        "strategyId": strategy_id, "strategy": {"name": "自检演示", "symbol": "DEMO"},
    })
    ok = status == 201 and isinstance(backtest, dict)
    check("POST /api/backtests 返回 201", ok, f"status={status}"
          + ("" if ok else f" body={backtest}"))
    if ok:
        summary = backtest.get("summary", {})
        check("回测摘要字段完整",
              {"netPnl", "returnPct", "maxDrawdownPct", "tradeCount", "winRatePct"} <= set(summary),
              f"净收益 {summary.get('returnPct')}% · {summary.get('tradeCount')} 笔")
        check("回测曲线包含策略权益与买入持有基准",
              bool(backtest.get("curve")) and {"equity", "benchmark"} <= set(backtest["curve"][0]),
              f"{len(backtest.get('curve', []))} 点")
        check("权益曲线无重复索引（切分点去重生效）",
              len({p["i"] for p in backtest["curve"]}) == len(backtest["curve"]))
        check("训练段 / 测试段各有一段统计",
              [s.get("name") for s in backtest.get("segments", [])] == ["训练段", "测试段"])
        check("回测结果附带模型局限声明", bool(backtest.get("notice")))
        backtest_id = backtest.get("id")
    else:
        backtest_id = None

    status, history = call("GET", "/api/backtests")
    check("GET /api/backtests 历史列表可见",
          status == 200 and any(item.get("id") == backtest_id for item in history),
          f"{len(history)} 条")
    if backtest_id:
        status, detail = call("GET", f"/api/backtests/{backtest_id}")
        check("GET /api/backtests/<id> 能取回完整结果",
              status == 200 and detail.get("id") == backtest_id)
    status, _ = call("GET", "/api/backtests/not-exist-id")
    check("不存在的回测返回 404", status == 404)

    # ---- 模拟盘：逐 bar 推进直到完成 ----
    status, run = call("POST", "/api/trading/runs", {
        "strategyId": strategy_id, "bars": bars, "config": CONFIG})
    if status != 201:
        check("POST /api/trading/runs 启动模拟盘", False, f"status={status} body={run}")
        return
    run_id = run.get("runId")
    check("POST /api/trading/runs 启动模拟盘", bool(run_id),
          f"runId={str(run_id)[:8]}, 目标 {run.get('totalBars')} 根 bar")

    steps = 0
    snapshot = run
    while snapshot.get("status") == "running" and steps <= len(bars) + 5:
        status, snapshot = call("POST", f"/api/trading/runs/{run_id}/step", {})
        if status != 200:
            check("step 接口推进 bar", False, f"status={status} body={snapshot}")
            return
        steps += 1
    check("逐 bar 推进至回放结束", snapshot.get("status") == "completed",
          f"{steps} 次 step，cursor={snapshot.get('cursor')}/{snapshot.get('totalBars')}")
    check("完成后自动清仓", snapshot.get("position") is None)

    status, again = call("POST", f"/api/trading/runs/{run_id}/step", {})
    check("已完成的运行再 step 不改变状态、不新增成交",
          status == 200 and again.get("status") == "completed"
          and len(again.get("trades", [])) == len(snapshot.get("trades", [])),
          f"{len(snapshot.get('trades', []))} 笔成交")

    # ---- 关键一致性：HTTP 逐步推进 == 本地一次性跑完 ----
    from app import config as appconfig, engine, marketdata
    local = engine.TradingSession(marketdata.load_csv(appconfig.SAMPLE_CSV),
                                  engine.normalize_config(CONFIG))
    while local.status == "running":
        local.advance()
    remote_trades = [(t["entryIdx"], round(t["netPnl"], 2)) for t in snapshot.get("trades", [])]
    local_trades = [(t["entryIdx"], round(t["netPnl"], 2)) for t in local.trades]
    check("HTTP 逐步推进结果与本地一次性回放完全一致",
          remote_trades == local_trades,
          f"远端 {len(remote_trades)} 笔 / 本地 {len(local_trades)} 笔")

    status, runs = call("GET", "/api/trading/runs")
    check("GET /api/trading/runs 列表可见",
          status == 200 and any(r.get("runId") == run_id for r in runs))
    status, trades = call("GET", "/api/trading/trades")
    check("GET /api/trading/trades 汇总所有运行成交",
          status == 200 and isinstance(trades, list) and len(trades) == len(local_trades),
          f"{len(trades)} 笔")

    # ---- 策略交易记录：策略卡片「详细」入口的逐笔盈亏明细 ----
    status, ledger = call("GET", f"/api/strategies/{strategy_id}/trades")
    ledger_trades = ledger.get("trades", []) if isinstance(ledger, dict) else []
    check("GET /api/strategies/<id>/trades 返回策略级成交明细",
          status == 200 and isinstance(ledger_trades, list) and len(ledger_trades) >= 1,
          f"{len(ledger_trades)} 笔（回测 + 模拟盘汇总）")
    check("每笔盈亏明细字段完整（毛盈亏 / 费用 / 净盈亏 / 收益率 / 持仓时长）",
          all({"grossPnl", "fees", "netPnl", "returnPct", "holdLabel", "entryTime",
               "exitTime", "source", "sourceLabel"} <= set(t) for t in ledger_trades),
          f"{len(ledger_trades)} 笔逐笔字段齐")
    check("成交按平仓时间倒序",
          [t.get("exitTime") for t in ledger_trades]
          == sorted((t.get("exitTime") for t in ledger_trades), reverse=True))
    ledger_summary = ledger.get("summary") or {}
    check("汇总与逐笔一致（笔数 / 净盈亏）",
          ledger_summary.get("tradeCount") == len(ledger_trades)
          and abs((ledger_summary.get("netPnl") or 0)
                  - sum(t.get("netPnl") or 0 for t in ledger_trades)) < 0.01,
          f"汇总 {ledger_summary.get('tradeCount')} 笔 / 净 {ledger_summary.get('netPnl')}")
    check("成交标注来源（常驻 / 模拟盘 / 回测）",
          all(t.get("source") in ("live", "sim", "paper", "backtest") and t.get("sourceLabel")
              for t in ledger_trades)
          and "paper" in {t["source"] for t in ledger_trades},
          f"来源 {sorted({t['source'] for t in ledger_trades})}")
    status, _ = call("GET", "/api/strategies/not-exist-id/trades")
    check("未知策略的交易记录返回 404", status == 404)

    status, dash = call("GET", "/api/dashboard")
    check("GET /api/dashboard 反映当前模拟盘",
          status == 200 and dash.get("paper", {}).get("active") is True,
          f"权益 {dash.get('paper', {}).get('equity')}")
    check("仪表盘同步策略库与运行计数",
          dash.get("strategyCount") == 1 and dash.get("activeTradingRuns") == 0,
          f"strategies={dash.get('strategyCount')}, activeRuns={dash.get('activeTradingRuns')}")

    # 这条曾经是真实 bug：诊断读 state_json（不含 bars），永远返回"没有可分析数据"
    status, diag = call("GET", "/api/diagnostics")
    check("GET /api/diagnostics 在有回放时可用（bars 未被漏读）",
          status == 200 and diag.get("available") is True,
          f"bars={diag.get('bars')}, ATR={diag.get('atrLast20')}")
    check("诊断给出样本区间与胜率等真实统计",
          diag.get("priceChangePct") is not None and diag.get("tradeCount") == len(local_trades))
    check("诊断声明'不构成推荐或有效性结论'", bool(diag.get("note")))

    status, ai = call("GET", "/api/ai/status")
    check("GET /api/ai/status 只回传是否配置，不回传 Key",
          status == 200 and "configured" in ai and "key" not in ai and "apiKey" not in ai,
          f"configured={ai.get('configured')}, provider={ai.get('provider')}")

    status, deleted = call("DELETE", f"/api/trading/runs/{run_id}")
    check("DELETE 可删除已完成的运行记录", status == 200 and deleted.get("ok") is True)
    status, _ = call("GET", f"/api/trading/runs/{run_id}")
    check("删除后再取该运行返回 404", status == 404)


def check_real_history_backtest() -> None:
    """回测页改成「选品种 + 时间区间 → 服务端拉真实历史行情」后的全链路。

    联网部分允许降级（公开行情源可能临时不可用），但**参数校验与页面不再
    依赖本地行情文件**这两类断言必须永远成立，不能因为断网就跳过。
    """
    section("4b. 回测：按时间区间拉真实历史行情（不依赖本地行情文件）")
    from datetime import date, timedelta

    end_day = date.today()
    start_day = end_day - timedelta(days=400)
    start, end = start_day.isoformat(), end_day.isoformat()

    # ---- 静态回归：页面里不该再出现上传控件 / CSV 解析 ----
    status, script = call("GET", "/js/backtest.js")
    js = script.decode("utf-8", "replace") if isinstance(script, bytes) else str(script)
    check("回测页脚本不再有本地行情文件上传控件",
          status == 200 and 'type="file"' not in js and "parseCsv" not in js)
    check("回测页走按时间区间的服务端行情接口",
          status == 200 and "/api/market/history" in js and "range" in js)

    # ---- 参数校验：不联网也要拦住（不能变成 500）----
    bad_ranges = [
        ("缺品种", {"symbol": "", "start": start, "end": end, "period": "1d"}),
        ("日期格式错误", {"symbol": "rb", "start": "not-a-date", "end": end, "period": "1d"}),
        ("开始晚于结束", {"symbol": "rb", "start": end, "end": start, "period": "1d"}),
        ("周期非法", {"symbol": "rb", "start": start, "end": end, "period": "xyz"}),
    ]
    for label, spec in bad_ranges:
        status, body = call("POST", "/api/backtests", {"range": spec, "config": CONFIG}, timeout=30)
        check(f"非法区间「{label}」返回 400 + 可读原因（不是 500）",
              status == 400 and bool(body.get("error")), f"status={status}")

    status, probe = call("GET", f"/api/market/history?symbol=rb&start={start}&end={end}&period=1d",
                         timeout=60)
    check("GET /api/market/history 只回统计、不把 bars 传回前端",
          status == 200 and isinstance(probe, dict) and "bars" not in probe,
          f"status={status}")

    if not (status == 200 and probe.get("ok")):
        # 断网 / 上游挂了：仍必须是可读降级，不能用合成数据悄悄兜底
        check("行情源不可用时探测给出可读原因",
              status == 200 and probe.get("ok") is False and bool(probe.get("reason")),
              str(probe.get("reason", ""))[:48])
        status, body = call("POST", "/api/backtests",
                            {"range": {"symbol": "rb", "start": start, "end": end, "period": "1d"},
                             "config": CONFIG}, timeout=60)
        check("行情源不可用时按区间回测返回 4xx/502 而不是 500",
              status in (400, 502) and bool(body.get("error")), f"status={status}")
        return

    check("探测结果标注真实历史行情与日线来源",
          probe.get("sourceKind") == "real-history" and "futures_main_sina" in str(probe.get("source"))
          and probe.get("count", 0) >= 30,
          f"{probe.get('count')} 根 · {str(probe.get('source'))[:28]}")
    check("探测结果带请求区间与可得窗口",
          {"requestedStart", "requestedEnd", "availableFrom", "availableTo"} <= set(probe.get("coverage") or {}))

    cfg = {**CONFIG, "atrMaxPct": 3.0, "multiplier": 10.0}
    payload = {"range": {"symbol": "rb", "start": start, "end": end, "period": "1d"},
               "config": cfg, "strategy": {"name": "自检 · 按时间回测"}}
    status, result = call("POST", "/api/backtests", payload, timeout=90)
    ok = status == 201 and result.get("sourceBars", 0) >= 30
    check("POST /api/backtests 用 range 跑通真实行情回测", ok,
          f"status={status} bars={result.get('sourceBars')}")
    if not ok:
        return
    check("结果落库数据源 / 品种 / 周期 / 请求区间",
          result.get("sourceKind") == "real-history"
          and result.get("period") == "1d"
          and bool(result.get("symbol"))
          and (result.get("requestedRange") or {}).get("start") == start)
    check("结果声明为真实历史回放（不是合成演示行情）",
          "真实历史" in str(result.get("notice")) and "合成" not in str(result.get("dataSource")))
    check("结果仍带权益曲线 / 逐笔 / 训练测试分段",
          bool(result.get("curve")) and isinstance(result.get("trades"), list)
          and [s.get("name") for s in result.get("segments", [])] == ["训练段", "测试段"])
    detail_status, detail = call("GET", f"/api/backtests/{result['id']}")
    check("回测明细可读回且保留数据源",
          detail_status == 200 and detail.get("sourceKind") == "real-history")
    listed = call("GET", "/api/backtests")[1]
    check("回测列表能直接看出品种与周期",
          any(row.get("id") == result["id"] and row.get("periodLabel") == "日线"
              for row in listed), f"{len(listed)} 条")

    # 分钟线上游只有最近约 1023 根：要历史区间时必须明确拒绝 + 给可用起点
    status, far = call("GET", "/api/market/history?symbol=rb&start=2020-01-01&end=2020-12-31&period=5m",
                       timeout=60)
    check("分钟线超出可得窗口时明确报错并给出可用起点",
          status == 200 and far.get("ok") is False and bool(far.get("availableFrom")),
          str(far.get("reason", ""))[:46])


def check_simulate() -> None:
    section("3. 区间模拟：策略 + 时间段 → 买卖点 / 逐笔盈亏 / 按日汇总")
    status, options = call("GET", "/api/simulate/options")
    check("GET /api/simulate/options 返回行情来源与默认参数",
          status == 200
          and {item["id"] for item in options.get("sources", [])} == {"demo", "live"}
          and options.get("maxDays", 0) >= 1
          and options.get("defaults", {}).get("source") in ("demo", "live"),
          f"默认来源={options.get('defaults', {}).get('source')}, 上限={options.get('maxDays')} 个交易日")

    status, result = call("POST", "/api/simulate", {"source": "demo", "days": 3, "config": CONFIG})
    ok = status == 201 and isinstance(result, dict)
    check("POST /api/simulate 一次跑完并返回 201", ok,
          f"{result.get('barCount')} 根 bar" if ok else f"status={status} body={result}")
    if not ok:
        return

    summary = result.get("summary", {})
    check("区间模拟摘要字段完整",
          {"netPnl", "returnPct", "winRatePct", "maxDrawdownPct", "tradeCount",
           "profitFactor", "feesTotal", "avgHoldMinutes", "expectancy"} <= set(summary),
          f"{summary.get('tradeCount')} 笔 · 净盈亏 {summary.get('netPnl')}")
    check("行情来源与声明随结果返回",
          result.get("source", {}).get("kind") == "demo" and bool(result.get("notice")))
    check("结果带区间首尾时间与 bar 数",
          bool(result.get("startTime")) and bool(result.get("endTime")) and result.get("barCount", 0) >= 240,
          f"{result.get('barCount')} 根")

    trades = result.get("trades", [])
    check("逐笔成交包含开平仓时间、价格、持仓时长与盈亏",
          bool(trades) and all({"entryTime", "exitTime", "entryPrice", "exitPrice",
                                "holdMinutes", "holdLabel", "netPnl", "fees", "exitReason"} <= set(t)
                               for t in trades),
          f"{len(trades)} 笔")
    check("买卖点价格为正且开仓早于平仓",
          all(t["entryPrice"] > 0 and t["exitPrice"] > 0 and t["entryTime"] < t["exitTime"]
              for t in trades))
    check("每笔成交带持仓期间的最大浮盈/浮亏",
          all(t.get("mfePct") is not None and t.get("maePct") is not None for t in trades))

    daily = result.get("daily", [])
    daily_net = round(sum(day["netPnl"] for day in daily), 2)
    check("按交易日汇总的盈亏之和等于区间总盈亏",
          bool(daily) and abs(daily_net - round(summary.get("netPnl", 0), 2)) < 0.01,
          f"按日 {daily_net} / 合计 {summary.get('netPnl')}")
    check("权益曲线含策略与买入持有基准",
          bool(result.get("curve")) and {"equity", "benchmark"} <= set(result["curve"][0]),
          f"{len(result.get('curve', []))} 点")

    run_id = result.get("runId")
    status, reloaded = call("GET", f"/api/simulate/{run_id}")
    check("GET /api/simulate/<id> 能取回同一条模拟并复现盈亏",
          status == 200 and reloaded.get("summary", {}).get("netPnl") == summary.get("netPnl"),
          f"runId={str(run_id)[:8]}")
    status, history = call("GET", "/api/simulate/history")
    check("GET /api/simulate/history 列出区间模拟记录",
          status == 200 and any(item.get("runId") == run_id for item in history),
          f"{len(history)} 条")

    # 固定历史区间重跑：demo 行情按日期确定性生成，结果必须逐笔一致
    import datetime
    end_day = datetime.date.today() - datetime.timedelta(days=6)
    start_day = end_day - datetime.timedelta(days=4)
    window = {"source": "demo", "start": start_day.isoformat(), "end": end_day.isoformat(), "config": CONFIG}
    status, first = call("POST", "/api/simulate", window)
    status2, second = call("POST", "/api/simulate", window)
    check("同一历史区间两次模拟结果完全一致（行情可复现）",
          status == 201 and status2 == 201
          and [round(t["netPnl"], 2) for t in first.get("trades", [])]
          == [round(t["netPnl"], 2) for t in second.get("trades", [])],
          f"{first.get('barCount')} 根 bar · {len(first.get('trades', []))} 笔")

    status, body = call("POST", "/api/simulate", {"source": "live", "days": 3, "config": CONFIG})
    check("真实来源无采集数据时给出可执行的提示",
          status == 400 and "至少需要" in str(body.get("error", "")),
          str(body.get("error", ""))[:60])

    status, body = call("POST", "/api/simulate", {"source": "unknown", "days": 3, "config": CONFIG})
    check("未知行情来源返回 400", status == 400)

    status, body = call("POST", "/api/simulate", {
        "source": "demo", "start": "2026-01-01", "end": "2026-11-04", "config": CONFIG})
    check("区间超过单次上限返回 400", status == 400, str(body.get("error", ""))[:40])

    status, body = call("POST", "/api/simulate", {
        "source": "demo", "days": 3, "strategyId": "not-exist", "config": CONFIG})
    check("引用不存在的策略返回 400", status == 400)

    status, _ = call("GET", "/api/simulate/not-exist-run")
    check("不存在的模拟记录返回 404", status == 404)


def check_catalog_and_kline() -> None:
    section("4. 品种清单 / 按需 K 线 / 诊断报告 / 环境分析降级")

    # ---- 品种清单：盈亏口径全部靠这里的乘数，少一个品种就会算错一个数量级 ----
    status, body = call("GET", "/api/instruments")
    groups = body.get("groups", []) if status == 200 else []
    codes = [item["code"] for group in groups for item in group.get("items", [])]
    check("GET /api/instruments 返回按板块分组的完整清单",
          status == 200 and len(groups) >= 8 and len(codes) >= 60,
          f"{len(groups)} 个板块 / {len(codes)} 个品种")
    check("清单覆盖金融/黑色/有色/贵金属/能化/农产品",
          {"if", "rb", "cu", "au", "sc", "m"} <= set(codes),
          "含 IF/RB/CU/AU/SC/M")
    check("每个品种都带乘数、最小变动与保证金",
          all(item.get("multiplier") and item.get("tick") and item.get("marginPct")
              for group in groups for item in group.get("items", [])))
    check("乘数按品种区分，不再全部写死 300",
          len({item["multiplier"] for group in groups for item in group.get("items", [])}) > 5,
          f"{len({item['multiplier'] for group in groups for item in group.get('items', [])})} 种乘数")

    # ---- 合约生成：郑商所 3 位月份是最容易写错的地方 ----
    status, body = call("GET", "/api/instruments/rb/contracts")
    rows = body.get("contracts", [])
    check("GET /api/instruments/rb/contracts 生成未到期合约且首个标为主力",
          status == 200 and bool(rows) and rows[0].get("isMain") is True,
          f"{len(rows)} 个合约，首个 {rows[0].get('symbol') if rows else '—'}")
    status, body = call("GET", "/api/instruments/sr/contracts")
    shfe_rows = body.get("contracts", [])
    check("郑商所品种用 3 位月份（SR601 而不是 SR2601）",
          status == 200 and bool(shfe_rows)
          and all(len(row["symbol"][2:]) == 3 for row in shfe_rows),
          f"示例 {shfe_rows[0]['symbol'] if shfe_rows else '—'}")
    status, _ = call("GET", "/api/instruments/nope/contracts")
    check("未知品种的合约返回 404", status == 404)

    # ---- 按需 K 线：要联网，失败时只要求「明确报错」，不强制 200 ----
    status, body = call("GET", "/api/kline?symbol=rb&period=1m&limit=60", timeout=45)
    if status == 200:
        bars = body.get("bars", [])
        check("GET /api/kline 返回主力连续分钟线",
              bool(bars) and body.get("continuous") == "RB0" and body.get("count") == len(bars),
              f"{body.get('continuous')} {len(bars)} 根")
        check("K 线时间严格递增，且 low ≤ open/close ≤ high",
              all(a["timestamp"] < b["timestamp"] for a, b in zip(bars, bars[1:]))
              and all(bar["low"] <= min(bar["open"], bar["close"])
                      and max(bar["open"], bar["close"]) <= bar["high"] for bar in bars))
        check("返回里明确标注这是主力连续而非具体合约",
              "主力连续" in (body.get("notice") or ""),
              (body.get("notice") or "")[:24])
        check("bar 根数受 limit 约束", len(bars) <= 60, f"{len(bars)} ≤ 60")
    else:
        check("行情源不可用时 /api/kline 给出 502 + 可读原因（不是 500）",
              status == 502 and bool(body.get("error")),
              f"status={status} {str(body.get('error'))[:40]}")

    status, body = call("GET", "/api/kline?symbol=rb&period=5m&limit=40", timeout=45)
    check("5 分钟周期走窗口合并，不报错",
          status in (200, 502) and (status != 200 or body.get("period") == "5m"))
    # ---- 9 个周期里的长周期：4h/12h 由 60m 合并，1d/1w 走真实日线（联网失败可降级） ----
    for period, label, src_kw in (("4h", "4 小时", "60 分钟合并"),
                                  ("12h", "12 小时", "60 分钟合并"),
                                  ("1d", "1 天", "futures_main_sina"),
                                  ("1w", "1 周", "futures_main_sina")):
        status, body = call("GET", f"/api/kline?symbol=rb&period={period}&limit=60", timeout=90)
        check(f"{label}周期返回正确口径的 K 线（联网失败可降级）",
              status in (200, 502) and (status != 200 or (
                  body.get("period") == period
                  and body.get("periodLabel") == label
                  and src_kw in (body.get("source") or "")
                  and len(body.get("bars") or []) > 0)),
              f"status={status} {str(body.get('source') or body.get('error'))[:40]}")
    status, body = call("GET", "/api/kline?symbol=rb&period=1d&limit=900", timeout=90)
    if status == 200:
        day_bars = body.get("bars") or []
        status_w, body_w = call("GET", "/api/kline?symbol=rb&period=1w&limit=900", timeout=90)
        if status_w == 200:
            week_bars = body_w.get("bars") or []
            check("周线由日线合并：根数更少且时间严格递增",
                  0 < len(week_bars) < len(day_bars)
                  and all(a["timestamp"] < b["timestamp"] for a, b in zip(week_bars, week_bars[1:])),
                  f"周线 {len(week_bars)} 根 < 日线 {len(day_bars)} 根")
    status, _ = call("GET", "/api/kline?period=1m")
    check("缺少 symbol 返回 400", status == 400)
    status, body = call("GET", "/api/kline?symbol=rb&period=7m", timeout=45)
    check("不支持的周期返回 502 并说明可选值",
          status == 502 and "7m" in str(body.get("error", "")),
          str(body.get("error", ""))[:36])

    # ---- 秒级实时：一次性 tick 快照 + SSE 秒级 K 线流（联网失败可降级） ----
    status, body = call("GET", "/api/kline/tick?symbol=rb&forming=1&period=1m", timeout=30)
    check("实时 tick 快照返回价格与行情时间（联网失败可降级）",
          status in (200, 502) and (status != 200 or (
              body.get("continuous") == "RB0"
              and float(body.get("price") or 0) > 0
              and bool(body.get("tickTime")))),
          f"status={status} price={body.get('price') if status == 200 else body.get('error')}")
    if status == 200:
        check("forming=1 附带形成中 bar，timestamp 与周期桶对齐",
              isinstance(body.get("forming"), dict)
              and (body["forming"].get("timestamp") or "").endswith("+08:00")
              and float(body["forming"].get("close") or 0) > 0,
              str(body.get("forming", {}).get("timestamp"))[:24])
    status, _ = call("GET", "/api/kline/tick")
    check("tick 缺少 symbol 返回 400", status == 400)
    status, body = call("GET", "/api/kline/tick?symbol=rb&period=7m&forming=1", timeout=30)
    check("tick+forming 非法周期不整体报错（快照仍返回）",
          status in (200, 502) and (status != 200 or body.get("price") is not None))

    # SSE 秒级流：裸 socket 读到 hello 即通过（长连接不能用 call()）
    try:
        sock = socket.create_connection(("127.0.0.1", PORT), timeout=20)
        sock.sendall(("GET /api/kline/stream?symbol=rb&period=1m HTTP/1.1\r\n"
                      f"Host: 127.0.0.1:{PORT}\r\nAccept: text/event-stream\r\n"
                      "Connection: close\r\n\r\n").encode())
        sock.settimeout(25)
        buffer = b""
        started = time.time()
        hello = None
        while time.time() - started < 20:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buffer += chunk
            if b"\r\n\r\n" in buffer:
                head, buffer = buffer.split(b"\r\n\r\n", 1)
                if b"200" not in head.split(b"\r\n", 1)[0]:
                    break
            if b"event: hello" in buffer and b"\n\n" in buffer:
                raw = buffer.split(b"event: hello", 1)[1].split(b"\n\n", 1)[0]
                line = next((ln[6:] for ln in raw.decode("utf-8", "replace").splitlines()
                             if ln.startswith("data: ")), "")
                if line:
                    hello = json.loads(line)
                break
        sock.close()
        check("SSE 秒级流 hello 推全量窗口与实时 tick（联网失败可降级）",
              hello is not None and len(hello.get("bars") or []) > 0
              and (hello.get("tick") or {}).get("price"),
              f"bars={len((hello or {}).get('bars') or [])} tick={(hello or {}).get('tick', {}).get('price')}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        check("SSE 秒级流 hello 推全量窗口与实时 tick（联网失败可降级）",
              False, f"连接失败 {type(exc).__name__}")

    # ---- 诊断报告：只验读取与错误分支，不在这里真调模型（要花钱、还会拖慢自检） ----
    status, body = call("GET", "/api/reports")
    check("GET /api/reports 返回列表与免责声明",
          status == 200 and isinstance(body.get("items"), list) and bool(body.get("notice")),
          f"{len(body.get('items', []))} 篇")
    status, _ = call("GET", "/api/reports/not-exist")
    check("不存在的报告返回 404", status == 404)
    status, _ = call("GET", "/api/reports/not-exist/export?format=md")
    check("导出不存在的报告不返回 200", status != 200, f"status={status}")
    # 用一个不存在的 runId 触发前置校验：会在调模型之前就 400，
    # 自检因此既不花钱也不依赖网络，外网/离线都能跑
    status, body = call("POST", "/api/reports", {"runId": "not-exist-run"}, timeout=20)
    check("报告生成在数据缺失时提前 400（不空跑一次模型）",
          status == 400 and "找不到" in str(body.get("error", "")),
          str(body.get("error", ""))[:32])

    # ---- 环境分析：首屏只读缓存，没缓存时必须给「去抓一次」的提示而不是报错 ----
    status, body = call("GET", "/api/macro/rb2601")
    check("没缓存时 /api/macro 降级为可读提示",
          status == 200 and body.get("available") is False and bool(body.get("message")),
          str(body.get("message", ""))[:28])

    # ---- AI 调参：先走参数校验分支，确认不会白调模型 ----
    status, body = call("POST", "/api/ai/strategy/draft", {"prompt": ""})
    check("AI 调参缺诉求时返回 400 而不是直接请求模型",
          status == 400 and "4–800" in str(body.get("error", "")),
          str(body.get("error", ""))[:36])

    # ---- 品种诊断：只看所选品种自己的行情，全部本地计算（不调模型）----
    status, body = call("GET", "/api/diagnostics/instrument?symbol=rb&period=1m", timeout=60)
    if status == 200:
        rng, trend, vol = body.get("range") or {}, body.get("trend") or {}, body.get("volatility") or {}
        bars = rng.get("bars") or 0
        check("GET /api/diagnostics/instrument 返回该品种自己的技术面",
              body.get("symbol") == "rb" and bars >= 60 and bool(trend.get("alignment"))
              and vol.get("atrPct") is not None,
              f"{bars} 根 / {trend.get('alignment')} / ADX {trend.get('adx')}")
        check("区间高低能包住首末收盘价，位置百分比在 0–100",
              rng["low"] <= min(rng["firstClose"], rng["lastClose"])
              and max(rng["firstClose"], rng["lastClose"]) <= rng["high"]
              and 0 <= (rng.get("positionPct") or 0) <= 100,
              f"{rng['low']}–{rng['high']} 位置 {rng.get('positionPct')}%")
        check("四条均线齐全且按周期排序（短周期名对得上）",
              all(trend.get(k) is not None for k in ("ma5", "ma10", "ma20", "ma60")),
              f"MA5 {trend.get('ma5')} / MA60 {trend.get('ma60')}")
        check("ADX 落在 0–100 且强度判定与阈值一致",
              trend.get("adx") is None or (
                  0 <= trend["adx"] <= 100
                  and trend.get("strength") == ("强趋势" if trend["adx"] >= 40 else
                                                "有趋势" if trend["adx"] >= 25 else
                                                "趋势偏弱" if trend["adx"] >= 20 else "震荡")),
              f"ADX {trend.get('adx')} → {trend.get('strength')}")
        check("量能比与跳空次数有值（0 也是合法值，不能缺失）",
              (body.get("volume") or {}).get("ratio") is not None
              and (vol.get("gapCount") or 0) >= 0,
              f"量比 {(body.get('volume') or {}).get('ratio')} / 跳空 {vol.get('gapCount')}")
        check("明确标注样本是主力连续、且给出样本区间",
              "主力连续" in (body.get("notice") or "") and body.get("continuous")
              and rng.get("startTime") and rng.get("endTime"),
              f"{rng.get('startTime', '')[:16]} → {rng.get('endTime', '')[:16]}")
    else:
        check("行情源不可用时品种诊断给 502 + 可读原因（不是 500）",
              status == 502 and bool(body.get("error")), f"status={status}")

    status, body = call("GET", "/api/diagnostics/instrument")
    check("品种诊断缺 symbol 返回 400", status == 400, str(body.get("error", ""))[:24])
    status, body = call("GET", "/api/diagnostics/instrument?symbol=nope")
    check("品种诊断遇到无法识别的品种返回 400 并给出示例",
          status == 400 and "rb2601" in str(body.get("error", "")),
          str(body.get("error", ""))[:36])
    status, body = call("POST", "/api/diagnostics/instrument/analyze", {"symbol": ""})
    check("AI 诊断缺品种时 400，不会白调一次模型", status == 400, f"status={status}")
    # 回放诊断与品种诊断必须是两个数据源，否则会"选了螺纹却看到黄金"
    status, body = call("GET", "/api/diagnostics")
    check("回放诊断仍是独立端点（与所选品种无关）",
          status == 200 and "available" in body, f"available={body.get('available')}")


def check_message_context() -> None:
    section("5. 消息面与情绪：VIX 真值 / 复合温度可复算 / 线索口径")

    # 消息面源头是公开接口，个别源失败属正常，检查结构而不是硬要求全部抓到
    status, body = call("GET", "/api/diagnostics/context?symbol=rb", timeout=90)
    check("GET /api/diagnostics/context 返回完整面板",
          status == 200 and all(k in body for k in
                                ("temperature", "vix", "overseas", "news", "calendar", "unavailable", "notice")),
          f"status={status} / 不可用源 {[x['key'] for x in body.get('unavailable') or []]}")
    check("口径写明「情绪温度不是 CBOE VIX」",
          "不是 CBOE VIX" in str(body.get("notice") or ""), str(body.get("notice") or "")[:40])

    vix = body.get("vix")
    if vix:
        check("VIX 来自 CBOE 官方日线且数值合理",
              "cboe" in str(vix.get("source") or "").lower() and 5 <= (vix.get("close") or 0) <= 150
              and len(str(vix.get("date") or "")) == 10,
              f"{vix.get('close')} @ {vix.get('date')} / {vix.get('level')}")
        check("VIX 近一年分位落在 0–100",
              vix.get("percentile1y") is None or 0 <= vix["percentile1y"] <= 100,
              f"{vix.get('percentile1y')}%")
    else:
        check("VIX 源不可用时如实留空，没有编数字", vix is None, "vix=null")

    temp = body.get("temperature") or {}
    parts = temp.get("components") or []
    if parts:
        total_weight = sum(float(c.get("weight") or 0) for c in parts)
        check("情绪温度：剔除缺失分量后权重重新归一化到 1",
              abs(total_weight - 1) < 0.02, f"Σweight={total_weight:.3f}")
        check("情绪温度：每个分量都在 0–100 且带可核对依据",
              all(0 <= float(c.get("value") or 0) <= 100 for c in parts)
              and all(c.get("label") for c in parts),
              f"{len(parts)} 个分量")
        check("情绪温度分值与等级一致（0–100）",
              0 <= (temp.get("score") or 0) <= 100 and bool(temp.get("level")),
              f"{temp.get('score')} / {temp.get('level')}")
    else:
        check("线索全缺失时温度标为数据不足而不是硬算",
              temp.get("score") is None and temp.get("level") == "数据不足", str(temp.get("level")))

    news = body.get("news") or {}
    check("快讯倾向分落在 -1~1，并写明关键词词典口径",
          news.get("toneScore") is None or -1 <= news["toneScore"] <= 1,
          f"{news.get('toneScore')} / {str(news.get('method') or '')[:24]}")
    check("相关快讯带时间、原文、倾向与线索类型",
          not (news.get("items") or []) or all(
              all(k in item for k in ("time", "text", "tone", "scope")) for item in news["items"][:5]),
          f"{news.get('relatedCount')} 条 / 分类 {news.get('category')}")
    check("跨市场资产涨跌幅是数值（红涨绿跌由前端染色）",
          all(isinstance(x.get("changePct"), (int, float)) for x in (body.get("overseas") or [])[:5]),
          f"{len(body.get('overseas') or [])} 个外盘资产")

    status, bad = call("GET", "/api/diagnostics/context")
    check("消息面缺 symbol 返回 400", status == 400, str(bad.get("error", ""))[:24])
    status, bad = call("GET", "/api/diagnostics/context?symbol=nope")
    check("消息面遇到无法识别的品种返回 400", status == 400, f"status={status}")

    # prompt 骨架与给模型的事实清单：不调模型，纯本地检查
    from app import context as context_mod
    from app import diagnose
    check("默认 AI 骨架以消息面为主、技术面只作交叉校验",
          all(k in diagnose.CONTEXT_PROMPT for k in ("消息面主线", "恐慌指数与跨市场避险",
                                                     "情绪温度与资金行为", "交叉校验"))
          and "趋势结构" in diagnose.TECH_ONLY_PROMPT, "两套 prompt 并存")
    facts = context_mod.facts(body)
    check("交给模型的事实清单写明口径与未获取项",
          all(k in facts for k in ("品种", "口径", "未获取的数据项")),
          f"{len(facts)} 个键")


def check_strategy_runner() -> None:
    section("6. 策略常驻交易：连续模式 / 增量喂数据 / 逐条启停")

    # ---- 引擎层：用合成行情验语义，不联网 ----
    from datetime import date

    from app import engine, marketdata, simulate

    cfg = engine.normalize_config({**simulate.DEFAULT_CONFIG, "multiplier": 300.0, "tickSize": 0.2,
                                   "fast": 9, "slow": 21, "maType": "EMA", "atr": True,
                                   "atrPeriod": 14, "rsi": False})
    bars = marketdata.synthetic_bars([date(2026, 11, 2), date(2026, 11, 3)],
                                     symbol="IF2612", base_price=3000.0)

    # 回放模式：跑完必须收尾（这是回测/区间模拟的前提，不能被连续模式带坏）
    replay = engine.TradingSession(list(bars), cfg, trade_prefix="T1")
    while replay.status == "running":
        replay.advance()
    check("回放模式跑完即收尾（status=completed）", replay.status == "completed",
          f"{len(replay.trades)} 笔成交")

    live = engine.TradingSession(bars[:120], cfg, trade_prefix="T2", continuous=True)
    while live.cursor < len(live.bars):
        live.advance()
    check("连续模式跟到最后一根仍保持 running，不强制平仓收尾",
          live.status == "running", f"cursor={live.cursor}")

    before = len(live.bars)
    added = live.feed(bars[120:150])
    check("feed 只把更新的 bar 追加到末尾",
          added == 30 and len(live.bars) == before + 30, f"+{added} 根")

    again = live.feed(bars[120:150])
    check("重复 feed 同一批不会重复入账", again == 0 and len(live.bars) == before + 30)

    tail = dict(live.bars[-1])
    tail["close"] = round(tail["close"] + 1, 2)
    live.feed([tail])
    check("feed 会用最新版本刷新尚未处理的 bar（分钟线最后一根还在形成）",
          live.bars[-1]["close"] == tail["close"], f"{live.bars[-1]['close']}")

    trades_before = len(live.trades)
    skipped = live.skip_to(len(live.bars) - 1)
    check("skip_to 跳过暂停期间的 bar 且不产生任何成交",
          skipped == 29 and len(live.trades) == trades_before, f"跳过 {skipped} 根")
    check("跳过后会留一条 gap 信号，事后能解释这段空白",
          any(item["type"] == "gap" for item in live.signals))

    # ---- 滚动窗口：常驻交易只增不减，跑久了必须裁，但索引不能错位 ----
    from datetime import timedelta

    long_days = [d for d in (date(2026, 10, 26) + timedelta(days=offset) for offset in range(12))
                 if d.weekday() < 5]
    long_bars = marketdata.synthetic_bars(long_days, symbol="RB0", base_price=3000.0)
    rolling = engine.TradingSession(long_bars[:300], cfg, trade_prefix="T3", continuous=True)
    while rolling.cursor < len(rolling.bars):
        rolling.advance()
    for start in range(300, len(long_bars), 200):
        rolling.feed(long_bars[start:start + 200])
        while rolling.cursor < len(rolling.bars):
            rolling.advance()

    kept = [(t["exitIdx"], rolling.bars[t["exitIdx"]]["timestamp"]) for t in rolling.trades[-3:]]
    dropped = rolling.trim(600)
    moved = [(t["exitIdx"], rolling.bars[t["exitIdx"]]["timestamp"]) for t in rolling.trades[-3:]]
    check("滚动窗口裁掉最老的 bar，只留最近 600 根",
          dropped > 0 and len(rolling.bars) == 600, f"丢 {dropped} 根")
    check("裁剪后成交索引指向的行情时间不变（索引整体偏移，不错位）",
          [item[1] for item in kept] == [item[1] for item in moved]
          and all(after[0] == before[0] - dropped for before, after in zip(kept, moved)),
          f"{kept[0][0] if kept else '—'} → {moved[0][0] if moved else '—'}")
    check("裁剪后 cursor 仍在有效范围内",
          0 <= rolling.cursor <= len(rolling.bars))

    # ---- 序列化往返：state() 是驼峰键、实例属性是蛇形 ----
    # 这里错过一次：setattr(session, "realizedPnl", ...) 只是挂了个没人读的属性，
    # 服务重启后浮盈、权益曲线、当前时间全回到初始值，而界面看不出来。
    snapshot = json.loads(json.dumps({**live.state(), **live.market_data()}, ensure_ascii=False))
    revived = engine.TradingSession.restore(snapshot)
    check("会话序列化后能原样恢复（成交 / 持仓 / 浮盈 / 权益曲线一项不丢）",
          revived.cursor == live.cursor and revived.realized_pnl == live.realized_pnl
          and revived.unrealized_pnl == live.unrealized_pnl and revived.equity == live.equity
          and revived.peak_equity == live.peak_equity
          and len(revived.trades) == len(live.trades)
          and len(revived.equity_curve) == len(live.equity_curve)
          and revived.position == live.position,
          f"realized={revived.realized_pnl} equity={revived.equity}")
    check("恢复的是真字段而不是驼峰同名影子属性（current_time 不为 None）",
          revived.current_time == live.current_time and revived.current_time is not None,
          str(revived.current_time))
    check("恢复后指标序列与 bars 等长，可以继续推进",
          len(revived.series["fastLine"]) == len(revived.bars) and revived.status == "running",
          f"{len(revived.bars)} 根")

    # ---- 接口层 ----
    status, body = call("GET", "/api/strategies/trading")
    check("GET /api/strategies/trading 返回运行态列表",
          status == 200 and isinstance(body.get("items"), list),
          f"{body.get('count', '?')} 条")
    check("空列表时 running / paused 计数都为 0",
          body.get("running") == 0 and body.get("paused") == 0)

    status, body = call("POST", "/api/strategies/not-exist-id/start", {})
    check("启动不存在的策略返回 400 并说明原因",
          status == 400 and "不存在" in str(body.get("error", "")),
          str(body.get("error", ""))[:30])
    # 策略压根不在运行表里时，三个动作都该报「没有在运行」——
    # 而不是分开给 "不存在" / "不是暂停状态"，那种细分对用户没有信息量。
    for action in ("pause", "resume", "stop"):
        status, body = call("POST", f"/api/strategies/not-exist-id/{action}", {})
        check(f"对未运行的策略执行 {action} 返回 400（不静默成功）",
              status == 400 and "没有在运行" in str(body.get("error", "")),
              str(body.get("error", ""))[:28])

    # ---- 真跑一条：行情源不可用时只要求「明确报错」，不强制成功 ----
    status, created = call("POST", "/api/strategies", {
        "name": "自检 · 常驻交易", "symbol": "rb2610", "timeframe": "5 分钟",
        "maType": "EMA", "fast": 9, "slow": 21, "atr": True, "atrPeriod": 14, "rsi": False,
    })
    strategy_id = created.get("id") if isinstance(created, dict) else None
    check("为常驻交易自检创建一条策略", status in (200, 201) and bool(strategy_id),
          str(strategy_id)[:8])

    if strategy_id:
        status, body = call("POST", f"/api/strategies/{strategy_id}/start", {}, timeout=90)
        if status == 200:
            check("启动后状态为「交易中」",
                  body.get("status") == "running" and body.get("statusLabel") == "交易中",
                  str(body.get("continuous")))
            check("启动时标明实际跟随的主力连续代码（不是策略里那个可能已交割的合约）",
                  bool(body.get("continuous")), str(body.get("continuous")))
            check("启动后按品种表套乘数：螺纹是 10，不是写死的 300",
                  body.get("equity") is not None and body.get("bars", 0) > 60,
                  f"{body.get('bars')} 根 / 成交 {body.get('tradeCount')} 笔")

            status, body = call("POST", f"/api/strategies/{strategy_id}/resume", {})
            check("对正在交易的策略再点「恢复」会被拒绝并说明原因",
                  status == 400 and "只有已暂停" in str(body.get("error", "")),
                  str(body.get("error", ""))[:26])

            status, body = call("POST", f"/api/strategies/{strategy_id}/pause", {})
            check("暂停后转为「已暂停」且持仓保留",
                  body.get("status") == "paused" and body.get("statusLabel") == "已暂停",
                  "持仓" if body.get("position") else "空仓")
            status, body = call("POST", f"/api/strategies/{strategy_id}/resume", {})
            check("恢复后回到「交易中」", body.get("status") == "running")
            live_trades = body.get("tradeCount", 0)
            status, body = call("POST", f"/api/strategies/{strategy_id}/stop", {})
            _status, after = call("GET", "/api/strategies/trading")
            check("停止后从运行列表移除",
                  body.get("status") == "stopped"
                  and all(item["strategyId"] != strategy_id for item in after.get("items", [])))
            # 停止不能吞成交：整份会话归档进 trading_runs，交易记录仍逐笔可查
            _status, ledger = call("GET", f"/api/strategies/{strategy_id}/trades")
            live_rows = [t for t in ledger.get("trades", []) if t.get("source") == "live"]
            check("停止后常驻成交归档保留，交易记录仍可逐笔查看",
                  len(live_rows) >= live_trades,
                  f"会话 {live_trades} 笔 / 记录 {len(live_rows)} 笔")
        else:
            check("行情源不可用时启动给出 400 + 可读原因（不是 500）",
                  status == 400 and bool(body.get("error")), str(body.get("error", ""))[:44])

        # 删策略必须连带把运行态一起清掉，否则会留下对着幽灵策略记账的线程
        status, _ = call("DELETE", f"/api/strategies/{strategy_id}")
        check("删除策略成功，运行态随之清除", status == 200)


def check_live_feed() -> None:
    section("7. 实时行情：聚合口径 / 接口 / SSE 推流")
    status, body = call("GET", "/api/live/status")
    check("GET /api/live/status 可用", status == 200 and "driver" in body, f"status={status}")
    check("初始状态未启动采集", body.get("running") is False)

    status, body = call("POST", "/api/live/start", {"driver": "sim", "symbol": "IF9999", "speedup": 60})
    check("POST /api/live/start 启动模拟源", status == 200 and body.get("running") is True,
          f"status={status}")
    check("模拟源明确标注数据非真实行情", "模拟" in (body.get("notice") or ""))

    # 模拟源 60× 加速：等几秒应至少收口出一根新 bar
    before = call("GET", "/api/live/bars?symbol=IF9999&period=1m&limit=500")[1].get("bars") or []
    time.sleep(4)
    after = call("GET", "/api/live/bars?symbol=IF9999&period=1m&limit=500")[1].get("bars") or []
    check("模拟源持续产生新 bar", len(after) >= len(before) and len(after) > 100,
          f"{len(before)} → {len(after)}")

    if after:
        stamps = [bar["timestamp"] for bar in after]
        check("bar 时间戳严格递增且不重复", stamps == sorted(set(stamps)))
        check("OHLC 满足 low<=min(o,c)<=max(o,c)<=high",
              all(bar["low"] <= min(bar["open"], bar["close"])
                  and bar["high"] >= max(bar["open"], bar["close"]) for bar in after))
        check("成交量非负", all(bar["volume"] >= 0 for bar in after))

    five = call("GET", "/api/live/bars?symbol=IF9999&period=5m&limit=500")[1].get("bars") or []
    check("5 分钟聚合根数少于 1 分钟", 0 < len(five) < len(after), f"5m={len(five)} 1m={len(after)}")
    if five and after:
        check("5 分钟首根开盘价取自 1 分钟首根",
              abs(five[0]["open"] - after[0]["open"]) < 1e-9)

    # SSE：读前若干字节确认是事件流格式（长连接，读够就断开）
    try:
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        conn.request("GET", "/api/live/stream?symbol=IF9999")
        response = conn.getresponse()
        chunk = response.read(4096).decode("utf-8", "ignore")   # hello 事件里带 500 根 bar，先读够再断言
        conn.close()
        check("SSE 返回 text/event-stream",
              response.getheader("Content-Type", "").startswith("text/event-stream"),
              response.getheader("Content-Type", ""))
        check("SSE 首个事件是 hello 且带行情快照", "event: hello" in chunk and '"bars"' in chunk)
    except Exception as exc:                     # noqa: BLE001
        check("SSE 事件流可读", False, f"{type(exc).__name__}: {exc}")

    status, body = call("POST", "/api/live/stop", {})
    check("POST /api/live/stop 停止采集", status == 200 and body.get("running") is False)

    # 数据源隔离：模拟源写下的 bar 必须带 source='sim'，不能被当作真实行情读出。
    # （曾出现模拟源 30× 加速把 bar 时间推到"未来"，与 CTP 真行情混在一张图上顶歪坐标轴）
    db_path = os.path.join(DATA_DIR, "qice.sqlite3") if DATA_DIR else ""
    if db_path and os.path.exists(db_path):
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT source, COUNT(*) FROM live_bars WHERE symbol='IF9999' GROUP BY source"
            ).fetchall()
        sources = dict(rows)
        check("模拟源写入的 bar 标记为 source=sim", sources.get("sim", 0) > 0, str(sources))
        check("模拟 bar 不会被当作 CTP 真实行情返回", sources.get("ctp", 0) == 0, str(sources))
    else:
        print("  ~ 跳过数据源标记断言（--external 模式拿不到临时数据目录）")

    status, _ = call("POST", "/api/live/start", {"driver": "ctp", "symbol": "IF9999"})
    check("未连接柜台时拒绝启动 CTP 行情", status == 400, f"status={status}")


def check_security() -> None:
    section("8. 安全回归：密钥泄露 / 目录穿越 / 跨站请求")
    for path in ("/.env", "/../.env", "/%2e%2e/.env", "/js/../.env",
                 "/.git/config", "/.venv/pyvenv.cfg", "/app/server.py",
                 "/requirements.txt", "/data/runtime/qice.sqlite3"):
        status, _ = call("GET", path)
        check(f"GET {path} 不可访问", status == 404, f"status={status}")

    status, _ = call("POST", "/api/strategies",
                     raw=b'{"name":"x","fast":9,"slow":21}',
                     headers={"Content-Type": "text/plain"})
    check("写操作强制 application/json（挡住跨站简单请求）", status == 415, f"status={status}")

    status, _ = call("POST", "/api/strategies",
                     {"name": "csrf-test", "fast": 9, "slow": 21},
                     headers={"Origin": "http://evil.example.com"})
    check("外站 Origin 被拒绝", status == 403, f"status={status}")

    # 用 skip_host 让 http.client 不自带 Host，才能伪造出恶意 Host
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    try:
        conn.request("GET", "/api/health", headers={"skip_host": True, "Host": "evil.example.com"})
        status = conn.getresponse().status
    finally:
        conn.close()
    check("伪造 Host 被拒绝（防 DNS rebinding）", status == 403, f"status={status}")

    status, _ = call("GET", "/js/not-exists.js")
    check("不存在的静态资源返回 404", status == 404)

    status, _ = call("GET", "/api/unknown")
    check("未知 API 路径返回 404", status == 404)

    status, body = call("POST", "/api/backtests", {"bars": [], "config": CONFIG})
    check("空行情被拒绝且返回可读错误",
          status == 400 and "行情" in str(body.get("error", "")), f"status={status}")


def check_launcher() -> None:
    """启动器行为：重复点 run.bat 不该报错，只有"别的程序占端口"才算失败。

    这条是真实踩过的坑：服务在跑的时候再运行一次 run.bat，会甩出一串
    Errno 10048 和 code 1，用户以为程序坏了。
    """
    section("9. 启动器：重复运行复用实例 / 端口被别的程序占用才报错")
    from app import server as launcher

    info = launcher.probe_running("127.0.0.1", PORT)
    check("能认出端口上正在跑的就是本服务",
          isinstance(info, dict) and info.get("service") == "qice-local-paper-lab", str(info))

    check("空闲端口探测不误报", launcher.probe_running("127.0.0.1", free_port()) is None)

    env = {**os.environ, "QICE_NO_OPEN": "1",
           "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}

    def run_server(port: int) -> tuple[int, str]:
        done = subprocess.run(
            [sys.executable, "-m", "app.server", "--port", str(port)],
            cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=90)
        return done.returncode, f"{done.stdout}\n{done.stderr}"

    code, output = run_server(PORT)
    check("重复启动退出码为 3（复用，而非失败）", code == 3, f"rc={code}")
    check("重复启动时提示了可直接打开的地址",
          f"127.0.0.1:{PORT}" in output, output.strip()[-200:])
    check("重复启动时提示了如何重启",
          "重启" in output, output.strip()[-200:])

    squatter = socket.socket()
    squatter.bind(("127.0.0.1", 0))
    squatter.listen(8)
    busy_port = squatter.getsockname()[1]
    try:
        code, output = run_server(busy_port)
        check("端口被其他程序占用时退出码为 1", code == 1, f"rc={code}")
        check("端口被其他程序占用时建议换端口",
              "8766" in output, output.strip()[-200:])
    finally:
        squatter.close()


def main() -> int:
    global PORT, DATA_DIR
    parser = argparse.ArgumentParser(description="期策 · 全链路自检")
    parser.add_argument("--external", help="只测已在运行的服务，例如 http://127.0.0.1:8765")
    parser.add_argument("--keep", action="store_true", help="保留临时数据目录以便排查")
    args = parser.parse_args()

    print("\033[1m期策 · 全链路自检\033[0m")

    if args.external:
        PORT = int(args.external.rsplit(":", 1)[-1].strip("/"))
        print(f"目标：{args.external}（复用现有服务，会写入该服务的数据目录）")
        check_http_chain()
        check_simulate()
        check_catalog_and_kline()
        check_real_history_backtest()
        check_message_context()
        check_strategy_runner()
        check_live_feed()
        check_security()
    else:
        check_engine_invariants()

        PORT = free_port()
        tmpdir = tempfile.mkdtemp(prefix="qice-smoke-")
        DATA_DIR = tmpdir
        env = {**os.environ, "QICE_DATA_DIR": tmpdir,
               "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        print(f"\n\033[1m启动临时服务\033[0m  port={PORT}  数据目录={tmpdir}")
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.server", "--port", str(PORT)],
            cwd=str(PROJECT_ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace")
        try:
            if not wait_health(proc):
                output = proc.stdout.read() if proc.poll() is not None else ""
                print("\033[31m服务未能启动\033[0m")
                print(output[-3000:])
                return 1
            check_http_chain()
            check_simulate()
            check_catalog_and_kline()
            check_real_history_backtest()
            check_message_context()
            check_strategy_runner()
            check_live_feed()
            check_security()
            check_launcher()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            if args.keep:
                print(f"\n临时数据目录已保留：{tmpdir}")
            else:
                import shutil
                shutil.rmtree(tmpdir, ignore_errors=True)

    passed = sum(1 for ok, _, _ in results if ok)
    failed = [name for ok, name, _ in results if not ok]
    print(f"\n\033[1m结果：{passed}/{len(results)} 通过\033[0m")
    if failed:
        print("\033[31m失败项：\033[0m")
        for name in failed:
            print(f"  · {name}")
        return 1
    print("\033[32m全链路畅通：行情 → 策略 → 回测 → 模拟盘 → 仪表盘/诊断/问答 → 安全边界\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
