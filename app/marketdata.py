"""行情来源：CSV 解析（脚本/导入用）+ 按交易时段合成分钟 K 线（模拟盘默认源）。

模拟盘不再要求用户先准备 CSV，所以这里补一个"给定日期区间就能出 bar"的生成器。
它是**确定性**的：同一合约同一天永远生成同一段走势，便于复现和对比策略参数。
真实行情请走 CTP 采集（app/feed.py → db.live_bars），两者绝不混用。
"""
from __future__ import annotations

import csv
import io
import random
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from . import config, engine

REQUIRED_FIELDS = ("timestamp", "open", "high", "low", "close", "volume")

# 国内股指期货日盘：09:30–11:30、13:00–15:00，各 120 根 1 分钟 bar
SESSIONS = ((time(9, 30), time(11, 30)), (time(13, 0), time(15, 0)))
MINUTES_PER_SESSION = 120
BARS_PER_DAY = MINUTES_PER_SESSION * len(SESSIONS)
CN_TZ = timezone(timedelta(hours=8))


# --------------------------------------------------------------------------
# CSV（脚本、CLI、旧数据迁移用；页面已不再要求用户上传）
# --------------------------------------------------------------------------
def parse_csv_text(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    missing = [name for name in REQUIRED_FIELDS if name not in (reader.fieldnames or [])]
    if missing:
        raise ValueError("CSV 缺少字段：" + "、".join(missing))
    rows = []
    for line_no, row in enumerate(reader, start=2):
        if not any((value or "").strip() for value in row.values()):
            continue
        try:
            rows.append({"timestamp": (row["timestamp"] or "").strip(),
                         "open": float(row["open"]), "high": float(row["high"]),
                         "low": float(row["low"]), "close": float(row["close"]),
                         "volume": float(row["volume"]),
                         "event_blackout": (row.get("event_blackout") or "").strip().lower()
                                           in ("1", "true", "yes", "y")})
        except (TypeError, ValueError):
            raise ValueError(f"CSV 第 {line_no} 行数值格式无效。") from None
    return engine.validate_bars(rows)


def load_csv(path: Path) -> list[dict]:
    return parse_csv_text(Path(path).read_text(encoding="utf-8-sig"))


def sample_bars() -> list[dict]:
    """内置合成样例行情，用于零数据时跑通全链路（明确标注非真实行情）。"""
    if not config.SAMPLE_CSV.exists():
        raise ValueError("内置样例行情不存在，请检查 data/samples/ 目录。")
    return load_csv(config.SAMPLE_CSV)


# --------------------------------------------------------------------------
# 合成分钟 K 线
# --------------------------------------------------------------------------
def trading_days(start: date, end: date, *, limit: int = 20) -> list[date]:
    """区间内的交易日（周一至周五，暂不剔除法定节假日），取最近 limit 个。"""
    if end < start:
        start, end = end, start
    days: list[date] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days[-limit:]


def recent_trading_days(count: int, *, today: date | None = None) -> list[date]:
    """截至今天的最近 count 个交易日（含今天）。"""
    count = max(1, int(count))
    today = today or datetime.now(CN_TZ).date()
    days: list[date] = []
    cursor = today
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def _round_tick(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 6)


def synthetic_bars(days: list[date], *, symbol: str = "DEMO", base_price: float = 4000.0,
                   tick: float = 0.2, max_bars: int | None = None,
                   until: datetime | None = None) -> list[dict]:
    """按交易日生成确定性的 1 分钟 bar。

    走势模型：每 20 分钟切换一次方向（漂移 0.012%–0.04%/分钟，偶尔接近 0 走震荡），
    叠加高斯噪声。目的不是模拟真实市场，而是让均线策略在样本里出现足够多次交叉，
    从而能完整演示"信号 → 成交 → 盈亏"这条链路；同时用均值回归项把价格锁在
    开盘价 ±3% 内，避免长样本单边漂移到不合理价位。
    """
    tick = max(tick, 0.0001)
    bars: list[dict] = []
    price = float(base_price)
    anchor = float(base_price)
    for day in days:
        rng = random.Random(f"qice:{symbol}:{day.isoformat()}")
        drift = 0.0
        minute_index = 0
        for begin, finish in SESSIONS:
            stamp_time = datetime.combine(day, begin, tzinfo=CN_TZ)
            for _ in range(MINUTES_PER_SESSION):
                if until is not None and stamp_time > until:
                    return bars        # 当天未收盘：只生成到当前时刻，不造"未来"的 bar
                if minute_index % 20 == 0:
                    strength = rng.choice((0.0, 0.0002, 0.00028, 0.00038))
                    drift = rng.choice((-1, 1)) * strength
                open_price = price
                pull = -(price / anchor - 1) * 0.00008       # 远离锚点时的回归压力
                close_price = open_price * (1 + drift + pull + rng.gauss(0, 0.00035))
                wiggle = abs(rng.gauss(0, 0.00028)) + 0.00004
                high_price = max(open_price, close_price) * (1 + wiggle)
                low_price = min(open_price, close_price) * (1 - wiggle)
                o, c = _round_tick(open_price, tick), _round_tick(close_price, tick)
                h, l = _round_tick(high_price, tick), _round_tick(low_price, tick)
                h = max(h, o, c)
                l = min(l, o, c)
                bars.append({
                    "timestamp": stamp_time.isoformat(timespec="seconds"),
                    "open": o, "high": h, "low": l, "close": c,
                    "volume": float(max(1, int(abs(rng.gauss(600, 200)) + 80))),
                    "event_blackout": False,
                })
                price = c
                minute_index += 1
                stamp_time += timedelta(minutes=1)
                if max_bars and len(bars) >= max_bars:
                    return bars
    return bars
