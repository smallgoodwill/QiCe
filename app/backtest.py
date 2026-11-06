"""回测编排：训练段/测试段切分 + 指标汇总。

业务口径（和前端页面文案保持一致，改这里要同步改 UI 说明）：
  · 信号在 bar 收盘确认，下一根 bar 开盘价加减滑点成交；
  · 到达切分点强制平仓，训练段的盈亏不带入测试段；
  · 手续费按"每边每手"计，一手来回记两次；
  · 基准 = 买入持有（首根收盘价起算）。

行情来源有两条，前端只走第一条：
  · `range` = {symbol, start, end, period}：服务端按时间区间拉真实历史行情
    （`history.py`），不再需要用户导入 CSV；
  · `bars`：直接给行情数组，仅供本地自检脚本与旧 API 调用方使用。
"""
from __future__ import annotations

from . import engine, history

NOTICE = "结果为历史/模拟回放；不包含完整期货合约、保证金、涨跌停、换月与真实撮合规则。"
LEGACY_SOURCE = "请求内行情数组（本地自检 / 旧 API 调用方）"


def _load_bars(payload: dict) -> tuple[list[dict], dict | None]:
    """返回 (校验过的 bars, 数据源元信息)；元信息会写进回测结果并落库。"""
    spec = payload.get("range") or payload.get("dataRange")
    if spec is None:
        return engine.validate_bars(payload.get("bars")), None
    if not isinstance(spec, dict):
        raise ValueError("回测区间格式不正确。")
    dataset = history.fetch(spec.get("symbol"), spec.get("start"), spec.get("end"),
                            str(spec.get("period") or "1d").lower())
    bars = engine.validate_bars(dataset["bars"])
    meta = {key: value for key, value in dataset.items() if key != "bars"}
    return bars, meta


def run_backtest(payload: dict) -> dict:
    bars, source = _load_bars(payload)
    cfg = engine.normalize_config(payload.get("config"))
    split_pct = int(payload.get("config", {}).get("splitPct", 70)) if isinstance(payload.get("config"), dict) else 70
    if not (30 <= split_pct <= 90):
        raise ValueError("训练段比例需在 30%–90% 之间。")

    strategy = payload.get("strategy") or {}
    split = int(len(bars) * split_pct / 100)
    if split <= cfg["slow"]:
        raise ValueError("训练段太短，无法预热慢线，请增加行情或提高训练段比例。")

    session = engine.TradingSession(bars, cfg, strategy=strategy, trade_prefix="BT", curve_limit=None)

    # 逐 bar 推进：与模拟盘共用同一条撮合路径
    for i in range(1, len(bars)):
        session.cursor = i
        if i == split:
            session.force_close(i, "训练段结束，边界清仓", bars[i]["open"])
        session.advance()

    base_close = bars[0]["close"] or 1.0
    # 切分点会先后产生两个同索引权益点（force_close 记一次、advance 记一次），
    # 这里按索引去重并保留后一个，否则图表会在该处出现垂直断点、统计也会重复计入。
    by_index: dict[int, float] = {}
    for point in session.equity_curve:
        by_index[point["i"]] = point["equity"]
    curve = [{"i": idx, "equity": equity,
              "benchmark": round(cfg["capital"] * bars[idx]["close"] / base_close, 2)}
             for idx, equity in sorted(by_index.items())]

    trades = []
    for trade in session.trades:
        trades.append({**trade, "segment": "训练段" if trade["entryIdx"] < split else "测试段"})

    overall = engine.summarize_trades(trades, curve, cfg["capital"])
    train_trades = [t for t in trades if t["entryIdx"] < split]
    test_trades = [t for t in trades if t["entryIdx"] >= split]
    train_curve = [p for p in curve if p["i"] < split]
    test_curve = [p for p in curve if p["i"] >= split]
    test_start = test_curve[0]["equity"] if test_curve else cfg["capital"]

    return {
        "id": session.run_id,
        "strategyId": payload.get("strategyId"),
        "strategyName": strategy.get("name"),
        "createdAt": session.created_at,
        "sourceBars": len(bars),
        "startTime": bars[0]["timestamp"],
        "endTime": bars[-1]["timestamp"],
        "splitIndex": split,
        "splitPct": split_pct,
        "parameters": engine.public_config(cfg),
        "summary": overall,
        "curve": curve,
        "trades": trades,
        "segments": [{"name": "训练段", **engine.summarize_trades(train_trades, train_curve, cfg["capital"])},
                     {"name": "测试段", **engine.summarize_trades(test_trades, test_curve, test_start)}],
        # 数据源随结果落库：翻旧记录时能看清「哪段真行情、什么周期、可用范围到哪」
        "dataSource": source["source"] if source else LEGACY_SOURCE,
        "sourceKind": source["sourceKind"] if source else "payload-bars",
        "symbol": source["symbol"] if source else None,
        "continuous": source["continuous"] if source else None,
        "period": source["period"] if source else None,
        "periodLabel": source["periodLabel"] if source else None,
        "requestedRange": {"start": source["start"], "end": source["end"]} if source else None,
        "coverage": source["coverage"] if source else None,
        "warnings": source["warnings"] if source else [],
        "notice": source["notice"] if source else NOTICE,
    }
