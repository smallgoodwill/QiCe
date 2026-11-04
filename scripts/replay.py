#!/usr/bin/env python3
"""命令行回放：与网页端模拟盘共用同一套引擎。

为什么不再单独实现一遍
----------------------
旧版脚本自带一份 EMA / ATR / 撮合循环，和网页端引擎的 ATR 口径不同
（脚本用 EMA 平滑，引擎用简单平均），同一份行情在两边会跑出不同盈亏，
"命令行验证过的策略"拿到网页上就变样。

现在这里只做三件事：解析参数 → 调 app.engine.TradingSession → 导出 CSV。
任何撮合/信号规则的修改都只发生在 app/engine.py 一处。

用法（在项目根目录执行）：
    python scripts/replay.py data/samples/sample_5m_synthetic.csv
    python scripts/replay.py bars.csv --fast 9 --slow 21 --fee-per-side 2 --multiplier 10
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

# 允许直接 `python scripts/replay.py`（把项目根目录加入模块搜索路径）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import engine, marketdata  # noqa: E402  （需先设置 sys.path）

TRADE_FIELDS = ["id", "symbol", "side", "entryTime", "entryPrice", "exitTime", "exitPrice",
                "qty", "multiplier", "grossPnl", "fees", "netPnl", "entryReason", "exitReason"]


def build_config(args) -> dict:
    """把命令行参数收成引擎配置；ATR 阈值对外是百分数。"""
    return engine.normalize_config({
        "fast": args.fast, "slow": args.slow, "maType": args.ma_type,
        "atr": not args.no_atr, "atrPeriod": args.atr_period,
        "atrMaxPct": args.max_atr_pct * 100,     # 0.004 → 0.4（%）
        "rsi": args.rsi, "rsiPeriod": args.rsi_period, "rsiUpper": args.rsi_upper,
        "feePerSide": args.fee_per_side, "slippage": args.slippage,
        "multiplier": args.multiplier, "qty": args.qty, "capital": args.capital,
        "dailyLossLimit": args.daily_loss_limit, "useBlackout": not args.ignore_blackout,
        "tickSize": args.tick_size, "slippageTicks": args.slippage_ticks,
    })


def run(args) -> dict:
    bars = marketdata.load_csv(Path(args.input))
    cfg = build_config(args)
    session = engine.TradingSession(
        bars, cfg, strategy={"name": args.symbol, "symbol": args.symbol},
        trade_prefix="REPLAY", curve_limit=None)

    # 逐 bar 推进：与网页端模拟盘、回测完全同一条撮合路径
    while session.status == "running":
        session.advance()

    summary = engine.summarize_trades(session.trades, session.equity_curve, cfg["capital"])
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)

    trade_path = out / "trades.csv"
    with trade_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRADE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(session.trades)

    # 按平仓日汇总，便于人工核对"当日已实现盈亏"
    daily: dict[str, dict] = {}
    for trade in session.trades:
        day = trade["exitTime"][:10]
        row = daily.setdefault(day, {"date": day, "closed_trades": 0, "net_pnl": 0.0, "fees": 0.0})
        row["closed_trades"] += 1
        row["net_pnl"] = round(row["net_pnl"] + trade["netPnl"], 2)
        row["fees"] = round(row["fees"] + trade["fees"], 2)
    diag_path = out / "daily_diagnostics.csv"
    with diag_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date", "closed_trades", "net_pnl", "fees", "diagnostic"])
        writer.writeheader()
        for day in sorted(daily):
            writer.writerow({**daily[day], "diagnostic": "样本日已平仓记录；不含实时风险监控"})

    result = {
        "mode": "historical_paper_replay",
        "engine": "app.engine.TradingSession",
        "data_notice": "历史/样例数据回放；非实时，不下单",
        "symbol": args.symbol,
        "bars": len(bars),
        "parameters": engine.public_config(cfg),
        **summary,
        "finalEquity": session.equity,
        "maxDrawdownPct": round(session.max_drawdown_pct, 4),
        "startTime": bars[0]["timestamp"],
        "endTime": bars[-1]["timestamp"],
        "caveat": "不含真实合约规则、保证金、涨跌停、换月与完整逐笔撮合；不能消除突发新闻风险。",
    }
    (out / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n输出文件：{trade_path}、{diag_path}、{out / 'summary.json'}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="命令行行情回放（与网页端共用统一策略引擎）")
    parser.add_argument("input", help="CSV：timestamp,open,high,low,close,volume；可选 event_blackout")
    parser.add_argument("--output-dir", default="replay_output")
    parser.add_argument("--symbol", default="DEMO")
    parser.add_argument("--fast", type=int, default=9)
    parser.add_argument("--slow", type=int, default=21)
    parser.add_argument("--ma-type", choices=["EMA", "SMA"], default="EMA")
    parser.add_argument("--atr-period", type=int, default=14)
    parser.add_argument("--max-atr-pct", type=float, default=0.004, help="ATR/close 上限，小数，默认 0.004 = 0.4%%")
    parser.add_argument("--no-atr", action="store_true", help="关闭 ATR 波动门槛")
    parser.add_argument("--rsi", action="store_true", help="启用 RSI 超买过滤")
    parser.add_argument("--rsi-period", type=int, default=14)
    parser.add_argument("--rsi-upper", type=float, default=70.0)
    parser.add_argument("--daily-loss-limit", type=float, default=0.0, help="当日已实现亏损达到此金额后不再开仓；0 表示关闭")
    parser.add_argument("--ignore-blackout", action="store_true", help="忽略 CSV 的 event_blackout 标记")
    parser.add_argument("--capital", type=float, default=1_000_000.0)
    parser.add_argument("--qty", type=int, default=1)
    parser.add_argument("--multiplier", type=float, default=1.0, help="合约乘数；请按品种核实")
    parser.add_argument("--fee-per-side", type=float, default=2.0)
    parser.add_argument("--tick-size", type=float, default=1.0)
    parser.add_argument("--slippage-ticks", type=float, default=1.0)
    parser.add_argument("--slippage", type=float, default=None, help="单边滑点（价格点）；默认 = 跳数 × 最小变动价位")
    args = parser.parse_args()
    if args.slippage is None:
        args.slippage = args.slippage_ticks * args.tick_size
    run(args)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        raise SystemExit(f"错误：{exc}")
