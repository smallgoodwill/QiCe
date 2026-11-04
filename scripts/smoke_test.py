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


def check_live_feed() -> None:
    section("3. 实时行情：聚合口径 / 接口 / SSE 推流")
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
    section("4. 安全回归：密钥泄露 / 目录穿越 / 跨站请求")
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
            check_live_feed()
            check_security()
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
