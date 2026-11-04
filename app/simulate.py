"""策略区间模拟盘：给定「策略 + 行情来源 + 时间段」，一次跑完并给出盈亏明细。

和 trading.py 的分工
--------------------
· trading.py 是"逐 bar 推进 + 前端轮询"的实时回放，用来边跑边看进度；
· 本模块是"区间一次算完"，回答的是"这条策略在这段时间赚了多少、买卖点在哪"。

两者共用 engine.TradingSession，撮合口径（收盘确认信号、下一根开盘成交、
双边手续费、固定滑点）完全一致，不会出现同一段行情两个结果。

行情来源
--------
· demo：服务端按交易时段合成 1 分钟 K 线，确定性、可复现，用来验证策略与界面；
· live：实时行情页从 CTP 采集并落库的分钟 K 线，真实但只能从接入之后开始积累。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from . import config, db, engine, marketdata

DEMO_NOTICE = ("演示行情由服务端按交易时段合成，同一日期永远生成同一段走势；"
               "它的作用是把「信号→成交→盈亏」链路走通，不代表任何真实品种。")
LIVE_NOTICE = ("真实行情取自本机采集库（CTP 逐笔聚合的 1 分钟 K 线）。"
               "柜台只推实时行情、不回溯历史，所以区间从你开始采集的那一分钟算起。")

MAX_DAYS = 20
DEFAULT_CONFIG = {
    "capital": 1_000_000, "multiplier": 300, "qty": 1,
    "feePerSide": 23.0, "tickSize": 0.2, "slippageTicks": 1,
    "atrMaxPct": 0.4, "atrPeriod": 14,
}


# --------------------------------------------------------------------------
# 选项（前端首屏用）
# --------------------------------------------------------------------------
def options() -> dict:
    contracts = [row for row in db.live_bar_sources() if row["source"] == "ctp"]
    ready = [row for row in contracts if row["count"] >= config.MIN_BARS]
    today = datetime.now(marketdata.CN_TZ).date()
    return {
        "today": today.isoformat(),
        "maxDays": MAX_DAYS,
        "minBars": config.MIN_BARS,
        "sources": [
            {"id": "demo", "label": "演示行情",
             "desc": "按交易时段合成的 1 分钟 K 线，选好日期就能跑，结果可复现。",
             "notice": DEMO_NOTICE},
            {"id": "live", "label": "真实采集",
             "desc": "本机从 CTP 采集并落库的分钟 K 线，只能覆盖开始采集之后的时间。",
             "notice": LIVE_NOTICE},
        ],
        "contracts": contracts,
        # 采集够了才默认"真实采集"，否则第一次打开就报错，体验很差
        "defaults": {**DEFAULT_CONFIG, "days": 3,
                     "source": "live" if ready else "demo"},
    }


# --------------------------------------------------------------------------
# 区间与行情
# --------------------------------------------------------------------------
def _parse_day(value: str, field: str) -> date:
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except (TypeError, ValueError):
        raise ValueError(f"{field}需为 YYYY-MM-DD 格式的日期。") from None


def _resolve_range(payload: dict) -> tuple[date, date, list[date]]:
    start_raw, end_raw = payload.get("start"), payload.get("end")
    if start_raw and end_raw:
        start, end = _parse_day(start_raw, "开始日期"), _parse_day(end_raw, "结束日期")
        if end < start:
            start, end = end, start
        days = marketdata.trading_days(start, end, limit=MAX_DAYS + 1)
    else:
        try:
            count = int(payload.get("days") or 3)
        except (TypeError, ValueError):
            raise ValueError("天数需为整数。") from None
        count = max(1, min(MAX_DAYS, count))
        days = marketdata.recent_trading_days(count)
    if not days:
        raise ValueError("所选区间内没有交易日（周一至周五），请重新选择。")
    if len(days) > MAX_DAYS:
        raise ValueError(f"一次最多模拟 {MAX_DAYS} 个交易日，请缩短区间。")
    return days[0], days[-1], days


def _load_demo(payload: dict, days: list[date], symbol: str) -> tuple[list[dict], dict]:
    tick = float(payload.get("config", {}).get("tickSize") or DEFAULT_CONFIG["tickSize"])
    base = float(payload.get("basePrice") or 4000.0)
    # 截止到"现在"：当天未收盘就只生成已经发生的分钟，避免出现未来时间的 bar
    bars = marketdata.synthetic_bars(days, symbol=symbol, base_price=base, tick=tick,
                                     until=datetime.now(marketdata.CN_TZ))
    meta = {"kind": "demo", "label": "演示行情（合成）", "symbol": symbol,
            "notice": DEMO_NOTICE, "dayCount": len(days),
            "days": [day.isoformat() for day in days]}
    return bars, meta


def _load_live(payload: dict, days: list[date], symbol: str) -> tuple[list[dict], dict]:
    start_ts = f"{days[0].isoformat()}T00:00:00+08:00"
    end_ts = f"{days[-1].isoformat()}T23:59:59+08:00"
    bars = db.live_bars_range(symbol, start_ts, end_ts, source="ctp")
    meta = {"kind": "live", "label": "真实采集（CTP）", "symbol": symbol,
            "notice": LIVE_NOTICE, "dayCount": len(days),
            "days": [day.isoformat() for day in days],
            "startTime": bars[0]["timestamp"] if bars else None,
            "endTime": bars[-1]["timestamp"] if bars else None}
    return bars, meta


def _live_hint() -> str:
    contracts = [row for row in db.live_bar_sources() if row["source"] == "ctp"]
    if not contracts:
        return "本机还没有采集到真实行情：先到「实时 K 线」页启动 CTP 采集，或改用演示行情。"
    detail = "、".join(f"{row['symbol']} {row['count']} 根" for row in contracts[:3])
    return f"当前已采集：{detail}。到「实时 K 线」页保持采集，攒够根数后再跑；也可以先用演示行情。"


# --------------------------------------------------------------------------
# 结果组装
# --------------------------------------------------------------------------
def _minutes_between(start: str, end: str) -> int | None:
    try:
        delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return None
    return max(0, round(delta.total_seconds() / 60))


def _hold_label(minutes: int | None) -> str:
    if minutes is None:
        return "—"
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时{rest:02d} 分"
    days, hours = divmod(hours, 24)
    return f"{days} 天{hours:02d} 小时"


def _enrich_trade(trade: dict, bars: list[dict], cfg: dict) -> dict:
    """给逐笔成交补上持仓时长、收益率和持仓期间的最大浮盈/浮亏。"""
    hold = _minutes_between(trade["entryTime"], trade["exitTime"])
    notional = trade["entryPrice"] * cfg["multiplier"] * trade["qty"]
    entry_idx, exit_idx = trade["entryIdx"], trade["exitIdx"]
    window = bars[entry_idx:exit_idx + 1] if 0 <= entry_idx <= exit_idx < len(bars) else []
    mfe = mae = None
    if window:
        highest = max(bar["high"] for bar in window)
        lowest = min(bar["low"] for bar in window)
        mfe = round((highest - trade["entryPrice"]) / trade["entryPrice"] * 100, 3)
        mae = round((lowest - trade["entryPrice"]) / trade["entryPrice"] * 100, 3)
    return {**trade, "holdMinutes": hold, "holdLabel": _hold_label(hold),
            "returnPct": round(trade["netPnl"] / notional * 100, 4) if notional else None,
            "mfePct": mfe, "maePct": mae}


def _daily_stats(trades: list[dict], curve: list[dict], days: list[str]) -> list[dict]:
    rows: dict[str, dict] = {
        day: {"date": day, "tradeCount": 0, "netPnl": 0.0, "fees": 0.0,
              "wins": 0, "losses": 0, "dayEndEquity": None}
        for day in days
    }
    for trade in trades:
        day = str(trade.get("exitTime", ""))[:10]
        row = rows.setdefault(day, {"date": day, "tradeCount": 0, "netPnl": 0.0, "fees": 0.0,
                                    "wins": 0, "losses": 0, "dayEndEquity": None})
        row["tradeCount"] += 1
        row["netPnl"] += trade["netPnl"]
        row["fees"] += trade["fees"]
        if trade["netPnl"] > 0:
            row["wins"] += 1
        elif trade["netPnl"] < 0:
            row["losses"] += 1
    for point in curve:
        day = str(point["timestamp"])[:10]
        if day in rows:
            rows[day]["dayEndEquity"] = point["equity"]
    ordered = [rows[key] for key in sorted(rows)]
    cumulative = 0.0
    for row in ordered:
        row["netPnl"] = round(row["netPnl"], 2)
        row["fees"] = round(row["fees"], 2)
        cumulative += row["netPnl"]
        row["cumPnl"] = round(cumulative, 2)
    return ordered


def build_result(session: engine.TradingSession, meta: dict) -> dict:
    cfg = session.cfg
    bars = session.bars
    base_close = bars[0]["close"] or 1.0
    curve = [{"timestamp": point["timestamp"], "equity": point["equity"], "i": point["i"],
              "benchmark": round(cfg["capital"] * bars[point["i"]]["close"] / base_close, 2)}
             for point in sorted(session.equity_curve, key=lambda item: item["i"])]
    trades = [_enrich_trade(trade, bars, cfg) for trade in session.trades]
    summary = engine.summarize_trades(trades, curve, cfg["capital"])

    holds = [trade["holdMinutes"] for trade in trades if trade["holdMinutes"] is not None]
    winners = [trade["netPnl"] for trade in trades if trade["netPnl"] > 0]
    losers = [trade["netPnl"] for trade in trades if trade["netPnl"] < 0]
    summary.update({
        "grossPnl": round(sum(trade["grossPnl"] for trade in trades), 2),
        "feesTotal": round(sum(trade["fees"] for trade in trades), 2),
        "avgHoldMinutes": round(sum(holds) / len(holds)) if holds else 0,
        "longestHoldMinutes": max(holds) if holds else 0,
        "avgWin": round(sum(winners) / len(winners), 2) if winners else 0,
        "avgLoss": round(sum(losers) / len(losers), 2) if losers else 0,
        "bestTrade": round(max(winners), 2) if winners else 0,
        "worstTrade": round(min(losers), 2) if losers else 0,
        "expectancy": round(summary["netPnl"] / len(trades), 2) if trades else 0,
        "endEquity": round(curve[-1]["equity"], 2) if curve else cfg["capital"],
    })

    return {
        "runId": session.run_id,
        "createdAt": session.created_at,
        "strategyId": session.strategy_id,
        "strategyName": session.strategy_name,
        "symbol": session.symbol,
        "source": meta,
        "parameters": engine.public_config(cfg),
        "startTime": bars[0]["timestamp"],
        "endTime": bars[-1]["timestamp"],
        "barCount": len(bars),
        "bars": bars,
        "curve": curve,
        "trades": trades,
        "daily": _daily_stats(trades, curve, [day["timestamp"][:10] for day in bars]),
        "summary": summary,
        "notice": meta.get("notice") or DEMO_NOTICE,
    }


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def run(payload: dict, strategy: dict | None) -> dict:
    source = str(payload.get("source") or "demo").lower()
    if source not in ("demo", "live"):
        raise ValueError("行情来源只能是 demo（演示行情）或 live（真实采集）。")

    # 没选策略时用一条内置均线策略兜底，保证第一次打开页面就能出结果
    if not strategy:
        strategy = {"id": None, "name": "默认均线策略（EMA 9/21）", "fast": 9, "slow": 21,
                    "maType": "EMA", "atr": True, "atrPeriod": 14, "rsi": False}

    raw_cfg = {**DEFAULT_CONFIG}
    raw_cfg.update(strategy)
    if isinstance(payload.get("config"), dict):
        raw_cfg.update(payload["config"])
    cfg = engine.normalize_config(raw_cfg)

    days = _resolve_range(payload)[2]
    if source == "live":
        # 真实行情按合约名查库；策略里存的是"沪深300股指 · IF2503"这类展示名，不能直接用
        stored = [row["symbol"] for row in db.live_bar_sources() if row["source"] == "ctp"]
        symbol = str(payload.get("symbol") or (stored[0] if stored else "IF2612"))
    else:
        symbol = "IF-DEMO"

    if source == "demo":
        bars, meta = _load_demo(payload, days, symbol)
    else:
        bars, meta = _load_live(payload, days, symbol)

    # 均线要先预热 slow 根，再留几根才有成交；低于这个数跑出来必然是 0 笔，不如直接说清楚
    need = max(config.MIN_BARS, cfg["slow"] + 5)
    if len(bars) < need:
        hint = _live_hint() if source == "live" else ""
        raise ValueError(
            f"该区间只有 {len(bars)} 根 bar，至少需要 {need} 根"
            f"（慢线 {cfg['slow']} 根预热 + 5 根成交）。{hint}")

    engine.validate_bars(bars)
    strategy_info = {"id": (strategy or {}).get("id"), "name": (strategy or {}).get("name") or "未命名策略",
                     "symbol": symbol}
    # curve_limit=None：区间模拟要完整权益曲线，不做滚动截断
    session = engine.TradingSession(bars, cfg, strategy=strategy_info,
                                    trade_prefix="SIM", curve_limit=None)
    session.meta = meta
    while session.status == "running":
        session.advance()

    result = build_result(session, meta)
    db.save_trading_run(session)
    return result


def get(run_id: str) -> dict:
    record = db.get_trading_run(run_id)
    if not record:
        raise ValueError("模拟记录不存在。")
    session = engine.TradingSession.restore(record)
    return build_result(session, record.get("sourceMeta") or {"kind": "unknown", "label": "历史记录"})


def history(limit: int = 12) -> list[dict]:
    """历史模拟记录（只取本模块写的 SIM 前缀运行，避免和实时回放混在一起）。"""
    items = []
    for state, total in db.list_trading_run_states():
        if state.get("tradePrefix") != "SIM":
            continue
        trades = state.get("trades") or []
        net = sum(trade.get("netPnl", 0) for trade in trades)
        items.append({
            "runId": state["runId"],
            "createdAt": state.get("createdAt"),
            "strategyName": state.get("strategyName"),
            "symbol": state.get("symbol"),
            "status": state.get("status"),
            "source": (state.get("sourceMeta") or {}).get("kind"),
            "barCount": total,
            "tradeCount": len(trades),
            "netPnl": round(net, 2),
            "startTime": (state.get("sourceMeta") or {}).get("days", [None])[0],
            "endTime": (state.get("sourceMeta") or {}).get("days", [None])[-1],
        })
        if len(items) >= limit:
            break
    return items
