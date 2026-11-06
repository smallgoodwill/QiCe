"""按品种取 K 线（按需拉取，不依赖采集器是否在跑）。

支持 9 个周期：`1m` / `5m` / `15m` / `30m` / `60m` / `4h` / `12h` / `1d` / `1w`，
数据来源按周期分派（见 `fetch()`），不是所有周期都从 1 分钟线重采样——
1 分钟线只有约 4 天，重采样出的 60 分钟只有十几根，图画不出来。

与 `feed.FeedHub` 的分工
------------------------
Hub 是「采集器在跑才有时序」：只服务当前订阅的那一个合约，逐 tick 聚合成 bar。
本模块反过来——任意品种随取随用，直接拉**主力连续**合约（`RB0` / `AU0`）的 K 线，
支撑仪表盘「跟随所选品种的实时 K 线流动」这个需求。

两个必须写进界面的前提
----------------------
1. 取的是主力连续，不是具体月份合约。换月跳空、远近月价差都会让价格与
   `rb2601` 这类合约对不上，所以返回里带 `continuous` 字段，前端要标注出来。
2. 分钟线在整分钟边界之后才更新最后一根，天然有几十秒延迟；它不是逐笔行情。
   SSE 实时流只推 1 分钟 bar，所以只有 1m 周期能跟采集器逐 tick 走，
   其余周期一律走本模块轮询。

实现上 `4h`/`12h` 复用 `feed._resample` 合并 60m 原生线，`1d`/`1w` 走
`history._daily_bars` 的真实日线（周线按自然周合并）。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from . import instruments
from .feed import TZ, _iso, _parse, _resample

logger = logging.getLogger(__name__)

# 12 秒：1 分钟 bar 最快也要 60 秒才换一根，这个间隔既能让最后一根跟着动，
# 又不至于每次刷新都把请求打到上游。前端轮询 10 秒，配合起来最多 22 秒滞后。
CACHE_TTL = 12.0

# 仪表盘 9 个周期。分钟周期用新浪原生 N 分钟线（每个窗口约 1023 根），
# 4h/12h 由 60m 原生线合并（1m 只有 4 天，重采样出的长周期只有十几根），
# 1d/1w 走主力连续日线（周线按自然周合并）。
MINUTE_WINDOW_PERIODS = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}
HOUR_PERIODS = {"4h": 240, "12h": 720}          # 值 = 合并窗口的分钟数
SUPPORTED_PERIODS = (*MINUTE_WINDOW_PERIODS, *HOUR_PERIODS, "1d", "1w")
PERIOD_LABELS = {"1m": "1 分钟", "5m": "5 分钟", "15m": "15 分钟", "30m": "30 分钟",
                 "60m": "60 分钟", "4h": "4 小时", "12h": "12 小时",
                 "1d": "1 天", "1w": "1 周"}

NOTICE = ("主力连续合约行情，与具体月份合约存在换月跳空/价差；"
          "分钟线在整分钟边界后更新，非逐笔行情。")
NOTICE_DAY = ("主力连续日线（交易日收盘），与具体月份合约存在换月跳空/价差；"
              "4 小时及以上周期为历史/延迟数据，非逐笔行情。")

_cache: dict[str, tuple[float, list[dict]]] = {}
# 按周期单独缓存「上游原生 N 分钟全量窗口」：1m 只有 4 天，60m 却有 9 个月，
# 从 1m 重采样会把长周期的历史白白砍掉，所以两条缓存不能混用。
_window_cache: dict[tuple[str, int], tuple[float, list[dict]]] = {}
WINDOW_TTL = 60.0
# 日线缓存按 (连续码, 取数跨度) 分桶；日线一天才变一次，缓存可以放久一点。
_day_cache: dict[tuple[str, int], tuple[float, list[dict]]] = {}
DAY_TTL = 300.0
_lock = threading.Lock()


class KlineError(RuntimeError):
    """取数失败（上游不可用 / 品种不支持）。"""


def continuous_code(symbol: str) -> tuple[str, str]:
    """`rb2601` / `螺纹钢` / `沪深300股指 · IF2503` → (`RB0`, `rb`)。

    新浪的分钟线接口认「品种字母 + 0」，且郑商所那几个必须大写（`SR0`），
    因此这里统一转大写，与 `macro` 里取日线的写法保持一致。
    """
    inst = instruments.guess(symbol)
    if inst:
        return f"{inst.symbol_prefix.upper()}0", inst.code
    # 认不出品种时退化成「取前两个字母」，至少让纯字母代码（如 IF0）还能用
    letters = "".join(ch for ch in str(symbol) if ch.isascii() and ch.isalpha()).upper()
    if not letters:
        raise KlineError(f"无法从「{symbol}」识别品种代码。")
    return f"{letters[:2]}0", letters[:2].lower()


def _request(continuous: str, period: str) -> object:
    """统一的上游请求：新浪认 `1` / `5` / `15` / `30` / `60` 这样的纯数字周期。"""
    import akshare as ak                       # 延迟导入：首次 import 要 1~2 秒

    try:
        return ak.futures_zh_minute_sina(symbol=continuous, period=period)
    except Exception as exc:                   # noqa: BLE001 - 上游什么错都可能抛
        raise KlineError(f"{continuous} 分钟线拉取失败：{type(exc).__name__}") from exc


def _to_bars(frame: object, continuous: str) -> list[dict]:
    """把上游 DataFrame 转成采集端同款 bar 结构。"""
    if frame is None or not len(frame):        # type: ignore[arg-type]
        raise KlineError(f"{continuous} 没有返回分钟线（可能已停牌或代码不被支持）。")

    bars: list[dict] = []
    for row in frame.to_dict("records"):
        stamp = row.get("datetime")
        if stamp is None:
            continue
        try:
            moment = stamp if isinstance(stamp, datetime) else datetime.fromisoformat(str(stamp))
            close = float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
        # 新浪给的是北京时间裸时间，补上时区后统一成 +08:00
        moment = moment.replace(tzinfo=TZ) if moment.tzinfo is None else moment
        bars.append({"timestamp": _iso(moment),
                     "open": float(row.get("open", close)),
                     "high": float(row.get("high", close)),
                     "low": float(row.get("low", close)),
                     "close": close,
                     "volume": float(row.get("volume") or 0)})
    if not bars:
        raise KlineError(f"{continuous} 返回的数据无法解析成 K 线。")
    bars.sort(key=lambda bar: bar["timestamp"])
    return bars


def _fetch_raw(continuous: str) -> list[dict]:
    """拉 1 分钟线（仪表盘 / 重采样用）。"""
    return _to_bars(_request(continuous, "1"), continuous)


def _raw_window(continuous: str, minutes: int, *, force: bool = False) -> list[dict]:
    """拉上游**原生 N 分钟**全量窗口（约 1023 根），带独立缓存。"""
    key = (continuous, minutes)
    with _lock:
        hit = _window_cache.get(key)
        if hit and not force and time.time() - hit[0] < WINDOW_TTL:
            return hit[1]
    bars = _to_bars(_request(continuous, str(minutes)), continuous)
    with _lock:
        _window_cache[key] = (time.time(), bars)
    return bars


def _raw_bars(continuous: str, *, force: bool = False) -> list[dict]:
    """带 12 秒缓存的 1 分钟原始序列（各周期共用同一份，避免重复打上游）。"""
    with _lock:
        hit = _cache.get(continuous)
        if hit and not force and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
    raw = _fetch_raw(continuous)
    with _lock:
        _cache[continuous] = (time.time(), raw)
    return raw


def _daily_window(continuous: str, *, force: bool = False) -> list[dict]:
    """拉最近约 8 年的真实日线（1d 直接用 / 1w 合并用），按 (连续码, 今天) 缓存。

    函数级导入 `history`：history 顶层 import 了本模块（取分钟窗口），
    顶层互相 import 会成环，放在这里两边都安全。
    """
    end = datetime.now(TZ).date()
    key = (continuous, end.isoformat())
    with _lock:
        hit = _day_cache.get(key)
        if hit and not force and time.time() - hit[0] < DAY_TTL:
            return hit[1]
    from . import history                     # 延迟导入：避免与 history 循环依赖

    start = end.replace(year=end.year - 8) if end.month != 2 or end.day != 29 else end.replace(year=end.year - 8, day=28)
    try:
        bars, _skipped = history._daily_bars(continuous, start, end)   # noqa: SLF001
    except Exception as exc:                  # noqa: BLE001 - HistoryError/上游异常都转成 KlineError
        raise KlineError(f"{continuous} 日线拉取失败：{exc}") from exc
    if not bars:
        raise KlineError(f"{continuous} 没有返回日线（可能已停牌或代码不被支持）。")
    with _lock:
        _day_cache[key] = (time.time(), bars)
    return bars


def _weekly(daily: list[dict]) -> list[dict]:
    """把日线按自然周（ISO 周）合并成周线：开=首根开、高/低取极值、收=末根收、量累加。

    时间戳取该周第一根日线的时间，保证与日线同轴且单调递增。
    """
    merged: list[dict] = []
    bucket_key, current = None, None
    for bar in daily:
        iso = _parse(bar["timestamp"]).isocalendar()[:2]      # (ISO 年, ISO 周)
        if current is None or iso != bucket_key:
            if current:
                merged.append(current)
            bucket_key = iso
            current = {"timestamp": bar["timestamp"], "open": bar["open"], "high": bar["high"],
                       "low": bar["low"], "close": bar["close"], "volume": float(bar["volume"])}
        else:
            current["high"] = max(current["high"], bar["high"])
            current["low"] = min(current["low"], bar["low"])
            current["close"] = bar["close"]
            current["volume"] = round(current["volume"] + float(bar["volume"]), 2)
    if current:
        merged.append(current)
    return merged


def _period_bars(continuous: str, period: str, *, force: bool = False) -> list[dict]:
    """按周期分派取数，返回**完整窗口**（不截断）。

    - `1m`：1 分钟原始序列（12 秒缓存，跟得上最后一根的跳动）
    - `5m`~`60m`：新浪**原生 N 分钟**全量窗口（约 1023 根；从 1m 重采样只会得到 4 天）
    - `4h` / `12h`：原生 60 分钟线按 240/720 分钟桶合并（新浪只有 1/5/15/30/60 五档）
    - `1d`：真实日线（`futures_main_sina`，约 8 年窗口）
    - `1w`：日线按自然周合并
    """
    if period in MINUTE_WINDOW_PERIODS:
        if period == "1m":
            return _raw_bars(continuous, force=force)
        return _raw_window(continuous, MINUTE_WINDOW_PERIODS[period], force=force)
    if period in HOUR_PERIODS:
        base = _raw_window(continuous, 60, force=force)
        return _resample(base, HOUR_PERIODS[period])
    daily = _daily_window(continuous, force=force)
    return daily if period == "1d" else _weekly(daily)


def _meta(symbol: str, code: str, continuous: str, period: str, count: int) -> dict:
    """返回体里的周期相关元信息：数据源口径 + 必须写进界面的提示。"""
    if period in HOUR_PERIODS:
        return {"source": "akshare · futures_zh_minute_sina（60 分钟合并）", "notice": NOTICE_DAY}
    if period in ("1d", "1w"):
        extra = "；周线由日线按自然周合并。" if period == "1w" else ""
        return {"source": "akshare · futures_main_sina（真实日线）", "notice": NOTICE_DAY + extra}
    return {"source": "akshare · futures_zh_minute_sina", "notice": NOTICE}


def fetch(symbol: str, *, period: str = "1m", limit: int = 320, force: bool = False) -> dict:
    """取某个品种最近的 K 线（9 个周期，按周期分派真实数据源）。

    :param symbol: 任意能识别品种的写法（`rb2601`、`AU`、`螺纹钢`）
    :param period: `1m` / `5m` / `15m` / `30m` / `60m` / `4h` / `12h` / `1d` / `1w`
    :param limit:  返回的 bar 根数上限（取最近的 limit 根）
    :param force:  跳过缓存强制刷新
    """
    if period not in SUPPORTED_PERIODS:
        raise KlineError(f"不支持的周期「{period}」，可选：{'、'.join(PERIOD_LABELS[p] for p in SUPPORTED_PERIODS)}。")
    limit = max(30, min(int(limit), 900))
    continuous, code = continuous_code(symbol)
    bars = _period_bars(continuous, period, force=force)[-limit:]
    if not bars:
        raise KlineError(f"{continuous} 没有可用的 {PERIOD_LABELS[period]} K 线。")

    return {
        "symbol": symbol,
        "instrument": code,
        "continuous": continuous,
        "period": period,
        "periodLabel": PERIOD_LABELS[period],
        "count": len(bars),
        "bars": bars,
        "fetchedAt": _iso(datetime.now(TZ)),
        **_meta(symbol, code, continuous, period, len(bars)),
    }


def fetch_window(symbol: str, *, period: str = "1m", force: bool = False) -> dict:
    """取上游**可得的全部** K 线，不按 limit 截断。

    仪表盘只要最近几百根，按时间区间回测却必须知道最早的 bar 在哪一天，
    否则「我要回测去年 10 月」会被静默换成最近 4 天。这里把完整窗口交出去，
    由 `history.py` 做时间过滤与超出范围提示。
    """
    if period not in SUPPORTED_PERIODS:
        raise KlineError(f"不支持的周期「{period}」，可选：{'、'.join(PERIOD_LABELS[p] for p in SUPPORTED_PERIODS)}。")
    continuous, code = continuous_code(symbol)
    bars = _period_bars(continuous, period, force=force)
    if not bars:
        raise KlineError(f"{continuous} 没有可用的 {PERIOD_LABELS[period]} K 线。")

    return {
        "symbol": symbol,
        "instrument": code,
        "continuous": continuous,
        "period": period,
        "periodLabel": PERIOD_LABELS[period],
        "count": len(bars),
        "bars": bars,
        "availableFrom": bars[0]["timestamp"][:10],
        "availableTo": bars[-1]["timestamp"][:10],
        "fetchedAt": _iso(datetime.now(TZ)),
        **_meta(symbol, code, continuous, period, len(bars)),
    }


def clear_cache() -> None:
    """测试用：清掉进程内缓存。"""
    with _lock:
        _cache.clear()
        _window_cache.clear()
        _day_cache.clear()
