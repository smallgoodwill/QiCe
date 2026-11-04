"""聚合视图：仪表盘与诊断。

抽出来的理由：这些是"读多表拼视图"的展示逻辑，和路由/SQL 都不该混在一起。
所有数值都来自数据库里的真实回放记录，没有数据就返回空状态，不做推测。
"""
from __future__ import annotations

from . import db, trading

DIAGNOSTIC_NOTE = "诊断仅汇总当前回放样本，不构成品种推荐或策略有效性结论。"


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
    state, total = latest
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
