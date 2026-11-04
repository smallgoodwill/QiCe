#!/usr/bin/env python3
"""Educational, long-only futures paper replay. Standard library only."""
import argparse
import csv
import json
import math
from datetime import datetime
from pathlib import Path


def parse_time(value):
    return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))


def load_bars(path):
    bars = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required = {"timestamp", "open", "high", "low", "close", "volume"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError("CSV 缺少字段: " + ", ".join(sorted(missing)))
        for row in reader:
            bar = {"timestamp": parse_time(row["timestamp"]),
                   "open": float(row["open"]), "high": float(row["high"]),
                   "low": float(row["low"]), "close": float(row["close"]),
                   "volume": float(row["volume"]),
                   "event_blackout": row.get("event_blackout", "").strip().lower() in {"1", "true", "yes", "y"}}
            if not (bar["low"] <= min(bar["open"], bar["close"]) <= max(bar["open"], bar["close"]) <= bar["high"]):
                raise ValueError(f"第 {reader.line_num} 行 OHLC 价格关系无效")
            bars.append(bar)
    if len(bars) < 3:
        raise ValueError("至少需要 3 根 bar")
    if any(bars[i]["timestamp"] <= bars[i-1]["timestamp"] for i in range(1, len(bars))):
        raise ValueError("timestamp 必须严格递增且无重复")
    return bars


def ema(values, period):
    result = [None] * len(values)
    if len(values) < period:
        return result
    seed = sum(values[:period]) / period
    result[period - 1] = seed
    alpha = 2 / (period + 1)
    for i in range(period, len(values)):
        seed = alpha * values[i] + (1 - alpha) * seed
        result[i] = seed
    return result


def calculate_atr(bars, period):
    tr = []
    for i, b in enumerate(bars):
        prev_close = bars[i - 1]["close"] if i else b["close"]
        tr.append(max(b["high"] - b["low"], abs(b["high"] - prev_close), abs(b["low"] - prev_close)))
    return ema(tr, period)


def run(args):
    bars = load_bars(args.input)
    if args.fast < 2 or args.slow <= args.fast:
        raise ValueError("参数需满足 2 ≤ fast < slow")
    if args.qty < 1 or args.multiplier <= 0 or args.tick_size <= 0:
        raise ValueError("qty、multiplier、tick-size 必须为正数")
    closes = [b["close"] for b in bars]
    fast, slow = ema(closes, args.fast), ema(closes, args.slow)
    atr = calculate_atr(bars, args.atr_period)
    fee = args.fee_per_side * args.qty
    slip = args.slippage_ticks * args.tick_size
    trades, position = [], None
    daily_realized = {}
    blocked_days = set()

    def close_position(i, reason, raw_price=None):
        nonlocal position
        b = bars[i]
        raw = b["open"] if raw_price is None else raw_price
        exit_price = raw - slip  # long exits assume adverse slippage
        gross = (exit_price - position["entry_price"]) * args.multiplier * args.qty
        total_fee = fee * 2
        net = gross - total_fee
        day = b["timestamp"].date().isoformat()
        daily_realized[day] = daily_realized.get(day, 0) + net
        trades.append({"symbol": args.symbol, "side": "LONG", "entry_time": position["entry_time"].isoformat(),
                       "entry_price": round(position["entry_price"], 6), "exit_time": b["timestamp"].isoformat(),
                       "exit_price": round(exit_price, 6), "qty": args.qty, "multiplier": args.multiplier,
                       "gross_pnl": round(gross, 2), "fees": round(total_fee, 2), "slippage_estimate": round(slip * args.multiplier * args.qty * 2, 2),
                       "net_pnl": round(net, 2), "entry_reason": position["reason"], "exit_reason": reason})
        position = None

    # Signal from bar i close; any order fills at bar i+1 open. Never fill on the signal bar.
    for i in range(1, len(bars) - 1):
        if fast[i] is None or slow[i] is None or fast[i-1] is None or slow[i-1] is None:
            continue
        cross_up = fast[i-1] <= slow[i-1] and fast[i] > slow[i]
        cross_down = fast[i-1] >= slow[i-1] and fast[i] < slow[i]
        next_bar = bars[i + 1]
        signal_day = bars[i]["timestamp"].date().isoformat()
        if position and cross_down:
            close_position(i + 1, "均线死叉，下一根 bar 开盘模拟退出")
        if position is None and cross_up and signal_day not in blocked_days and not bars[i]["event_blackout"] and not next_bar["event_blackout"]:
            # Volatility gate uses only information known at signal close.
            if atr[i] is None or bars[i]["close"] <= 0 or atr[i] / bars[i]["close"] > args.max_atr_pct:
                continue
            # Conservative day stop based on realized net PnL; no new entries after limit.
            if daily_realized.get(signal_day, 0) <= -args.daily_loss_limit:
                blocked_days.add(signal_day)
                continue
            entry_price = next_bar["open"] + slip
            position = {"entry_time": next_bar["timestamp"], "entry_price": entry_price,
                        "reason": f"EMA({args.fast}) 上穿 EMA({args.slow})；ATR/价格={atr[i]/bars[i]['close']:.3%}"}

    if position:
        close_position(len(bars) - 1, "样本结束，按最后一根 bar 收盘模拟平仓", bars[-1]["close"])

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    trade_path = out / "trades.csv"
    fields = ["symbol", "side", "entry_time", "entry_price", "exit_time", "exit_price", "qty", "multiplier", "gross_pnl", "fees", "slippage_estimate", "net_pnl", "entry_reason", "exit_reason"]
    with trade_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader(); writer.writerows(trades)
    daily = {}
    for t in trades:
        day = t["exit_time"][:10]
        daily[day] = daily.get(day, 0) + t["net_pnl"]
    diag_path = out / "daily_diagnostics.csv"
    with diag_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "closed_trades", "net_pnl", "diagnostic"])
        writer.writeheader()
        for day in sorted(daily):
            count = sum(t["exit_time"].startswith(day) for t in trades)
            writer.writerow({"date": day, "closed_trades": count, "net_pnl": round(daily[day], 2),
                             "diagnostic": "样本日已平仓记录；不含实时风险监控"})
    net = round(sum(t["net_pnl"] for t in trades), 2)
    wins = sum(t["net_pnl"] > 0 for t in trades)
    summary = {"mode": "historical_paper_replay", "data_notice": "历史/样例数据回放；非实时，不下单",
               "symbol": args.symbol, "bars": len(bars), "closed_trades": len(trades), "wins": wins,
               "win_rate": round(wins / len(trades), 4) if trades else None, "net_pnl": net,
               "fast_ema": args.fast, "slow_ema": args.slow, "atr_period": args.atr_period,
               "max_atr_pct": args.max_atr_pct, "manual_blackout_bars": sum(b["event_blackout"] for b in bars),
               "caveat": "不含真实合约规则、保证金、涨跌停、换月与完整逐笔撮合；不能消除突发新闻风险。"}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n输出文件：{trade_path}、{diag_path}、{out / 'summary.json'}")


def main():
    p = argparse.ArgumentParser(description="5 分钟均线交叉 + ATR 波动过滤的教学型 paper replay")
    p.add_argument("input", help="CSV 文件：timestamp,open,high,low,close,volume；可选 event_blackout")
    p.add_argument("--output-dir", default="replay_output")
    p.add_argument("--symbol", default="DEMO")
    p.add_argument("--fast", type=int, default=9)
    p.add_argument("--slow", type=int, default=21)
    p.add_argument("--atr-period", type=int, default=14)
    p.add_argument("--max-atr-pct", type=float, default=0.004, help="ATR/close 上限，默认 0.4%%")
    p.add_argument("--daily-loss-limit", type=float, default=1000, help="当日已实现亏损达到此金额后不再开仓")
    p.add_argument("--qty", type=int, default=1)
    p.add_argument("--multiplier", type=float, default=1, help="演示盈亏乘数；请按品种核实")
    p.add_argument("--fee-per-side", type=float, default=2)
    p.add_argument("--tick-size", type=float, default=1)
    p.add_argument("--slippage-ticks", type=float, default=1)
    args = p.parse_args()
    run(args)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        raise SystemExit(f"错误：{exc}")
