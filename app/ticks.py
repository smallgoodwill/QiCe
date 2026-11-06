"""秒级实时 tick：让主页 K 线按秒跟随最新价跳动。

和 `klines.py` 的分工
--------------------
`klines` 给的是**已收盘 bar 的窗口**（1 分钟线整分钟才更新一次，天然滞后几十秒）；
本模块给的是**当前这一秒的最新价**（新浪实时快照，约 0.25~0.5 秒可达）。
主页「实时 K 线」要的是后者：把 tick 合成进正在形成的那根 bar，每秒推一次。

口径（必须和 klines/history 的桶对齐，否则 update 会追加出重复 bar）
------------------------------------------------------------------
- 分钟/小时周期：桶起点 = `_floor(now, minutes)`，与 `feed._resample` 同一取整；
- `1d`：timestamp 用当日 `T15:00:00+08:00`，与 `history._daily_bars` 的日线口径一致；
- `1w`：若本周已有周线 bar 就沿用它的 timestamp（= 该周第一根日线），
  否则近似用本周一 `T15:00:00+08:00`，下一次全量刷新会被真实周线覆盖。

上游
----
`ak.futures_zh_spot(symbol='RB0', market='CF'|'FF')`：
- 认**大写连续合约码**（`RB0` / `SR0` / `IF0`），不用品种中文名映射；
- 中金所（股指/国债）必须 `market='FF'`，其余交易所 `market='CF'`；
- 返回当日开高低、最新价、当日累计成交量、持仓与行情时间。

成交量口径
----------
正在形成 bar 的 volume = 基底 bar 的 volume（上游窗口已统计的部分）
+ 自监听起点起 tick 累计量之差（每秒增量累加）。两者统计区间可能重叠几秒，
视觉上可接受；下一根 bar 收盘后由 klines 窗口回填为准确值。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

from . import instruments, klines
from .feed import TZ, _floor, _iso, _parse

# 上游实测约 0.25~0.5 秒；两个客户端共享一份缓存，避免按客户端数翻倍打上游
TICK_TTL = 0.8

NOTICE = ("实时快照约 1 秒 1 次，非逐笔行情；最后一根为实时合成 bar"
          "（成交量为监听起点起的增量 + 上游窗口量），收盘后由真实 K 线回填。")

_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


class TickError(RuntimeError):
    """实时快照取不到（上游不可用 / 品种不支持）。"""


def snapshot(symbol: str) -> dict:
    """取某个品种（主力连续）的实时快照。

    :param symbol: 任意能识别品种的写法（`rb2601`、`AU`、`螺纹钢`）
    :returns: `{price, open, high, low, dayVolume, hold, tickTime, tradedate, ...}`
    """
    continuous, code = klines.continuous_code(symbol)
    key = continuous.upper()
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < TICK_TTL:
            return hit[1]

    inst = instruments.get(code)
    market = "FF" if inst and inst.exchange == "CFFEX" else "CF"
    import akshare as ak                       # 延迟导入：首次 import 要 1~2 秒

    try:
        frame = ak.futures_zh_spot(symbol=key, market=market)
    except Exception as exc:                   # noqa: BLE001 - 上游什么错都可能抛
        raise TickError(f"{key} 实时快照拉取失败：{type(exc).__name__}") from exc
    if frame is None or not len(frame):
        raise TickError(f"{key} 没有返回实时快照（可能已停牌或代码不被支持）。")

    row = frame.iloc[0].to_dict()

    def _num(name: str) -> float:
        try:
            value = float(row.get(name))
            return value if value == value else 0.0       # NaN 防御
        except (TypeError, ValueError):
            return 0.0

    price = _num("current_price")
    if not price:
        raise TickError(f"{key} 实时价为空（可能不在交易时段）。")

    # 行情时间两种格式都出现过：`11:30:00` 与 `113000`
    raw_time = str(row.get("time") or "").strip()
    if raw_time.isdigit() and len(raw_time) == 6:
        tick_time = f"{raw_time[:2]}:{raw_time[2:4]}:{raw_time[4:]}"
    else:
        tick_time = raw_time

    data = {
        "symbol": symbol,
        "instrument": code,
        "continuous": key,
        "price": price,
        "open": _num("open"),
        "high": _num("high"),
        "low": _num("low"),
        "dayVolume": _num("volume"),
        "hold": _num("hold"),
        "tickTime": tick_time,
        "fetchedAt": _iso(datetime.now(TZ)),
        "source": "akshare · futures_zh_spot",
    }
    with _lock:
        _cache[key] = (time.time(), data)
    return data


def bucket_start(period: str, now: datetime | None = None) -> str:
    """当前周期「正在形成的那根 bar」的 timestamp（与 klines/history 的桶口径一致）。"""
    now = now or datetime.now(TZ)
    if period in klines.MINUTE_WINDOW_PERIODS:
        return _iso(_floor(now, klines.MINUTE_WINDOW_PERIODS[period]))
    if period in klines.HOUR_PERIODS:
        return _iso(_floor(now, klines.HOUR_PERIODS[period]))
    if period == "1d":
        return f"{now.date().isoformat()}T15:00:00+08:00"
    if period == "1w":
        monday = now.date().fromisocalendar(*now.isocalendar()[:2], 1)
        return f"{monday.isoformat()}T15:00:00+08:00"
    raise klines.KlineError(f"不支持的周期「{period}」。")


def same_bucket(bar_ts: str, period: str, now: datetime | None = None) -> bool:
    """判断已有 bar 的 timestamp 是否落在「当前正在形成的桶」里。"""
    now = now or datetime.now(TZ)
    moment = _parse(bar_ts)
    if period == "1d":
        return moment.date() == now.date()
    if period == "1w":
        return moment.isocalendar()[:2] == now.isocalendar()[:2]
    if period in klines.MINUTE_WINDOW_PERIODS:
        minutes = klines.MINUTE_WINDOW_PERIODS[period]
    elif period in klines.HOUR_PERIODS:
        minutes = klines.HOUR_PERIODS[period]
    else:
        return False
    return _floor(moment, minutes) == _floor(now, minutes)


class FormingBar:
    """正在形成的那根 bar：以窗口 bar 为基底，逐秒并入 tick。

    一个 SSE 连接持有一个实例；`feed()` 每秒调用一次，返回可直接推送的 bar。
    """

    def __init__(self, symbol: str, period: str, base: dict | None):
        self.symbol = symbol
        self.period = period
        self.bar: dict | None = dict(base) if base else None
        self._prev_day_vol: float | None = None   # 上一 tick 的当日累计量，算每秒增量用

    def feed(self, tick: dict) -> dict:
        price = tick["price"]
        now = datetime.now(TZ)
        day_vol = tick.get("dayVolume") or 0.0

        # 每秒成交量增量：当日累计量之差；跨日/复位时归零
        delta = 0.0
        if self._prev_day_vol is not None and day_vol >= self._prev_day_vol:
            delta = day_vol - self._prev_day_vol
        self._prev_day_vol = day_vol

        if self.bar is None or not same_bucket(self.bar["timestamp"], self.period, now):
            self.bar = {"timestamp": bucket_start(self.period, now),
                        "open": price, "high": price, "low": price,
                        "close": price, "volume": round(delta, 2)}
        else:
            self.bar["high"] = max(float(self.bar["high"]), price)
            self.bar["low"] = min(float(self.bar["low"]), price)
            self.bar["close"] = price
            self.bar["volume"] = round(float(self.bar["volume"]) + delta, 2)
        return dict(self.bar)


def forming(symbol: str, period: str, base: dict | None = None) -> dict:
    """无状态版：取一次 tick 合成一版形成中 bar（供一次性接口/测试用）。

    没有增量体积状态，volume 只保留基底值。
    """
    tick = snapshot(symbol)
    bar = FormingBar(symbol, period, base)
    bar._prev_day_vol = tick.get("dayVolume")        # noqa: SLF001 - 首 tick 不计增量
    out = bar.feed(tick)
    out["tickTime"] = tick["tickTime"]
    out["price"] = tick["price"]
    return out
