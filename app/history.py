"""按时间区间取**真实历史行情**，供策略回测使用。

为什么要单独一个模块
----------------------
回测页以前要求用户先导入 CSV，页面里还有一份合成样例行情。两者都不是真行情：
CSV 要用户自己找授权数据，合成样例只是跑通链路。现在改成「选品种 + 选起止时间
+ 选周期」，由服务端去公开行情源拉那段真实历史，再喂给同一条撮合引擎。

数据源与它各自的边界（必须对用户说清楚，不能悄悄换数据）
--------------------------------------------------------
1. 日线 `1d`：`akshare.futures_main_sina(symbol="RB0", start_date, end_date)`
   支持任意历史日期区间，返回的是**主力连续**日线，不是具体月份合约。
   想回测一整年、三年、十年，用这一档。
2. 分钟 `60m / 30m / 15m / 5m / 1m`：`akshare.futures_zh_minute_sina`
   上游固定只给最近约 1023 根（1m ≈ 4 个交易日，60m ≈ 9 个月），
   **不接受起始日期参数**。所以这里按时间过滤后，如果请求区间早于可得窗口，
   会直接报错并回报可用起点，绝不偷偷把「去年 10 月」换成「最近 4 天」。

所有 bar 都是北京时间 +08:00 的 ISO 字符串，日线用当日 15:00 作为时间戳
（交易日的近似收盘时刻，商品夜盘归属以上游日期为准）。行情源不可用时抛
`HistoryError`，由 API 层翻译成可读的 502，不退化成合成数据兜底。
"""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import date, datetime

from . import config, instruments, klines
from .feed import TZ

logger = logging.getLogger(__name__)

# 日线走 futures_main_sina；分钟线走 futures_zh_minute_sina，period 要传纯数字字符串
MINUTE_PERIODS = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60"}
PERIOD_LABELS = {"1d": "日线", "60m": "60 分钟", "30m": "30 分钟",
                 "15m": "15 分钟", "5m": "5 分钟", "1m": "1 分钟"}
SUPPORTED_PERIODS = ("1d", "60m", "30m", "15m", "5m", "1m")

DAILY_SOURCE = "新浪财经 · 主力连续日线（akshare futures_main_sina）"
MINUTE_SOURCE = "新浪财经 · 主力连续分钟线（akshare futures_zh_minute_sina）"
DAILY_NOTICE = ("真实历史行情回放：主力连续日线，与具体月份合约存在换月跳空/价差；"
                "不含保证金、涨跌停、手续费明细与真实撮合规则，也不发送任何真实委托。")
MINUTE_NOTICE = ("真实历史行情回放：主力连续分钟线，上游只提供最近约 1023 根，"
                 "与具体月份合约存在换月跳空/价差；非逐笔行情，不含真实撮合规则。")

# 同一区间 5 分钟内不重复打上游（前端改日期会频繁探测可用范围）
CACHE_TTL = 300.0
MAX_DAYS = 3650                    # 日线最长一次取 10 年，避免上游被拉爆
_cache: dict[tuple, tuple[float, dict]] = {}
_lock = threading.Lock()


class HistoryError(RuntimeError):
    """区间/品种不合法（400）或行情源不可用（502）。

    `extra` 里带可用窗口，前端的「按可用区间回测」按钮靠它一键修正日期。
    """

    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


def _parse_date(value: object, label: str) -> date:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise HistoryError(f"{label}必须是 YYYY-MM-DD 格式的日期。") from None


def _cache_get(key: tuple) -> dict | None:
    with _lock:
        hit = _cache.get(key)
        return hit[1] if hit and time.time() - hit[0] < CACHE_TTL else None


def _cache_put(key: tuple, payload: dict) -> None:
    with _lock:
        _cache[key] = (time.time(), payload)
        # 只留最近 24 份，避免长时间运行堆内存
        if len(_cache) > 24:
            for old in sorted(_cache, key=lambda k: _cache[k][0])[:-24]:
                _cache.pop(old, None)


def _daily_bars(continuous: str, start: date, end: date) -> tuple[list[dict], int]:
    """拉日线并转成引擎认识的 bar；返回 (bars, 跳过的异常行数)。"""
    import akshare as ak                      # 延迟导入：首次 import 要 1~2 秒

    try:
        frame = ak.futures_main_sina(symbol=continuous,
                                     start_date=start.strftime("%Y%m%d"),
                                     end_date=end.strftime("%Y%m%d"))
    except Exception as exc:                  # noqa: BLE001 - 上游什么错都可能抛
        raise HistoryError(f"{continuous} 历史日线拉取失败：{type(exc).__name__}", 502) from exc
    if frame is None or not len(frame):
        raise HistoryError(f"{continuous} 在所选区间没有返回日线，请检查日期或换一个品种。", 502)

    bars: list[dict] = []
    skipped = 0
    for row in frame.to_dict("records"):
        try:
            day = str(row["日期"])[:10]
            date.fromisoformat(day)          # 顺手校验，防止上游混入表头/汇总行
            o, h, l, c = (float(row[key]) for key in ("开盘价", "最高价", "最低价", "收盘价"))
            volume = float(row.get("成交量") or 0)
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        if not all(math.isfinite(v) for v in (o, h, l, c, volume)) or l > min(o, c) or h < max(o, c):
            skipped += 1                     # 主力连续换月偶尔会有自相矛盾的 OHLC
            continue
        bars.append({"timestamp": f"{day}T15:00:00+08:00", "open": o, "high": h,
                     "low": l, "close": c, "volume": max(0.0, volume)})
    bars.sort(key=lambda bar: bar["timestamp"])
    return bars, skipped


def _trim(bars: list[dict], start: date, end: date) -> list[dict]:
    """ISO 时间戳都是 +08:00、固定宽度，字典序比较等价于时间比较。"""
    lo = f"{start.isoformat()}T00:00:00+08:00"
    hi = f"{end.isoformat()}T23:59:59+08:00"
    return [bar for bar in bars if lo <= bar["timestamp"] <= hi]


def _resolve(symbol: object) -> tuple[instruments.Instrument, str]:
    text = str(symbol or "").strip()
    if not text:
        raise HistoryError("请先选择回测品种。")
    inst = instruments.guess(text)
    if not inst:
        raise HistoryError(f"无法从「{text}」识别品种，请从品种选择器里选一个。")
    continuous, _ = klines.continuous_code(text)
    return inst, continuous


def fetch(symbol: object, start: object, end: object, period: str = "1d", *,
          force: bool = False) -> dict:
    """按 [start, end] 取真实历史 K 线。

    返回体里除 `bars` 外还带 `source` / `coverage` / `notice`，
    回测结果会原样落库，这样以后翻旧记录能知道当时用的是哪段真行情。
    """
    period = str(period or "1d").lower()
    if period not in SUPPORTED_PERIODS:
        raise HistoryError(f"不支持的周期「{period}」，可选：{'、'.join(SUPPORTED_PERIODS)}。")
    inst, continuous = _resolve(symbol)
    start_day, end_day = _parse_date(start, "开始日期"), _parse_date(end, "结束日期")
    today = datetime.now(TZ).date()
    if end_day > today:
        end_day = today                      # 用户选到未来，收到今天即可
    if start_day > end_day:
        raise HistoryError("开始日期不能晚于结束日期。")
    if (end_day - start_day).days > MAX_DAYS:
        raise HistoryError(f"单次回测区间不能超过 {MAX_DAYS // 365} 年，请缩短日期。")

    key = (continuous, period, start_day.isoformat(), end_day.isoformat())
    if not force:
        cached = _cache_get(key)
        if cached:
            return cached

    warnings: list[str] = []
    if period == "1d":
        all_bars, skipped = _daily_bars(continuous, start_day, end_day)
        if skipped:
            warnings.append(f"上游有 {skipped} 根 OHLC 不合法的日线已跳过。")
        bars = _trim(all_bars, start_day, end_day)
        source, notice = DAILY_SOURCE, DAILY_NOTICE
        available_from = all_bars[0]["timestamp"][:10] if all_bars else ""
        available_to = all_bars[-1]["timestamp"][:10] if all_bars else ""
        if not bars:
            raise HistoryError("该区间没有交易日数据，请把区间放宽或换一个上市更久的品种。", 502)
    else:
        try:
            window = klines.fetch_window(continuous, period=period, force=force)
        except klines.KlineError as exc:
            raise HistoryError(str(exc), 502) from None
        all_bars = window["bars"]
        available_from, available_to = window["availableFrom"], window["availableTo"]
        window_info = {"availableFrom": available_from, "availableTo": available_to,
                       "period": period, "windowBars": len(all_bars)}
        if end_day.isoformat() < available_from:
            raise HistoryError(
                f"{PERIOD_LABELS[period]}真实行情最早只到 {available_from}（上游仅提供最近 {len(all_bars)} 根）。"
                "请把区间移到该日期之后，或改用日线回测更早的历史。", 502, **window_info)
        if start_day.isoformat() < available_from:
            raise HistoryError(
                f"{PERIOD_LABELS[period]}真实行情最早只到 {available_from}，无法覆盖你选的 {start_day}。"
                f" 可点「使用可用区间」，或改用日线（支持任意历史日期）。", 502, **window_info)
        bars = _trim(all_bars, start_day, end_day)
        source, notice = MINUTE_SOURCE, MINUTE_NOTICE
        if end_day.isoformat() > available_to:
            warnings.append(f"结束日期超过最新行情 {available_to}，已按最新可得数据回测。")

    if len(bars) < config.MIN_BARS:
        raise HistoryError(
            f"该区间只有 {len(bars)} 根 {PERIOD_LABELS[period]} K 线，至少需要 {config.MIN_BARS} 根。"
            "请延长区间、改用日线，或换更小的周期。", 502,
            availableFrom=available_from, availableTo=available_to, period=period)
    if period == "1d" and len(bars) < 60:
        warnings.append("区间内交易日不足 60 根，训练/测试切分后的统计意义有限。")

    payload = {
        "symbol": str(symbol).strip(),
        "instrument": inst.to_dict(),
        "continuous": continuous,
        "period": period,
        "periodLabel": PERIOD_LABELS[period],
        "start": start_day.isoformat(),
        "end": end_day.isoformat(),
        "startTime": bars[0]["timestamp"],
        "endTime": bars[-1]["timestamp"],
        "count": len(bars),
        "source": source,
        "sourceKind": "real-history",
        "notice": notice,
        "availableFrom": available_from,
        "availableTo": available_to,
        "coverage": {"requestedStart": start_day.isoformat(),
                     "requestedEnd": end_day.isoformat(),
                     "availableFrom": available_from, "availableTo": available_to,
                     "bars": len(bars), "windowBars": len(all_bars)},
        "warnings": warnings,
        "fetchedAt": datetime.now(TZ).isoformat(timespec="seconds"),
        "bars": bars,
    }
    _cache_put(key, payload)
    return payload


def probe(symbol: object, start: object, end: object, period: str = "1d", *,
          force: bool = False) -> dict:
    """只报「这段区间能不能跑、有多少根」，不把 bars 传回前端。

    回测页改动日期/周期时用它做即时提示；网络或区间出问题时返回 `ok=False`
    和可读原因（HTTP 仍是 200），避免页面上弹一堆红色错误。
    """
    try:
        data = fetch(symbol, start, end, period, force=force)
    except HistoryError as exc:
        return {"ok": False, "reason": str(exc), "period": str(period or "1d").lower(),
                "availableFrom": "", "availableTo": "", "count": 0,
                "source": MINUTE_SOURCE if str(period or "1d").lower() in MINUTE_PERIODS else DAILY_SOURCE,
                **exc.extra}
    return {key: value for key, value in data.items() if key != "bars"} | {"ok": True}


def clear_cache() -> None:
    """测试用：清掉进程内缓存。"""
    with _lock:
        _cache.clear()
