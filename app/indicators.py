"""技术指标纯函数。

约定：输出与输入等长，未预热完成的位置为 None。
这样调用方不必关心预热偏移，也不会出现"用未来数据算指标"的回绕。
"""
from __future__ import annotations

from typing import Sequence

Number = float


def sma(values: Sequence[float], period: int) -> list[float | None]:
    """简单移动平均（滑动窗口，O(n)）。"""
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    window = 0.0
    for i, value in enumerate(values):
        window += value
        if i >= period:
            window -= values[i - period]
        if i >= period - 1:
            out[i] = window / period
    return out


def ema(values: Sequence[float], period: int) -> list[float | None]:
    """指数移动平均：前 period 个值用 SMA 做种子，之后递推。"""
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    alpha = 2 / (period + 1)
    for i in range(period, len(values)):
        seed = values[i] * alpha + seed * (1 - alpha)
        out[i] = seed
    return out


def moving(values: Sequence[float], period: int, kind: str) -> list[float | None]:
    return sma(values, period) if kind == "SMA" else ema(values, period)


def true_range(bars: Sequence[dict]) -> list[float]:
    out: list[float] = []
    for i, bar in enumerate(bars):
        prev_close = bars[i - 1]["close"] if i else bar["close"]
        out.append(max(bar["high"] - bar["low"],
                       abs(bar["high"] - prev_close),
                       abs(bar["low"] - prev_close)))
    return out


def atr(bars: Sequence[dict], period: int) -> list[float | None]:
    """ATR 用真实波幅的简单平均（与演示脚本保持同一口径）。"""
    tr = true_range(bars)
    out: list[float | None] = [None] * len(tr)
    if period <= 0 or len(tr) < period:
        return out
    window = sum(tr[:period])
    out[period - 1] = window / period
    for i in range(period, len(tr)):
        window += tr[i] - tr[i - period]
        out[i] = window / period
    return out


def rsi(values: Sequence[float], period: int) -> list[float | None]:
    """Wilder 平滑 RSI。"""
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains += max(0.0, change)
        losses += max(0.0, -change)
    gain, loss = gains / period, losses / period
    out[period] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        gain = (gain * (period - 1) + max(0.0, change)) / period
        loss = (loss * (period - 1) + max(0.0, -change)) / period
        out[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    return out
