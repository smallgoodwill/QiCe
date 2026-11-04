"""实时行情：tick → 分钟 K 线聚合 + 订阅推送（只读行情，不下单）。

为什么单独一层
--------------
CSV 导入是「一次性快照 + 回放」，看盘需要的是「持续更新的 bar」。这里把两件事
解耦：采集、聚合、推送全在本模块，`engine.py` 的回放引擎一行不用改。输出结构沿用
`{timestamp, open, high, low, close, volume}`，所以 `indicators.py` 里的 SMA/ATR/RSI
可以直接复用。

两种数据源（driver）
--------------------
    sim  内置随机漫步模拟源。没有 SimNow 账号时用它把「采集 → 聚合 → 推送 → 画图」
         整条链路跑通；`speedup` 把 1 分钟压缩成几秒，界面上明确标注「模拟数据」。
    ctp  SimNow / CTP 真实行情，走 vn.py 网关的 tick 回调（`simnow.py` 已加监听点）。

并发模型
--------
1 个采集线程 → 每个合约 1 个 Aggregator → 每次 bar 变化广播给 N 个 SSE 队列。
队列有界，满了丢最旧的一条，慢客户端不会把采集线程拖死。
"""
from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from queue import Full, Queue

from . import db, simnow

logger = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=8))       # 国内期货一律按北京时间切 bar
MAX_BARS = 3000                          # 单合约内存里保留的 1m bar 上限
MAX_SUBSCRIBERS = 8                      # SSE 并发连接上限（本地单用户够用）
STREAM_QUEUE_SIZE = 200
PERIODS = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}

# 模拟源的基准价，只为了让随机行走的量级看起来合理（不是真实报价）
BASE_PRICES = {"IF": 3900.0, "IH": 2700.0, "IC": 5800.0, "IM": 6100.0,
               "RB": 3400.0, "HC": 3500.0, "I": 800.0, "J": 2000.0, "JM": 1400.0,
               "AU": 620.0, "AG": 7800.0, "CU": 78000.0, "AL": 20500.0,
               "SC": 520.0, "MA": 2500.0, "TA": 5800.0, "M": 3000.0, "Y": 7900.0}


def _iso(dt: datetime) -> str:
    return dt.astimezone(TZ).isoformat(timespec="seconds")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _floor(dt: datetime, minutes: int) -> datetime:
    """把时间向下取整到 minutes 分钟窗口的起点。"""
    dt = dt.astimezone(TZ)
    total = (dt.hour * 60 + dt.minute) // minutes * minutes
    return dt.replace(hour=total // 60, minute=total % 60, second=0, microsecond=0)


def _public(bar: dict) -> dict:
    """去掉内部字段（_window）后的对外结构。"""
    return {k: v for k, v in bar.items() if not k.startswith("_")}


def contract_root(symbol: str) -> str:
    """从 `IF2503` / `原油 · SC2503` 里取出品种字母（IF / SC）。"""
    letters = "".join(ch for ch in str(symbol) if ch.isascii() and ch.isalpha()).upper()
    return letters[:2] if letters else "IF"


def _resample(bars: list[dict], minutes: int) -> list[dict]:
    """把 1m bar 合并成 N 分钟 bar：开高低收按窗口取，成交量累加。"""
    merged: list[dict] = []
    bucket_key, current = None, None
    for bar in bars:
        key = _iso(_floor(_parse(bar["timestamp"]), minutes))
        if current is None or key != bucket_key:
            if current:
                merged.append(current)
            bucket_key = key
            current = {"timestamp": key, "open": bar["open"], "high": bar["high"],
                       "low": bar["low"], "close": bar["close"], "volume": bar["volume"]}
        else:
            current["high"] = max(current["high"], bar["high"])
            current["low"] = min(current["low"], bar["low"])
            current["close"] = bar["close"]
            current["volume"] = round(current["volume"] + bar["volume"], 2)
    if current:
        merged.append(current)
    return merged


class Aggregator:
    """单个合约的 tick → 1m bar 聚合器。

    不变量：`self.bars` 只放**已收口**的 bar；`self.current` 是正在形成的那根，
    快照时追加在末尾，所以图表最右边那根会随 tick 跳动。
    """

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.bars: deque[dict] = deque(maxlen=MAX_BARS)
        self.current: dict | None = None
        self.last_price: float | None = None
        self.last_tick_at: str = ""
        self.tick_count = 0

    def seed(self, bars: list[dict]) -> None:
        for bar in bars:
            self.bars.append({k: bar[k] for k in
                              ("timestamp", "open", "high", "low", "close", "volume")})

    def push(self, price: float, volume_delta: float, ts: datetime) -> tuple[dict | None, dict]:
        """喂一个 tick，返回 (刚收口的 bar 或 None, 当前 bar 快照)。"""
        price = round(float(price), 2)
        if price <= 0:
            return None, {}
        window = _floor(ts, 1)
        closed = None
        current = self.current
        if current is None or current["_window"] != window.isoformat():
            if current is not None:
                closed = _public(current)
                self.bars.append(closed)
            current = {"_window": window.isoformat(), "timestamp": _iso(window),
                       "open": price, "high": price, "low": price, "close": price,
                       "volume": 0.0}
            self.current = current
        current["high"] = max(current["high"], price)
        current["low"] = min(current["low"], price)
        current["close"] = price
        current["volume"] = round(current["volume"] + max(0.0, float(volume_delta or 0)), 2)
        self.last_price = price
        self.last_tick_at = _iso(ts)
        self.tick_count += 1
        return closed, _public(current)

    def snapshot(self, limit: int = 500) -> list[dict]:
        bars = list(self.bars)[-limit:]
        if self.current:
            bars.append(_public(self.current))
        return bars


class FeedHub:
    """行情总控：驱动启停、按周期取数、向 SSE 订阅者广播。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.aggs: dict[str, Aggregator] = {}
        self.subscribers: set[Queue] = set()
        self.driver: str | None = None
        self.running = False
        self.symbol = ""
        self.exchange = ""
        self.speedup = 1
        self.started_at = ""
        self.message = "未启动"
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_ctp_volume: dict[str, float] = {}

    # ---- 状态 ------------------------------------------------------------
    def status(self) -> dict:
        with self.lock:
            symbols = []
            for symbol, agg in self.aggs.items():
                tail = agg.snapshot(1)
                symbols.append({"symbol": symbol, "bars": len(agg.bars),
                                "tickCount": agg.tick_count, "lastPrice": agg.last_price,
                                "lastTime": agg.last_tick_at,
                                "lastBar": tail[-1] if tail else None})
            return {"driver": self.driver, "running": self.running,
                    "symbol": self.symbol, "exchange": self.exchange,
                    "speedup": self.speedup, "startedAt": self.started_at,
                    "message": self.message, "subscribers": len(self.subscribers),
                    "symbols": symbols,
                    "ctp": {"available": simnow.simnow.available(),
                            "connected": simnow.simnow.connected,
                            "credentialsConfigured": simnow.simnow.status()["credentialsConfigured"]},
                    "notice": ("模拟源数据由随机数生成，只用于验证界面与链路，"
                               "不可用于任何交易决策。") if self.driver == "sim" else ""}

    # ---- 取数 ------------------------------------------------------------
    def bars(self, symbol: str = "", period: str = "1m", limit: int = 500) -> list[dict]:
        minutes = PERIODS.get(str(period).lower(), 1)
        limit = max(1, min(3000, int(limit or 500)))
        with self.lock:
            symbol = (symbol or self.symbol or "").strip() or self.symbol
            agg = self.aggs.get(symbol)
            raw = agg.snapshot(limit * minutes + 2) if agg else db.recent_live_bars(symbol, limit * minutes + 2)
        if not raw:
            return []
        return (raw if minutes == 1 else _resample(raw, minutes))[-limit:]

    # ---- 订阅 ------------------------------------------------------------
    def open_stream(self) -> Queue:
        queue: Queue = Queue(maxsize=STREAM_QUEUE_SIZE)
        with self.lock:
            if len(self.subscribers) >= MAX_SUBSCRIBERS:
                raise ValueError("实时推送连接过多，请先关闭其他页面或标签。")
            self.subscribers.add(queue)
        return queue

    def close_stream(self, queue: Queue) -> None:
        with self.lock:
            self.subscribers.discard(queue)

    def _publish(self, event: dict) -> None:
        with self.lock:
            targets = list(self.subscribers)
        for queue in targets:
            try:
                queue.put_nowait(event)
            except Full:                      # 慢客户端：丢最旧一条，绝不阻塞采集
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except Exception:
                    pass

    # ---- 驱动：内置模拟源 -------------------------------------------------
    def start_sim(self, symbol: str, base_price: float | None = None,
                  speedup: int = 20, seed_bars: int = 300) -> dict:
        symbol = (symbol or "").strip() or "IF2503"
        speedup = max(1, min(120, int(speedup or 20)))
        price = float(base_price or BASE_PRICES.get(contract_root(symbol), 3000.0))
        history = self._make_history(price, max(60, min(1000, int(seed_bars))))
        with self.lock:
            self._stop_locked()
            agg = Aggregator(symbol)
            agg.seed(history)
            self.aggs = {symbol: agg}
            self.driver, self.running = "sim", True
            self.symbol, self.exchange = symbol, "SIM"
            self.speedup = speedup
            self.started_at = _iso(datetime.now(TZ))
            self.message = f"内置模拟源运行中（{speedup}× 加速，数据非真实行情）"
            self._stop.clear()
            self._thread = threading.Thread(target=self._sim_loop, args=(symbol, price, speedup),
                                            name="qice-sim-feed", daemon=True)
            self._thread.start()
        for bar in history:                    # 历史底图落库，重启后还能看到
            self._persist(symbol, bar)
        logger.info("模拟行情源启动：%s 基准价 %.2f，加速 %d×，%d 根历史 bar",
                    symbol, price, speedup, len(history))
        return self.status()

    @staticmethod
    def _make_history(price: float, count: int) -> list[dict]:
        """生成到「当前整分钟」为止的 count 根 1m bar（含随机波动与影线）。"""
        now = _floor(datetime.now(TZ), 1)
        rows: list[dict] = []
        close = price
        for i in range(count, 0, -1):
            open_ = round(close, 2)
            close = round(max(0.01, close + random.gauss(0, price * 0.0006)), 2)
            high = round(max(open_, close) + abs(random.gauss(0, price * 0.0003)), 2)
            low = round(min(open_, close) - abs(random.gauss(0, price * 0.0003)), 2)
            rows.append({"timestamp": _iso(now - timedelta(minutes=i)), "open": open_,
                         "high": high, "low": low, "close": close,
                         "volume": float(random.randint(200, 2000))})
        return rows

    def _sim_loop(self, symbol: str, start_price: float, speedup: int) -> None:
        price = start_price
        sim_now = _floor(datetime.now(TZ), 1) + timedelta(seconds=1)
        tick_gap = max(0.15, 60.0 / speedup / 8)      # 每根 bar 至少 8 个 tick
        while not self._stop.is_set():
            sim_now += timedelta(seconds=tick_gap * speedup)
            price = round(max(0.01, price + random.gauss(0, price * 0.0007)), 2)
            with self.lock:
                agg = self.aggs.get(symbol)
                if agg is None or not self.running:
                    return
                closed, current = agg.push(price, random.randint(1, 30), sim_now)
                if closed:
                    self._persist(symbol, closed)
                self._publish({"type": "bar", "symbol": symbol, "bar": current,
                               "closed": bool(closed)})
            time.sleep(tick_gap)

    # ---- 驱动：CTP 真实行情 ----------------------------------------------
    def start_ctp(self, symbol: str, exchange: str) -> dict:
        symbol = (symbol or "").strip()
        exchange = (exchange or "CFFEX").strip().upper()
        manager = simnow.simnow
        if not symbol:
            raise ValueError("请填写合约代码，例如 IF2503。")
        if not manager.available():
            raise ValueError("本机未安装 vnpy / vnpy_ctp，无法使用 CTP 行情。")
        if not manager.connected:
            raise ValueError("请先连接 SimNow 柜台：连接成功后才能订阅 CTP 行情。")
        aggregator = Aggregator(symbol)
        try:
            # 重启服务后接着上次的 K 线画，避免图上只有刚启动的一两根
            aggregator.seed(db.recent_live_bars(symbol, 600))
        except Exception:
            logger.exception("读取历史实时 bar 失败：%s", symbol)
        with self.lock:
            self._stop_locked()
            self.aggs = {symbol: aggregator}
            self.driver, self.running = "ctp", True
            self.symbol, self.exchange = symbol, exchange
            self.speedup = 1
            self.started_at = _iso(datetime.now(TZ))
            self.message = f"CTP 行情运行中：{symbol}.{exchange}"
        manager.add_tick_listener(self._on_ctp_tick)
        manager.subscribe(symbol, exchange)
        logger.info("CTP 行情启动：%s.%s", symbol, exchange)
        return self.status()

    def _on_ctp_tick(self, tick) -> None:
        """vn.py 事件线程回调：只做聚合与广播，异常不外抛。"""
        raw = str(getattr(tick, "symbol", "") or "")
        ts = getattr(tick, "datetime", None)
        price = getattr(tick, "last_price", None)
        if not raw or not ts or not price:
            return
        volume = float(getattr(tick, "volume", 0) or 0)
        with self.lock:
            if not self.running or self.driver != "ctp":
                return
            agg = self.aggs.get(raw)
            if agg is None:
                return
            previous = self._last_ctp_volume.get(raw)
            delta = max(0.0, volume - previous) if previous is not None else 0.0
            self._last_ctp_volume[raw] = volume
            closed, current = agg.push(float(price), delta, ts)
            if closed:
                self._persist(raw, closed)
            self._publish({"type": "bar", "symbol": raw, "bar": current,
                           "closed": bool(closed)})

    # ---- 停止 ------------------------------------------------------------
    def stop(self) -> dict:
        with self.lock:
            self._stop_locked()
        return self.status()

    def _stop_locked(self) -> None:
        self._stop.set()
        was_running = self.running
        self.running = False
        self.driver = None
        self._thread = None
        simnow.simnow.remove_tick_listener(self._on_ctp_tick)
        self._last_ctp_volume.clear()
        # 只停采集，不主动断开 CTP 连接：用户可能还要继续看 SimNow 的柜台状态
        self.message = "已停止" if was_running else self.message

    @staticmethod
    def _persist(symbol: str, bar: dict) -> None:
        try:
            db.save_live_bar(symbol, bar)
        except Exception:                    # 落库失败不能打断采集
            logger.exception("实时 bar 写入失败：%s %s", symbol, bar.get("timestamp"))


hub = FeedHub()
