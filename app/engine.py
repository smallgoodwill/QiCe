"""统一策略引擎：行情校验 + 参数归一 + 信号判定 + 撮合记账。

设计要点（这是本次重构的核心）
--------------------------------
回测与模拟盘以前是两套独立实现，同一策略、同一份行情会跑出不同结果：
  · 回测里 `fast_line[sig-1]` 在 sig==0 时会索引到列表末尾（Python 负索引），
    凭空产生一个"幽灵金叉"；
  · 回测在训练段切分点用 `continue` 跳过了一根 bar；
  · 两边的过滤器判定口径（用 sig 还是 i 的 bar）不一致。

现在只有一个 `TradingSession`：它按 bar 顺序逐根推进，每一根 bar 只做一件事——
用**上一根已收盘 bar** 的信号，在**当前 bar 开盘价**撮合。回测就是"循环调用
advance()"，模拟盘就是"每次 HTTP 请求调用一次 advance()"。两者结果必然一致。
"""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from . import config, indicators


# --------------------------------------------------------------------------
# 行情校验
# --------------------------------------------------------------------------
def validate_bars(raw: Any) -> list[dict]:
    """把请求里的行情数组标准化为内部 bar 列表。

    规则：30–20000 根、时间严格递增、OHLC 关系合法、数值有限、成交量非负。
    """
    if not isinstance(raw, list) or not (config.MIN_BARS <= len(raw) <= config.MAX_BARS):
        raise ValueError(f"行情数据需为 {config.MIN_BARS}–{config.MAX_BARS:,} 根 bar。")
    bars: list[dict] = []
    previous: datetime | None = None
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError("行情每行必须是对象。")
        stamp = str(row.get("timestamp", "")).strip()
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            values = {k: float(row[k]) for k in ("open", "high", "low", "close", "volume")}
        except (ValueError, TypeError, KeyError):
            raise ValueError("bar 时间或 OHLCV 字段格式无效。") from None
        if any(not math.isfinite(v) for v in values.values()) or values["volume"] < 0:
            raise ValueError("bar 含有非有限数值或负成交量。")
        if previous is not None and parsed <= previous:
            raise ValueError("行情时间必须严格递增且不能重复。")
        previous = parsed
        if values["low"] > min(values["open"], values["close"]) or values["high"] < max(values["open"], values["close"]):
            raise ValueError("OHLC 价格关系无效。")
        bars.append({"timestamp": stamp, **values, "event_blackout": _to_bool(row.get("event_blackout"))})
    return bars


def _to_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(value)


def _num(raw: dict, *names: str, default: float) -> float:
    """按别名顺序取第一个存在的数值字段。"""
    for name in names:
        if name in raw and raw[name] is not None and raw[name] != "":
            return float(raw[name])
    return default


def _flag(raw: dict, *names: str, default: bool) -> bool:
    for name in names:
        if name in raw and raw[name] is not None:
            return _to_bool(raw[name])
    return default


# --------------------------------------------------------------------------
# 策略参数
# --------------------------------------------------------------------------
def normalize_config(raw: Any) -> dict:
    """把前端/策略库的宽松参数收敛为一份完整、经过校验的配置。

    字段别名是为了兼容历史页面（`atr`/`atrEnabled`、`rsi`/`rsiEnabled`）。
    `atrMaxPct` 对外统一用百分数（0.4 = 0.4%），内部换算为小数。
    """
    raw = raw if isinstance(raw, dict) else {}
    cfg = {
        "maType": "SMA" if str(raw.get("maType", "EMA")).upper() == "SMA" else "EMA",
        "fast": int(_num(raw, "fast", default=9)),
        "slow": int(_num(raw, "slow", default=21)),
        "atr": _flag(raw, "atr", "atrEnabled", default=True),
        "atrPeriod": int(_num(raw, "atrPeriod", default=14)),
        "atrMaxPct": _num(raw, "atrMaxPct", default=0.4) / 100,   # 内部用小数
        "rsi": _flag(raw, "rsi", "rsiEnabled", default=False),
        "rsiPeriod": int(_num(raw, "rsiPeriod", default=14)),
        "rsiUpper": _num(raw, "rsiUpper", default=70.0),
        "feePerSide": _num(raw, "feePerSide", default=2.0),
        "slippage": _num(raw, "slippage", default=1.0),
        "multiplier": _num(raw, "multiplier", default=1.0),
        "qty": int(_num(raw, "qty", default=1)),
        "capital": _num(raw, "capital", default=1_000_000.0),
        # 0 表示关闭"单日已实现亏损停开"风控（回测默认关闭，模拟盘页面会显式传值）
        "dailyLossLimit": _num(raw, "dailyLossLimit", default=0.0),
        "useBlackout": _flag(raw, "useBlackout", default=True),
        "tickSize": _num(raw, "tickSize", default=1.0),
        "slippageTicks": _num(raw, "slippageTicks", default=1.0),
    }
    _validate_config(cfg)
    return cfg


def _validate_config(cfg: dict) -> None:
    if not (2 <= cfg["fast"] < cfg["slow"] <= 500):
        raise ValueError("策略参数不合法：需满足 2 ≤ 快线 < 慢线 ≤ 500。")
    if not (2 <= cfg["atrPeriod"] <= 500):
        raise ValueError("ATR 周期需在 2–500 之间。")
    if not (2 <= cfg["rsiPeriod"] <= 100):
        raise ValueError("RSI 周期需在 2–100 之间。")
    if not (0 <= cfg["rsiUpper"] <= 100):
        raise ValueError("RSI 阈值需在 0–100 之间。")
    if min(cfg["atrMaxPct"], cfg["feePerSide"], cfg["slippage"], cfg["dailyLossLimit"], cfg["slippageTicks"]) < 0:
        raise ValueError("波动阈值、费用、滑点与亏损限额不能为负。")
    if min(cfg["multiplier"], cfg["qty"], cfg["capital"], cfg["tickSize"]) <= 0:
        raise ValueError("合约乘数、手数、初始资金与最小变动价位必须大于 0。")


def public_config(cfg: dict) -> dict:
    """回显给前端时把内部小数阈值还原成百分数，避免口径误解。"""
    out = dict(cfg)
    out["atrMaxPct"] = round(cfg["atrMaxPct"] * 100, 6)
    return out


# --------------------------------------------------------------------------
# 撮合记账
# --------------------------------------------------------------------------
class TradingSession:
    """单策略、单向做多、逐 bar 推进的模拟会话。

    生命周期：create() 建仓态 → 反复 advance() → status 变为 completed/stopped。
    `split_index` 只有回测用：到达该 bar 前强制平仓，使训练段与测试段的盈亏不串味。
    """

    def __init__(self, bars: list[dict], cfg: dict, *, strategy: dict | None = None,
                 run_id: str | None = None, created_at: str | None = None,
                 trade_prefix: str = "PAPER", curve_limit: int | None = 1000):
        strategy = strategy or {}
        self.bars = bars
        self.cfg = cfg
        self.run_id = run_id or str(uuid.uuid4())
        self.created_at = created_at or _now_iso()
        self.trade_prefix = trade_prefix
        self.curve_limit = curve_limit
        self.strategy_id = strategy.get("id")
        self.strategy_name = strategy.get("name") or "未命名策略"
        self.symbol = strategy.get("symbol") or "DEMO"

        closes = [bar["close"] for bar in bars]
        self.series = {
            "fastLine": indicators.moving(closes, cfg["fast"], cfg["maType"]),
            "slowLine": indicators.moving(closes, cfg["slow"], cfg["maType"]),
            "atr": indicators.atr(bars, cfg["atrPeriod"]),
            "rsi": indicators.rsi(closes, cfg["rsiPeriod"]),
        }

        self.status = "running"
        self.cursor = 1
        self.cash = cfg["capital"]
        self.position: dict | None = None
        self.trades: list[dict] = []
        self.signals: list[dict] = []
        self.equity_curve: list[dict] = []
        self.daily_pnl: dict[str, float] = {}
        self.realized_pnl = 0.0
        self.unrealized_pnl = 0.0
        self.equity = cfg["capital"]
        self.peak_equity = cfg["capital"]
        self.max_drawdown_pct = 0.0
        self.current_time: str | None = None
        self.last_message = "模拟回放已创建，等待逐 bar 推进。"

    # -- 序列化 ------------------------------------------------------------
    def market_data(self) -> dict:
        """行情与指标序列（体量大，单独存一列）。"""
        return {"bars": self.bars, **self.series}

    def state(self) -> dict:
        """可持久化的会话状态（不含行情）。"""
        return {
            "version": 2, "runId": self.run_id, "createdAt": self.created_at, "status": self.status,
            "strategyId": self.strategy_id, "strategyName": self.strategy_name, "symbol": self.symbol,
            "cursor": self.cursor, "config": self.cfg, "cash": self.cash, "position": self.position,
            "trades": self.trades, "signals": self.signals[-400:], "equityCurve": self.equity_curve[-1000:],
            "dailyPnl": self.daily_pnl, "realizedPnl": self.realized_pnl, "unrealizedPnl": self.unrealized_pnl,
            "equity": self.equity, "peakEquity": self.peak_equity, "maxDrawdownPct": self.max_drawdown_pct,
            "currentTime": self.current_time, "lastMessage": self.last_message,
            "tradePrefix": self.trade_prefix, "curveLimit": self.curve_limit,
        }

    @classmethod
    def restore(cls, record: dict) -> "TradingSession":
        """从 db.get_trading_run() 的合并记录恢复会话（含 bars 与指标序列）。"""
        state = record
        session = cls(record["bars"], record["config"],
                      strategy={"id": record.get("strategyId"), "name": record.get("strategyName"),
                                "symbol": record.get("symbol")},
                      run_id=record["runId"], created_at=record.get("createdAt"),
                      trade_prefix=record.get("tradePrefix", "PAPER"),
                      curve_limit=record.get("curveLimit", 1000))
        for key in ("fastLine", "slowLine", "atr", "rsi"):
            if record.get(key):
                session.series[key] = record[key]
        for key in ("status", "cursor", "cash", "position", "trades", "signals", "equityCurve",
                    "dailyPnl", "realizedPnl", "unrealizedPnl", "equity", "peakEquity",
                    "maxDrawdownPct", "currentTime", "lastMessage"):
            if key in state:
                setattr(session, key, state[key])
        return session

    # -- 撮合 --------------------------------------------------------------
    def advance(self) -> "TradingSession":
        """处理 cursor 指向的那一根 bar，然后 cursor+1。"""
        if self.status != "running":
            return self
        total = len(self.bars)
        if self.cursor >= total:
            self._finish()
            return self

        i = self.cursor
        bar = self.bars[i]
        sig = i - 1                      # 信号只能来自已收盘的上一根 bar

        cross_up, cross_down, blockers = self._evaluate(sig)

        if self.position and cross_down:
            self._close(i, "均线死叉，下一根 bar 开盘退出", bar["open"])

        if not self.position and cross_up:
            if i >= total - 1:
                blockers = [*blockers, "样本末端不新开仓"]
            if blockers:
                self._log_signal(bar["timestamp"], "skipped", "；".join(blockers))
            else:
                self._open(i)

        if i == total - 1 and self.position:
            self._close(i, "回放结束，按最后收盘退出", bar["close"])

        self._mark(bar, i)
        self.cursor += 1
        if self.cursor >= total:
            self._finish()
        else:
            self.last_message = f"已处理 {self.cursor} / {total} 根 bar。"
        return self

    def force_close(self, index: int, reason: str, raw_price: float | None = None) -> None:
        """外部强制平仓（回测切分边界、用户取消运行）。"""
        if not self.position:
            return
        index = max(0, min(len(self.bars) - 1, index))
        self._close(index, reason, self.bars[index]["open"] if raw_price is None else raw_price)
        self._mark(self.bars[index], index)

    # -- 内部 --------------------------------------------------------------
    def _evaluate(self, sig: int) -> tuple[bool, bool, list[str]]:
        """在 sig 这根已收盘 bar 上判定信号与过滤器。

        注意 sig 必须 ≥1：`sig-1` 一旦为 -1 就会索引到列表末尾，
        这正是旧回测里"幽灵金叉"的成因。
        """
        if sig < 1:
            return False, False, []
        fast, slow = self.series["fastLine"], self.series["slowLine"]
        window = (fast[sig], slow[sig], fast[sig - 1], slow[sig - 1])
        if any(value is None for value in window):
            return False, False, []
        cross_up = fast[sig - 1] <= slow[sig - 1] and fast[sig] > slow[sig]
        cross_down = fast[sig - 1] >= slow[sig - 1] and fast[sig] < slow[sig]

        cfg, bars, bar = self.cfg, self.bars, self.bars[sig]
        blockers: list[str] = []
        if cfg["atr"]:
            atr_value = self.series["atr"][sig]
            if atr_value is None or bar["close"] <= 0 or atr_value / bar["close"] > cfg["atrMaxPct"]:
                blockers.append("ATR 波动超过阈值")
        if cfg["rsi"]:
            rsi_value = self.series["rsi"][sig]
            if rsi_value is None or rsi_value >= cfg["rsiUpper"]:
                blockers.append(f"RSI ≥ {cfg['rsiUpper']:g}（超买）")
        if cfg["useBlackout"] and (bars[sig]["event_blackout"] or self.bars[self.cursor]["event_blackout"]):
            blockers.append("事件禁入 bar")
        if cfg["dailyLossLimit"] > 0:
            day = bars[sig]["timestamp"][:10]
            if self.daily_pnl.get(day, 0.0) <= -cfg["dailyLossLimit"]:
                blockers.append("已达当日已实现亏损限额")
        return cross_up, cross_down, blockers

    def _open(self, i: int) -> None:
        cfg, bar = self.cfg, self.bars[i]
        price = bar["open"] + cfg["slippage"]
        self.position = {"side": "LONG", "qty": cfg["qty"], "entryIdx": i,
                         "entryTime": bar["timestamp"], "entryPrice": price,
                         "reason": f"{cfg['maType']}({cfg['fast']}) 上穿 ({cfg['slow']}) 且过滤器通过"}
        self._log_signal(bar["timestamp"], "entry", "均线金叉通过过滤")

    def _close(self, i: int, reason: str, raw_price: float) -> None:
        cfg, bar, pos = self.cfg, self.bars[i], self.position
        price = raw_price - cfg["slippage"]
        gross = (price - pos["entryPrice"]) * cfg["multiplier"] * cfg["qty"]
        fees = cfg["feePerSide"] * cfg["qty"] * 2
        net = gross - fees
        self.cash += net
        self.realized_pnl += net
        day = bar["timestamp"][:10]
        self.daily_pnl[day] = self.daily_pnl.get(day, 0.0) + net
        self.trades.append({
            "id": f"{self.trade_prefix}-{len(self.trades) + 1:05d}", "runId": self.run_id,
            "strategyName": self.strategy_name, "symbol": self.symbol, "side": "做多",
            "entryIdx": pos["entryIdx"], "exitIdx": i,
            "entryTime": pos["entryTime"], "entryPrice": round(pos["entryPrice"], 6),
            "exitTime": bar["timestamp"], "exitPrice": round(price, 6),
            "qty": cfg["qty"], "multiplier": cfg["multiplier"],
            "grossPnl": round(gross, 2), "fees": round(fees, 2), "netPnl": round(net, 2),
            "entryReason": pos["reason"], "exitReason": reason,
        })
        self.position = None
        self.unrealized_pnl = 0.0
        self._log_signal(bar["timestamp"], "exit", reason)

    def _mark(self, bar: dict, index: int) -> None:
        cfg = self.cfg
        if self.position:
            self.unrealized_pnl = round(
                (bar["close"] - cfg["slippage"] - self.position["entryPrice"])
                * cfg["multiplier"] * cfg["qty"] - cfg["feePerSide"] * cfg["qty"] * 2, 2)
        self.equity = round(self.cash + self.unrealized_pnl, 2)
        self.peak_equity = max(self.peak_equity, self.equity)
        if self.peak_equity > 0:
            drawdown = (self.peak_equity - self.equity) / self.peak_equity * 100
            self.max_drawdown_pct = max(self.max_drawdown_pct, drawdown)
        self.current_time = bar["timestamp"]
        # 带 i 的权益点便于回测按切分点分段，前端渲染时忽略该字段
        self.equity_curve.append({"timestamp": bar["timestamp"], "equity": self.equity, "i": index})
        if self.curve_limit and len(self.equity_curve) > self.curve_limit * 2:
            self.equity_curve = self.equity_curve[-self.curve_limit:]

    def _log_signal(self, timestamp: str, kind: str, reason: str) -> None:
        self.signals.append({"timestamp": timestamp, "type": kind, "reason": reason})
        if len(self.signals) > 1000:
            self.signals = self.signals[-500:]

    def _finish(self) -> None:
        self.status = "completed"
        self.last_message = "行情回放完成。"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# 指标统计（回测与诊断共用口径）
# --------------------------------------------------------------------------
def summarize_trades(trades: Iterable[dict], curve: Iterable[dict], starting_equity: float) -> dict:
    trades = list(trades)
    net = sum(t["netPnl"] for t in trades)
    winners = [t for t in trades if t["netPnl"] > 0]
    losers = [t for t in trades if t["netPnl"] < 0]
    win_amount = sum(t["netPnl"] for t in winners)
    loss_amount = abs(sum(t["netPnl"] for t in losers))
    peak, max_dd = starting_equity, 0.0
    for point in curve:
        peak = max(peak, point["equity"])
        if peak > 0:
            max_dd = max(max_dd, (peak - point["equity"]) / peak)
    return {
        "netPnl": round(net, 2),
        "returnPct": round(net / starting_equity * 100, 4) if starting_equity else 0,
        "maxDrawdownPct": round(max_dd * 100, 4),
        "tradeCount": len(trades),
        "winRatePct": round(len(winners) / len(trades) * 100, 2) if trades else 0,
        "profitFactor": round(win_amount / loss_amount, 4) if loss_amount else (None if not win_amount else "Infinity"),
    }
