"""品种大环境分析：把外部数据抓下来，整理成模型能读的事实清单，再让 AI 解释。

为什么不是"直接问模型"
----------------------
DeepSeek 不能联网，它嘴里的"当前库存""最新基差"全是训练数据里的旧值，
用来做交易判断等于瞎猜。所以这里的顺序是**先用数据把事实钉死，再让模型解释事实**：
数字全部来自 akshare 实时抓取（带抓取时间），模型只负责把数字翻译成人话。

数据源与降级
------------
每个源都是独立线程 + 独立超时，**任何一个挂掉都不影响其余部分**，
界面上会明确显示"这项没拿到、原因是什么"，而不是编一个数字填空。
全部失败时仍可返回纯本地计算的情绪指标，至少能看出波动率处在什么位置。

关于"恐慌指数"
--------------
CBOE VIX 在 akshare 里没有稳定来源（`index_option_300index_qvix` 依赖的第三方站点
经常 502，全球指数表里也没有 VIX 这一项）。所以这里用**可自行计算的替代口径**：
主力连续日线的 20 日年化已实现波动率，以及它在近一年中的分位。
它衡量同样的事情（市场有多不安），但**不是 VIX 本身**，页面上必须写清楚，
否则就成了拿一个自造指标冒充知名指数——那比没有更糟。
"""
from __future__ import annotations

import json
import logging
import math
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

from . import ai, config, instruments, marketdata

logger = logging.getLogger(__name__)

CACHE_TTL = 900          # 15 分钟：盘中够新，又不会把接口打爆
CACHE_DIR = config.RUNTIME_DIR / "macro-cache"
FETCH_DEADLINE = 22      # 单次抓取总预算（秒）

BASE_NOTICE = ("数据来自公开第三方接口（akshare），可能有延迟或缺失；"
               "「情绪指数」是用已实现波动率构造的替代指标，不是 CBOE VIX。"
               "以下内容为数据解读，不构成任何投资建议。")

# 库存接口只认固定名称，且并非全品种都有；这里给一份已知可用的映射，
# 没列到的品种就直接跳过（不猜、不编）。
INVENTORY_NAMES = {
    "rb": "螺纹钢", "hc": "热轧卷板", "i": "铁矿石", "j": "焦炭", "jm": "焦煤",
    "m": "豆粕", "y": "豆油", "p": "棕榈油", "c": "玉米", "cs": "玉米淀粉",
    "a": "豆一", "b": "豆二", "jd": "鸡蛋", "lh": "生猪",
    "l": "塑料", "v": "PVC", "pp": "聚丙烯", "eg": "乙二醇", "eb": "苯乙烯",
    "ta": "PTA", "ma": "甲醇", "fg": "玻璃", "sa": "纯碱", "ur": "尿素",
    "sr": "白糖", "cf": "棉花", "oi": "菜油", "rm": "菜粕", "ap": "苹果",
    "al": "铝", "zn": "锌", "ni": "镍", "sn": "锡", "pb": "铅",
}


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _f(value, digits: int = 4):
    """把 numpy/pandas 的数字转成原生 float，None/NaN 一律变 None。

    必要性：pandas 的 np.float64 直接塞进 json.dumps 会 TypeError，
    而 NaN 会序列化成非法 JSON（NaN），前端 JSON.parse 直接炸。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return round(number, digits)


def _cache_file(symbol: str) -> Path:
    safe = "".join(ch for ch in symbol.upper() if ch.isalnum() or ch in "-_") or "UNKNOWN"
    return CACHE_DIR / f"{safe}.json"


def _read_cache(symbol: str) -> dict | None:
    path = _cache_file(symbol)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - float(payload.get("_cachedAt") or 0) > CACHE_TTL:
        return None
    return payload


def _write_cache(symbol: str, payload: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _cache_file(symbol).write_text(
            json.dumps({**payload, "_cachedAt": time.time()}, ensure_ascii=False),
            encoding="utf-8")
    except OSError:
        logger.warning("宏观缓存写入失败：%s", symbol)


# --------------------------------------------------------------------------
# 各数据源（每个都返回 dict，失败抛异常由上层兜住）
# --------------------------------------------------------------------------
def _fetch_quote(inst: instruments.Instrument) -> dict:
    """主力连续日线：拿最近一年的收盘价，算涨跌、波动率与分位。"""
    import akshare as ak
    frame = ak.futures_main_sina(symbol=f"{inst.symbol_prefix.upper()}0")
    rows = []
    for _, row in frame.tail(260).iterrows():
        close = _f(row.get("收盘价"))
        if close is None:
            continue
        rows.append({
            "date": str(row.get("日期"))[:10],
            "close": close,
            "high": _f(row.get("最高价")),
            "low": _f(row.get("最低价")),
            "volume": _f(row.get("成交量"), 0),
            "openInterest": _f(row.get("持仓量"), 0),
        })
    if len(rows) < 25:
        raise ValueError("主力连续历史不足 25 个交易日，无法计算波动率。")
    return {"label": "主力连续日线", "rows": rows, **_trend_metrics(rows)}


def _trend_metrics(rows: list[dict]) -> dict:
    closes = [row["close"] for row in rows]

    def change(days: int):
        if len(closes) <= days:
            return None
        base = closes[-days - 1]
        return round((closes[-1] / base - 1) * 100, 2) if base else None

    # 20 日年化已实现波动率：先算对数收益，再按 sqrt(252) 放大
    returns = []
    for prev, curr in zip(closes, closes[1:]):
        if prev > 0 and curr > 0:
            returns.append(math.log(curr / prev))
    volumes = []
    for size in (20, 21, 60):
        if len(returns) >= size:
            chunk = returns[-size:]
            volumes.append(statistics.stdev(chunk) * math.sqrt(252) * 100)
    vol20 = round(volumes[0], 2) if volumes else None

    # 历史分位：把"每 20 日滚动波动率"排一排，看当前处在什么位置
    percentiles = []
    if len(returns) >= 40:
        rolling = [statistics.stdev(returns[i:i + 20]) * math.sqrt(252) * 100
                   for i in range(len(returns) - 20)]
        if rolling and vol20 is not None:
            below = sum(1 for value in rolling if value <= vol20)
            percentiles.append(round(below / len(rolling) * 100, 1))

    highs = [row["high"] for row in rows[-60:] if row.get("high")]
    peak = max(highs) if highs else None
    oi_series = [row["openInterest"] for row in rows[-21:] if row.get("openInterest")]
    vol_series = [row["volume"] for row in rows[-21:] if row.get("volume")]

    return {
        "lastDate": rows[-1]["date"],
        "lastClose": closes[-1],
        "changePct1d": round((closes[-1] / closes[-2] - 1) * 100, 2) if len(closes) > 1 else None,
        "changePct5d": change(5),
        "changePct20d": change(20),
        "vol20Pct": vol20,
        "vol20Percentile": percentiles[0] if percentiles else None,
        "drawdownFrom60dHighPct": (round((closes[-1] / peak - 1) * 100, 2)
                                   if peak and closes[-1] else None),
        # 持仓量增减常被用来判断资金是进场还是撤离
        "openInterestChangePct": (round((oi_series[-1] / oi_series[0] - 1) * 100, 2)
                                  if len(oi_series) > 1 and oi_series[0] else None),
        "volumeChangePct": (round((vol_series[-1] / statistics.mean(vol_series) - 1) * 100, 2)
                            if len(vol_series) > 2 and statistics.mean(vol_series) else None),
        "sampleDays": len(rows),
    }


def _fetch_basis(inst: instruments.Instrument) -> dict:
    """现货基差：正基差（现货>期货）通常意味着近端偏紧。"""
    import akshare as ak
    today = datetime.now(marketdata.CN_TZ).date()
    frame = ak.futures_spot_price_daily(start_day=(today - timedelta(days=7)).strftime("%Y%m%d"),
                                        end_day=today.strftime("%Y%m%d"),
                                        vars_list=[inst.symbol_prefix.upper()])
    if frame is None or len(frame) == 0:
        raise ValueError("该品种没有基差数据。")
    row = frame.iloc[-1]
    return {
        "label": "现货基差",
        "date": str(row.get("date")),
        "spotPrice": _f(row.get("spot_price"), 2),
        "dominantContract": str(row.get("dominant_contract") or ""),
        "dominantPrice": _f(row.get("dominant_contract_price"), 2),
        "basis": _f(row.get("dom_basis"), 2),
        "basisRatePct": _f(row.get("dom_basis_rate"), 6),
    }


def _fetch_inventory(inst: instruments.Instrument) -> dict:
    name = INVENTORY_NAMES.get(inst.code)
    if not name:
        raise ValueError(f"{inst.name} 没有对应的库存数据源。")
    import akshare as ak
    frame = ak.futures_inventory_em(symbol=name)
    if frame is None or len(frame) == 0:
        raise ValueError("库存接口没有返回数据。")
    tail = frame.tail(6)
    latest = tail.iloc[-1]
    first = tail.iloc[0]
    latest_value = _f(latest.get("库存"), 2)
    first_value = _f(first.get("库存"), 2)
    return {
        "label": f"{name}库存",
        "date": str(latest.get("日期"))[:10],
        "value": latest_value,
        "change": _f(latest.get("增减"), 2),
        "changePct5d": (round((latest_value / first_value - 1) * 100, 2)
                        if latest_value and first_value else None),
        "series": [{"date": str(r.get("日期"))[:10], "value": _f(r.get("库存"), 2)}
                   for _, r in tail.iterrows()],
    }


def _fetch_calendar() -> dict:
    """今日财经日历：只看重要性高的，避免把几十条低价值事件塞给模型。"""
    import akshare as ak
    today = datetime.now(marketdata.CN_TZ).date()
    frame = ak.news_economic_baidu(date=today.strftime("%Y%m%d"))
    if frame is None or len(frame) == 0:
        raise ValueError("今日没有返回财经日历（可能非交易日或接口为空）。")
    items = []
    for _, row in frame.iterrows():
        try:
            stars = int(float(row.get("重要性") or 0))
        except (TypeError, ValueError):
            stars = 0
        if stars < 2:
            continue
        items.append({
            "time": str(row.get("时间") or "")[:5],
            "region": str(row.get("地区") or ""),
            "event": str(row.get("事件") or "")[:80],
            "actual": _f(row.get("公布"), 4),
            "forecast": _f(row.get("预期"), 4),
            "previous": _f(row.get("前值"), 4),
            "stars": stars,
        })
    return {"label": "财经日历", "date": today.isoformat(), "items": items[:14]}


def _fetch_news() -> dict:
    """市场快讯：取最新若干条标题，作为"消息面"的原料。"""
    import akshare as ak
    frame = ak.futures_news_shmet(symbol="全部")
    if frame is None or len(frame) == 0:
        raise ValueError("快讯接口没有返回内容。")
    items = []
    for _, row in frame.head(12).iterrows():
        text = str(row.get("内容") or "").strip().replace("\n", " ")
        if not text:
            continue
        items.append({"time": str(row.get("发布时间") or "")[:19],
                      "text": text[:180]})
    return {"label": "市场快讯", "items": items}


def _fetch_global() -> dict:
    """全球主要指数：给"大环境"一个外部参照。"""
    import akshare as ak
    frame = ak.index_global_spot_em()
    if frame is None or len(frame) == 0:
        raise ValueError("全球指数接口没有返回数据。")
    wanted = ("上证指数", "深证成指", "创业板指", "恒生指数", "道琼斯", "纳斯达克",
              "标普500", "日经225", "美元指数", "COMEX黄金", "NYMEX原油")
    items = []
    for _, row in frame.iterrows():
        name = str(row.get("名称") or "")
        if not any(key in name for key in wanted):
            continue
        items.append({"name": name, "price": _f(row.get("最新价"), 2),
                      "changePct": _f(row.get("涨跌幅"), 2)})
        if len(items) >= 10:
            break
    return {"label": "全球主要指数", "items": items}


def _fetch_macro() -> dict:
    """国内流动性：M2/M1 同比，判断大环境松紧。"""
    import akshare as ak
    frame = ak.macro_china_money_supply()
    if frame is None or len(frame) == 0:
        raise ValueError("货币供应接口没有返回数据。")
    row = frame.iloc[0]
    return {"label": "货币供应",
            "month": str(row.get("月份") or ""),
            "m2Yoy": _f(row.get("货币和准货币(M2)-同比增长"), 2),
            "m1Yoy": _f(row.get("货币(M1)-同比增长"), 2),
            "m0Yoy": _f(row.get("流通中的现金(M0)-同比增长"), 2)}


# --------------------------------------------------------------------------
# 抓取编排
# --------------------------------------------------------------------------
def collect(symbol: str, *, force: bool = False) -> dict:
    """并行抓取全部数据源并组装快照（带 15 分钟缓存）。"""
    inst = instruments.guess(symbol)
    if not inst:
        raise ValueError("无法识别该品种，请从列表中选择合约，例如 rb2601。")
    key = str(symbol or "").strip() or inst.contract(datetime.now().year, datetime.now().month)

    if not force:
        cached = _read_cache(key)
        if cached:
            return {**cached, "cached": True}

    tasks = {
        "quote": (_fetch_quote, (inst,)),
        "basis": (_fetch_basis, (inst,)),
        "inventory": (_fetch_inventory, (inst,)),
        "calendar": (_fetch_calendar, ()),
        "news": (_fetch_news, ()),
        "global": (_fetch_global, ()),
        "macro": (_fetch_macro, ()),
    }
    sources: dict[str, dict] = {}
    unavailable: list[dict] = []

    started = time.time()
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futures = {pool.submit(func, *args): name for name, (func, args) in tasks.items()}
        try:
            for future in as_completed(futures, timeout=FETCH_DEADLINE):
                name = futures[future]
                try:
                    payload = future.result()
                    sources[name] = {"available": True, **payload}
                except Exception as exc:                      # 单个源失败不影响整体
                    logger.info("宏观数据源 %s 不可用：%s", name, exc)
                    unavailable.append({"key": name, "reason": str(exc)[:160]})
        except TimeoutError:
            done = {futures[f] for f in futures if f.done()}
            for name in tasks:
                if name not in done and name not in sources:
                    unavailable.append({"key": name, "reason": "抓取超时"})

    snapshot = {
        "symbol": key,
        "instrument": inst.to_dict(),
        "generatedAt": datetime.now(marketdata.CN_TZ).isoformat(timespec="seconds"),
        "elapsedMs": int((time.time() - started) * 1000),
        "sources": sources,
        "unavailable": unavailable,
        "notice": BASE_NOTICE,
        "cached": False,
    }
    _write_cache(key, snapshot)
    return snapshot


# --------------------------------------------------------------------------
# 交给模型
# --------------------------------------------------------------------------
ANALYST_PROMPT = """你是期货市场研究员，为一个本地模拟交易平台撰写品种环境分析。

你会收到一份**已经抓好的事实清单**（JSON）。铁律：
1. 只使用清单里的数字。清单没有的，写"该项数据未获取"，绝对不要用记忆或常识补数字。
2. 不给买卖建议、不给点位、不预测价格、不承诺收益。你只解释"现在是什么状况、为什么"。
3. 每个数字后面标注它的日期，让读者知道数据新旧。
4. 如果"情绪指数"来自已实现波动率而不是 VIX，必须如实说明口径。
5. 用中文，Markdown，四个二级标题，总长度 600–900 字：
   ## 一、大环境
   ## 二、消息面
   ## 三、供需与库存
   ## 四、市场情绪
   最后加一行：> 以上为数据解读，不构成投资建议。
"""


def analyze(symbol: str, *, force: bool = False) -> dict:
    """抓数据 + 让模型写分析。返回快照与分析文本。"""
    snapshot = collect(symbol, force=force)
    facts = _condense(snapshot)
    result = ai.complete(ANALYST_PROMPT, json.dumps(facts, ensure_ascii=False, indent=1),
                         temperature=0.3, max_tokens=1600)
    return {**snapshot, "analysis": result["text"], "model": result["model"],
            "facts": facts, "analyzedAt": datetime.now(marketdata.CN_TZ).isoformat(timespec="seconds")}


def _condense(snapshot: dict) -> dict:
    """把抓到的原始数据压成模型读得动的摘要（去掉长序列，只留关键值）。"""
    sources = snapshot.get("sources") or {}
    inst = snapshot.get("instrument") or {}
    facts: dict = {
        "品种": f"{inst.get('name')}（{inst.get('code', '').upper()}，{inst.get('exchangeName')}）",
        "合约": snapshot.get("symbol"),
        "合约乘数": inst.get("multiplier"),
        "最小变动": inst.get("tick"),
        "数据抓取时间": snapshot.get("generatedAt"),
        "未获取的数据项": [item["key"] for item in snapshot.get("unavailable") or []],
    }
    quote = sources.get("quote")
    if quote:
        facts["主力连续"] = {key: quote.get(key) for key in
                        ("lastDate", "lastClose", "changePct1d", "changePct5d", "changePct20d",
                         "drawdownFrom60dHighPct", "openInterestChangePct", "volumeChangePct",
                         "sampleDays")}
        facts["情绪指数"] = {
            "年化已实现波动率20日(%)": quote.get("vol20Pct"),
            "在近一年滚动波动率中的分位(%)": quote.get("vol20Percentile"),
            "口径说明": "由主力连续日线收盘价计算，不是 CBOE VIX",
        }
    basis = sources.get("basis")
    if basis:
        facts["现货基差"] = {key: basis.get(key) for key in
                        ("date", "spotPrice", "dominantContract", "dominantPrice",
                         "basis", "basisRatePct")}
    inventory = sources.get("inventory")
    if inventory:
        facts["库存"] = {key: inventory.get(key) for key in
                     ("date", "value", "change", "changePct5d")}
        facts["库存近6期"] = inventory.get("series")
    calendar = sources.get("calendar")
    if calendar:
        facts["今日财经事件"] = [item for item in calendar.get("items") or []][:10]
    news = sources.get("news")
    if news:
        facts["最新快讯"] = [item["text"] for item in news.get("items") or []][:8]
    glob = sources.get("global")
    if glob:
        facts["全球市场"] = glob.get("items")
    macro = sources.get("macro")
    if macro:
        facts["货币供应"] = {key: macro.get(key) for key in ("month", "m2Yoy", "m1Yoy", "m0Yoy")}
    return facts


def cached(symbol: str) -> dict:
    """只读缓存，给页面首屏用（不触发抓取，避免打开页面就等十几秒）。"""
    payload = _read_cache(str(symbol or "").strip())
    if not payload:
        return {"available": False, "symbol": symbol,
                "message": "还没有缓存数据，点「开始分析」抓取一次。"}
    return {"available": True, **payload, "cached": True}
