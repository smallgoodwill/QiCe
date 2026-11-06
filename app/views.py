"""聚合视图：仪表盘与诊断。

抽出来的理由：这些是"读多表拼视图"的展示逻辑，和路由/SQL 都不该混在一起。
所有数值都来自数据库里的真实回放记录，没有数据就返回空状态，不做推测。
"""
from __future__ import annotations

from . import db, runner, simulate, trading

DIAGNOSTIC_NOTE = "诊断仅汇总当前回放样本，不构成品种推荐或策略有效性结论。"
TRADE_NOTICE = ("汇总常驻纸上交易、区间模拟与回测的全部成交；"
                "为模拟撮合结果（不含保证金、涨跌停与真实撮合规则），不代表真实成交与未来表现。")

# trading_runs 里的成交按 tradePrefix 认领来源。常驻交易停机后把整份会话
# 归档成一条 trading_run（见 runner.stop），前缀仍是 STRAT，靠这里认回来。
RUN_SOURCE = {
    "STRAT": ("live", "常驻交易"),
    "SIM": ("sim", "区间模拟"),
    "PAPER": ("paper", "模拟盘"),
}


def dashboard() -> dict:
    states = db.list_trading_run_states()
    active = next((item for item in states if trading.is_active(item[0])), None)
    selected = active or (states[0] if states else None)

    if selected:
        state, total = selected
        paper = trading.snapshot(state, total)
        current_day = str(state.get("currentTime") or "")[:10]
    else:
        paper = {"active": False, "status": "idle", "statusLabel": "尚无模拟盘"}
        current_day = ""

    all_trades = db.list_trading_run_trades()
    recent = sorted(all_trades, key=lambda t: t.get("exitTime", ""), reverse=True)[:8]
    daily_pnl = sum(t.get("netPnl", 0) for t in all_trades
                    if current_day and str(t.get("exitTime", "")).startswith(current_day))
    backtests = db.list_backtests(1)

    return {
        "strategyCount": db.stats()["strategies"],
        "paper": paper,
        "latestBacktest": backtests[0] if backtests else None,
        "recentPaperTrades": recent,
        "paperDailyRealizedPnl": round(daily_pnl, 2),
        "activeTradingRuns": trading.active_count([s for s, _ in states]),
        "tradingRuns": [trading.run_summary(s, t) for s, t in states[:20]],
        "dataSource": "user_imported_or_replay" if selected else None,
    }


def diagnostics() -> dict:
    latest = db.latest_state()
    if not latest:
        return {"available": False, "message": "尚无模拟盘记录。启动一段行情回放后生成诊断。"}
    summary_state, total = latest
    # latest_state() 只读 state_json（轻量列表用），不含行情；
    # 诊断要看 bars，必须按 runId 回表取 market_data_json。
    state = db.get_trading_run(summary_state.get("runId", "")) or summary_state
    bars = state.get("bars") or []
    trades = state.get("trades") or []
    if not bars:
        return {"available": False, "message": "当前模拟盘没有可分析的行情数据。"}

    first_close, last_close = bars[0]["close"], bars[-1]["close"]
    sample = bars[-20:]
    true_ranges = []
    for i, bar in enumerate(sample):
        prev = sample[i - 1]["close"] if i else bar["close"]
        true_ranges.append(max(bar["high"] - bar["low"], abs(bar["high"] - prev), abs(bar["low"] - prev)))
    atr_value = sum(true_ranges) / len(true_ranges) if true_ranges else 0.0

    wins = [t for t in trades if t.get("netPnl", 0) > 0]
    losses = [t for t in trades if t.get("netPnl", 0) < 0]
    win_amount = sum(t["netPnl"] for t in wins)
    loss_amount = abs(sum(t["netPnl"] for t in losses))

    current_day = str(state.get("currentTime") or "")[:10]
    day_trades = [t for t in trades if current_day and str(t.get("exitTime", "")).startswith(current_day)]

    return {
        "available": True,
        "source": "用户导入行情的模拟回放",
        "runId": state.get("runId"),
        "strategyName": state.get("strategyName"),
        "symbol": state.get("symbol"),
        "status": state.get("status"),
        "bars": len(bars),
        "totalBars": total,
        "cursor": state.get("cursor"),
        "startTime": bars[0]["timestamp"],
        "endTime": state.get("currentTime") or bars[-1]["timestamp"],
        "priceChangePct": round((last_close / first_close - 1) * 100, 3) if first_close else None,
        "atrLast20": round(atr_value, 6),
        "atrPctLast20": round(atr_value / last_close * 100, 3) if last_close else None,
        "tradeCount": len(trades),
        "winRatePct": round(len(wins) / len(trades) * 100, 2) if trades else None,
        "profitFactor": round(win_amount / loss_amount, 3) if loss_amount else ("Infinity" if win_amount else None),
        "closedNetPnl": round(sum(t.get("netPnl", 0) for t in trades), 2),
        "costs": round(sum(t.get("fees", 0) for t in trades), 2),
        "equity": state.get("equity"),
        "daily": {
            "date": current_day or None,
            "closedTrades": len(day_trades),
            "netPnl": round(sum(t.get("netPnl", 0) for t in day_trades), 2),
            "signals": [s for s in state.get("signals", [])
                        if current_day and str(s.get("timestamp", "")).startswith(current_day)][-20:],
        },
        "maxDrawdownPct": round(state.get("maxDrawdownPct", 0.0), 3),
        "note": DIAGNOSTIC_NOTE,
    }


# --------------------------------------------------------------------------
# 策略交易记录：点策略卡片「详细」看的那份逐笔成交与盈亏
# --------------------------------------------------------------------------
def _trade_row(trade: dict, source: str, source_label: str,
               segment: str | None = None) -> dict:
    """统一各来源成交的口径：补持仓时长/收益率，打上来源标签。"""
    row = simulate.enrich_trade_basic(trade)
    row["source"] = source
    row["sourceLabel"] = source_label
    if segment:
        row["segment"] = segment
    return row


def strategy_trades(strategy_id: str) -> dict:
    """一条策略的全量成交：常驻纸上交易 + 区间模拟/模拟盘 + 回测，按平仓时间倒序。

    三处的 trades 结构同源（engine._close 写入），差异只在来源标注：
    · 常驻交易优先读内存里的在跑会话——落库节流 30 秒一次，内存才是最新的；
      停机后成交被归档进 trading_runs（tradePrefix=STRAT），从那里认回来。
    · 回测成交还带训练段/测试段。
    """
    strategy = db.get_strategy(strategy_id)
    strategy_name = (strategy or {}).get("name")
    rows: list[dict] = []

    live = runner.trade_history(strategy_id)       # 在跑会话的全量成交（不在跑是 None）
    if live is not None:
        rows.extend(_trade_row(t, "live", "常驻交易") for t in live)
    else:
        # 落库快照兜底：服务重启后恢复出来的会话、以及恢复失败的残留记录
        for record in db.list_strategy_runtimes():
            if record.get("strategyId") != strategy_id:
                continue
            state = record.get("state") or {}
            rows.extend(_trade_row(t, "live", "常驻交易") for t in state.get("trades", []))

    # 区间模拟 / 模拟盘 / 常驻停机归档都在 trading_runs，按 tradePrefix 认来源
    for state, _total in db.list_trading_run_states():
        if state.get("strategyId") != strategy_id:
            continue
        source, label = RUN_SOURCE.get(str(state.get("tradePrefix") or "PAPER"), ("paper", "模拟盘"))
        rows.extend(_trade_row(t, source, label) for t in state.get("trades", []))

    # 回测：成交带训练段/测试段
    for item in db.list_backtests(200):
        if item.get("strategyId") != strategy_id:
            continue
        result = db.get_backtest(item["id"]) or {}
        for trade in result.get("trades", []):
            rows.append(_trade_row(trade, "backtest", "回测", segment=trade.get("segment")))

    rows.sort(key=lambda row: str(row.get("exitTime") or ""), reverse=True)
    if not strategy_name and rows:
        strategy_name = rows[0].get("strategyName")

    wins = [row for row in rows if (row.get("netPnl") or 0) > 0]
    losses = [row for row in rows if (row.get("netPnl") or 0) < 0]
    win_amount = sum(row["netPnl"] for row in wins)
    loss_amount = abs(sum(row["netPnl"] for row in losses))
    by_source = []
    for source, label in (("live", "常驻交易"), ("sim", "区间模拟"),
                          ("paper", "模拟盘"), ("backtest", "回测")):
        group = [row for row in rows if row["source"] == source]
        if group:
            by_source.append({"source": source, "label": label, "count": len(group),
                              "netPnl": round(sum(row["netPnl"] for row in group), 2)})

    return {
        "strategyId": strategy_id,
        "strategyName": strategy_name,
        "trades": rows,
        "summary": {
            "tradeCount": len(rows),
            "netPnl": round(sum(row.get("netPnl") or 0 for row in rows), 2),
            "grossPnl": round(sum(row.get("grossPnl") or 0 for row in rows), 2),
            "feesTotal": round(sum(row.get("fees") or 0 for row in rows), 2),
            "wins": len(wins), "losses": len(losses),
            "winRatePct": round(len(wins) / len(rows) * 100, 2) if rows else 0,
            "profitFactor": (round(win_amount / loss_amount, 4) if loss_amount
                             else (None if not win_amount else "Infinity")),
            "bestTrade": round(max((row["netPnl"] for row in rows), default=0), 2),
            "worstTrade": round(min((row["netPnl"] for row in rows), default=0), 2),
            "bySource": by_source,
        },
        "notice": TRADE_NOTICE,
    }
