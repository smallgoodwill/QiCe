"""品种诊断的「消息面 / 情绪」上下文。

为什么单独一个模块
------------------
`macro.py` 负责品种的**环境分析**（供需、库存、基差、宏观），它抓的数据其实相当全，
但以前只被压成一坨 JSON 塞给模型，页面上一条都看不见；而 `diagnose.py` 的 prompt
六个段落全是技术面，于是用户看到的就是「技术面一大片、消息面几乎没有」。

本模块把这些线索**结构化地算出来并直接交给页面**，同时给 AI 诊断提供事实清单：

* 真·恐慌指数：CBOE 官方 VIX 日线（不是拿波动率分位冒充的）
* 跨市场避险：COMEX 黄金 / 白银 / 原油 / 铜、美元指数、中美股指
* 品种定向快讯：SHMET 分类接口（铜/铝/锌/铅/镍/锡/贵金属/财经/要闻）
* 财经日历：高重要性事件
* 资金行为与供需：复用 macro 的 15 分钟缓存（持仓量、成交量、基差、库存）
* 复合「情绪温度」：把上面的线索加权成 0–100，**公式与每个分量都对外可见**

口径上的三条底线
----------------
1. 情绪温度是**本地复合代理指标**，不是 CBOE VIX 本身；VIX 作为独立真实数据单列。
2. 快讯情绪用**关键词词典**打标，只算线索强度，不是语义模型、不是研报结论。
3. 任一数据源失败只标「未获取」，绝不补数字；缺失分量从加权里剔除后重新归一化。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests

from . import config, instruments, macro, marketdata

logger = logging.getLogger(__name__)

CACHE_TTL = 900                  # 15 分钟，与 macro 一致
CACHE_DIR = config.RUNTIME_DIR / "context-cache"
FETCH_DEADLINE = 20              # 单轮抓取总预算（秒）
VIX_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
VIX_SOURCE = "CBOE 官方 VIX 日线 CSV（cdn.cboe.com）"

NOTICE = ("消息面与情绪线索来自公开第三方接口，可能有延迟或缺失；情绪温度是本地按公开"
          "公式加权的复合代理指标，不是 CBOE VIX，也不是任何机构发布的恐慌指数。"
          "快讯倾向由关键词词典判定，只作线索、不作结论。")

# SHMET 快讯分类（见 akshare.futures_news_shmet 的 symbol_map）。
# 有色品种能拿到 2000 条**品种定向**历史；其余板块退回「财经 / 要闻」通用宏观消息，
# 再用板块关键词从全市场快讯里补，保证 67 个品种都有消息面内容。
SECTOR_TO_SHMET = {
    "有色": "铜", "贵金属": "贵金属", "黑色": "财经", "能化": "财经",
    "农产品": "财经", "金融": "财经", "航运": "财经", "新能源": "小金属",
}
INSTRUMENT_TO_SHMET = {
    "cu": "铜", "al": "铝", "zn": "锌", "pb": "铅", "ni": "镍", "sn": "锡",
    "au": "贵金属", "ag": "贵金属",
}

# 板块关键词：用于从通用快讯里挑出跟本品种相关的条目
SECTOR_KEYWORDS = {
    "黑色": ["钢材", "螺纹", "热卷", "铁矿", "焦煤", "焦炭", "钢厂", "高炉", "粗钢",
             "基建", "房地产", "地产", "建材", "废钢", "铁矿石", "钢坯"],
    "有色": ["铜", "铝", "锌", "铅", "镍", "锡", "有色", "LME", "冶炼", "精矿"],
    "贵金属": ["黄金", "白银", "金价", "银价", "美联储", "降息", "加息", "通胀",
               "美元", "避险", "美债", "实际利率", "央行购金"],
    "能化": ["原油", "OPEC", "油价", "炼厂", "化工", "PTA", "甲醇", "塑料", "聚丙烯",
             "橡胶", "天然气", "沥青", "聚酯", "尿素", "纯碱", "玻璃"],
    "农产品": ["大豆", "豆粕", "豆油", "玉米", "棕榈", "菜油", "菜粕", "生猪", "白糖",
               "棉花", "USDA", "丰产", "减产", "种植", "压榨", "饲料"],
    "金融": ["股指", "A股", "沪深", "上证", "中证", "国债", "利率", "央行", "MLF",
             "降准", "资本市场", "北向"],
    "航运": ["集运", "运价", "红海", "航运", "集装箱", "SCFI", "港口", "船公司"],
    "新能源": ["碳酸锂", "锂矿", "锂盐", "多晶硅", "工业硅", "硅料", "新能源",
               "电池", "光伏", "储能", "锂电"],
}

# 通用宏观关键词：SHMET 只给有色品种做定向分类，黑色/能化/农产品往往只能拿「财经」。
# 这些词命中的条目会标成「宏观线索」，与品种直接相关的「板块线索」分开计。
GENERAL_KEYWORDS = ["美联储", "降息", "加息", "通胀", "CPI", "PPI", "PMI", "GDP", "美元",
                    "关税", "贸易", "地缘", "衰退", "刺激", "央行", "财政部", "国务院",
                    "政策", "OPEC", "经济数据", "非农", "议息", "流动性", "人民币", "汇率"]

# 轻量情绪词典：命中即计数，正负相减得到 [-1, 1] 的线索倾向。
# 刻意只收方向明确的词；「上调 / 下调」这类既可指价格也可指利率的词不收，避免误判。
BULL_WORDS = ["减产", "停产", "检修", "限产", "短缺", "供不应求", "去库", "库存下降",
              "补库", "需求回升", "超预期", "涨价", "走强", "上涨", "反弹", "刺激",
              "宽松", "降息", "降准", "抢购", "中断", "罢工", "制裁", "冲突", "避险",
              "供应收紧", "成交放量", "上调目标价", "创纪录", "偏紧"]
BEAR_WORDS = ["增产", "复产", "扩产", "累库", "库存增加", "供应过剩", "需求疲软",
              "不及预期", "降价", "走弱", "下跌", "回落", "抛售", "紧缩", "加息",
              "衰退", "解除", "缓和", "丰产", "需求下滑", "供应宽松", "下调目标价",
              "偏松", "过剩", "库存高企"]

HTML_TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _f(value, digits: int = 2):
    """把 pandas / 字符串里的数字安全转 float，脏值一律返回 None。"""
    try:
        if value is None or value == "":
            return None
        result = float(value)
        if result != result or result in (float("inf"), float("-inf")):
            return None
        return round(result, digits)
    except (TypeError, ValueError):
        return None


def _clamp(value, low=0.0, high=100.0):
    return max(low, min(high, value))


def _clean_text(text) -> str:
    """SHMET 正文偶尔带 <span style=...>；统一去标签、压空白。"""
    return WS.sub(" ", HTML_TAG.sub("", str(text or ""))).strip()


def _cache_file(symbol: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]", "_", str(symbol))[:80]
    return CACHE_DIR / f"{safe}.json"


def _read_cache(symbol: str) -> dict | None:
    path = _cache_file(symbol)
    try:
        if not path.exists():
            return None
        if time.time() - path.stat().st_mtime > CACHE_TTL:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_cache(symbol: str, payload: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_file(symbol).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.warning("消息面缓存写入失败：%s", symbol)


def _tone_of(text: str) -> tuple[str, list[str]]:
    """关键词情绪：返回 (偏多/偏空/中性, 命中词)。只算线索，不做语义判断。"""
    bull = [w for w in BULL_WORDS if w in text]
    bear = [w for w in BEAR_WORDS if w in text]
    if len(bull) == len(bear):
        return "中性", (bull + bear)[:4]
    return ("偏多" if len(bull) > len(bear) else "偏空"), (bull + bear)[:4]


# --------------------------------------------------------------------------
# 数据源（每个函数独立返回 dict，抛异常由编排层兜住）
# --------------------------------------------------------------------------
def _fetch_vix() -> dict:
    """CBOE 官方 VIX 日线：最新值、变化、近一年分位、分级、30 日走势。"""
    response = requests.get(VIX_URL, timeout=12)
    response.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(response.text)))[-320:]
    parsed = []
    for row in rows:
        try:
            close = float(row["CLOSE"])
            date = datetime.strptime(row["DATE"], "%m/%d/%Y").date().isoformat()
            parsed.append({"date": date, "close": round(close, 2)})
        except (KeyError, ValueError, TypeError):
            continue
    if len(parsed) < 30:
        raise ValueError("VIX 历史行数不足 30 条。")

    closes = [row["close"] for row in parsed]
    latest = closes[-1]

    def change(days):
        if len(closes) <= days:
            return None
        base = closes[-days - 1]
        return round((latest / base - 1) * 100, 2) if base else None

    window = closes[-250:]
    percentile = round(sum(1 for value in window if value <= latest) / len(window) * 100, 1)
    if latest < 15:
        level, tone = "低波动", "green"
    elif latest < 20:
        level, tone = "常态", "green"
    elif latest < 30:
        level, tone = "警惕", "amber"
    else:
        level, tone = "恐慌", "red"
    return {
        "label": "VIX 恐慌指数", "source": VIX_SOURCE, "date": parsed[-1]["date"],
        "close": latest, "change1dPct": change(1), "change5dPct": change(5),
        "change20dPct": change(20), "percentile1y": percentile,
        "level": level, "tone": tone, "spark": closes[-30:],
        "sampleDays": len(window),
    }


def _fetch_overseas() -> dict:
    """外盘避险资产实时：黄金 / 白银 / 原油 / 铜 / 伦敦金 / 天然气。"""
    import akshare as ak
    frame = ak.futures_foreign_commodity_realtime(symbol=["GC", "SI", "CL", "HG", "XAU", "NG"])
    if frame is None or len(frame) == 0:
        raise ValueError("外盘实时接口没有返回数据。")
    items = []
    for _, row in frame.iterrows():
        price = _f(row.get("最新价"))
        change = _f(row.get("涨跌幅"))
        if price is None or change is None:
            continue
        items.append({"name": str(row.get("名称") or ""), "price": price,
                      "changePct": change, "time": str(row.get("行情时间") or "")[:8],
                      "date": str(row.get("日期") or "")[:10]})
    if not items:
        raise ValueError("外盘实时行情全部为空值。")
    return {"label": "外盘避险资产", "items": items}


def _news_rows(category: str, limit: int) -> list[dict]:
    """取某个 SHMET 分类的最近若干条快讯（接口按时间正序返回，故取 tail 再反转）。"""
    import akshare as ak
    frame = ak.futures_news_shmet(symbol=category)
    if frame is None or len(frame) == 0:
        raise ValueError(f"{category} 快讯接口没有返回内容。")
    rows = []
    for _, row in frame.tail(limit).iterrows():
        text = _clean_text(row.get("内容"))
        if not text:
            continue
        rows.append({"time": str(row.get("发布时间") or "")[:19].replace("T", " "),
                     "text": text[:220]})
    rows.sort(key=lambda item: item["time"], reverse=True)
    return rows


def _fetch_news(inst: instruments.Instrument) -> dict:
    """品种定向快讯 + 全市场快讯，合并后按板块关键词过滤并打情绪标签。"""
    category = INSTRUMENT_TO_SHMET.get(inst.code) or SECTOR_TO_SHMET.get(inst.sector, "财经")
    sector_words = [word for word in [inst.name, inst.name[:2],
                                      *SECTOR_KEYWORDS.get(inst.sector, [])] if len(word) >= 2]
    sector_words = list(dict.fromkeys(sector_words))
    general_words = GENERAL_KEYWORDS
    keywords = list(dict.fromkeys([*sector_words, *general_words]))

    # 非定向品种额外抓「要闻」，避免只有几十条财经分类、一条都命中不了
    categories = [category] if category not in ("财经", "要闻") else [category, "要闻"]
    targeted: list[dict] = []
    for name in categories:
        try:
            targeted.extend(_news_rows(name, 60))
        except Exception:
            continue
    if not targeted:
        raise ValueError("快讯分类接口均不可用。")
    flash = _news_rows("全部", 10)
    targeted_is_specific = category not in ("财经", "要闻")

    related: list[dict] = []
    market_items: list[dict] = []
    seen = set()

    def decorate(item, scope, priority):
        tone, hits = _tone_of(item["text"])
        return {**item, "tone": tone, "hits": hits, "scope": scope, "priority": priority}

    def classify(pool, force_general=False):
        for item in pool:
            if item["text"] in seen:
                continue
            seen.add(item["text"])
            hit_sector = any(word in item["text"] for word in sector_words)
            hit_general = any(word in item["text"] for word in general_words)
            if targeted_is_specific and not force_general:
                # SHMET 定向分类里也会混入全站快讯，命中的优先级放前面
                related.append(decorate(item, f"{category}定向", 0 if hit_sector else 1))
            elif hit_sector:
                related.append(decorate(item, "板块线索", 0))
            elif hit_general:
                related.append(decorate(item, "宏观线索", 2))
            else:
                market_items.append(item)

    classify(targeted)
    classify(flash, force_general=True)

    # 两次稳定排序：先按时间倒序，再按线索优先级（板块 > 定向 > 宏观），
    # 保证给模型和页面看到的前几条一定是与品种最相关的
    related.sort(key=lambda item: item["time"], reverse=True)
    related.sort(key=lambda item: item["priority"])
    market_items.sort(key=lambda item: item["time"], reverse=True)
    bull = sum(1 for item in related if item["tone"] == "偏多")
    bear = sum(1 for item in related if item["tone"] == "偏空")
    denominator = max(1, bull + bear)
    return {
        "label": "消息面快讯", "category": category,
        "keywords": keywords[:8], "relatedCount": len(related),
        "bullCount": bull, "bearCount": bear,
        "toneScore": round((bull - bear) / denominator, 3),
        "items": related[:24],
        "marketItems": market_items[:8],
        "backgroundCount": len(market_items),
        "scopeBreakdown": {scope: sum(1 for item in related if item["scope"] == scope)
                           for scope in sorted({item["scope"] for item in related})},
        "method": "关键词词典（非语义模型）：利多词命中数 − 利空词命中数",
    }


def _fetch_calendar() -> dict:
    """复用 macro 里已经写好的财经日历抓取（只保留高重要性事件）。"""
    payload = macro._fetch_calendar()
    return {"label": "财经日历", **{k: v for k, v in payload.items() if k != "label"}}


def _fetch_macro(symbol: str) -> dict:
    """复用 macro 的 15 分钟缓存：资金行为、基差、库存、股指与美元。"""
    return macro.collect(symbol)


# --------------------------------------------------------------------------
# 复合情绪温度：每个分量 0–100（越高越紧张），权重对外公开
# --------------------------------------------------------------------------
WEIGHTS = {
    "vixPercentile": 0.30, "vixRise": 0.15, "ownVol": 0.15,
    "riskOff": 0.20, "newsTone": 0.10, "funds": 0.10,
}
COMPONENT_LABELS = {
    "vixPercentile": "VIX 近一年分位", "vixRise": "VIX 近 5 日抬升",
    "ownVol": "品种自身波动率分位", "riskOff": "跨市场避险强度",
    "newsTone": "相关快讯偏空程度", "funds": "资金异动（跌势增仓）",
}


def _risk_off_value(vix: dict | None, overseas: list[dict], global_items: list[dict]) -> tuple[float | None, list[str]]:
    """把跨市场线索折算成 0–100 的避险强度，并留下可核对的文字线索。"""
    clues: list[str] = []
    scores: list[float] = []

    def find(names):
        for item in global_items:
            if any(name in str(item.get("name") or "") for name in names):
                return item
        return None

    equity = [find([n]) for n in ("标普500", "纳斯达克", "道琼斯", "上证指数", "恒生指数")]
    equity_changes = [_f(item.get("changePct")) for item in equity if item]
    equity_changes = [value for value in equity_changes if value is not None]
    if equity_changes:
        average = statistics.mean(equity_changes)
        scores.append(_clamp(50 - average * 12))
        clues.append(f"中美股指平均 {average:+.2f}%")

    gold = next((item for item in overseas if "黄金" in item["name"] or "伦敦金" in item["name"]), None)
    if gold:
        scores.append(_clamp(50 + gold["changePct"] * 9))
        clues.append(f"{gold['name']} {gold['changePct']:+.2f}%")
    dollar = find(["美元指数"])
    if dollar:
        change = _f(dollar.get("changePct"))
        if change is not None:
            scores.append(_clamp(50 + change * 18))
            clues.append(f"美元指数 {change:+.2f}%")
    if vix and vix.get("change5dPct") is not None:
        scores.append(_clamp(50 + vix["change5dPct"] * 1.6))
        clues.append(f"VIX 近 5 日 {vix['change5dPct']:+.2f}%")

    if not scores:
        return None, clues
    return round(statistics.mean(scores), 1), clues


def _temperature(*, vix, overseas, global_items, news, quote) -> dict:
    """缺失分量剔除后按剩余权重归一化；公式与每个分量的取值都写进返回值。"""
    values: dict[str, float] = {}
    displays: dict[str, str] = {}

    if vix:
        if vix.get("percentile1y") is not None:
            # 分位高只代表「比过去一年紧张」，不等于绝对恐慌；
            # 混合一个绝对水平映射（10→0、40→100），避免 20 出头的 VIX 被算成 90 分
            percentile = vix["percentile1y"]
            absolute = _clamp(((_f(vix.get("close"), 2) or 20) - 10) / 30 * 100)
            values["vixPercentile"] = round(0.6 * percentile + 0.4 * absolute, 1)
            displays["vixPercentile"] = f"VIX {vix.get('close')} · 近一年分位 {percentile}%"
        if vix.get("change5dPct") is not None:
            values["vixRise"] = _clamp(50 + vix["change5dPct"] * 1.6)
            displays["vixRise"] = f"VIX 5 日 {vix['change5dPct']:+.2f}%"

    if quote and quote.get("vol20Percentile") is not None:
        values["ownVol"] = _clamp(quote["vol20Percentile"])
        displays["ownVol"] = f"品种波动率分位 {quote['vol20Percentile']}%"

    risk_value, clues = _risk_off_value(vix, overseas, global_items)
    if risk_value is not None:
        values["riskOff"] = risk_value
        displays["riskOff"] = "；".join(clues[:3])

    if news and news.get("relatedCount"):
        values["newsTone"] = _clamp((1 - news["toneScore"]) / 2 * 100)
        displays["newsTone"] = f"偏多 {news['bullCount']} / 偏空 {news['bearCount']}"

    if quote:
        change5d = _f(quote.get("changePct5d"))
        oi = _f(quote.get("openInterestChangePct"))
        if change5d is not None:
            # 跌得越多、同时持仓量越大（增仓对峙），紧张度越高
            funds = _clamp(50 - change5d * 8 + (oi or 0) * 1.5)
            values["funds"] = round(funds, 1)
            displays["funds"] = f"5 日 {change5d:+.2f}% · 持仓量 {oi if oi is not None else '—'}%"

    used_weight = sum(WEIGHTS[key] for key in values)
    if not used_weight:
        return {"score": None, "level": "数据不足", "tone": "",
                "components": [], "missing": list(WEIGHTS),
                "formula": "所有分量均未获取，无法计算情绪温度。"}

    score = round(sum(values[key] * WEIGHTS[key] for key in values) / used_weight, 1)
    components = [{"key": key, "label": COMPONENT_LABELS[key],
                   "value": round(values[key], 1),
                   "weight": round(WEIGHTS[key] / used_weight, 3),
                   "display": displays.get(key, "")} for key in values]
    if score < 25:
        level, tone = "平稳", "green"
    elif score < 45:
        level, tone = "温和", "blue"
    elif score < 65:
        level, tone = "警惕", "amber"
    elif score < 85:
        level, tone = "紧张", "red"
    else:
        level, tone = "恐慌", "red"
    return {
        "score": score, "level": level, "tone": tone, "components": components,
        "missing": [key for key in WEIGHTS if key not in values],
        "formula": ("各分量先折算为 0–100（越高越紧张），按权重加权；"
                    "缺失分量直接剔除，剩余权重重新归一化。权重：VIX 水平与分位 30%（0.6×近一年分位 "
                    "+ 0.4×绝对水平映射）/ VIX 5 日抬升 15% / 品种波动率分位 15% / "
                    "跨市场避险 20% / 快讯偏空 10% / 资金异动 10%。"),
        "riskClues": clues,
    }


# --------------------------------------------------------------------------
# 编排
# --------------------------------------------------------------------------
def collect(symbol: str, *, force: bool = False) -> dict:
    """并行抓取全部线索 → 算情绪温度 → 落 15 分钟缓存。单源失败不影响整体。"""
    inst = instruments.guess(symbol)
    if not inst:
        raise ValueError("无法识别该品种，请从列表中选择合约，例如 rb2601。")
    key = str(symbol or "").strip() or inst.contract(datetime.now().year, datetime.now().month)

    if not force:
        cached = _read_cache(key)
        if cached:
            return {**cached, "cached": True}

    tasks = {
        "vix": (_fetch_vix, ()),
        "overseas": (_fetch_overseas, ()),
        "news": (_fetch_news, (inst,)),
        "calendar": (_fetch_calendar, ()),
        "macro": (_fetch_macro, (key,)),
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
                    sources[name] = future.result()
                except Exception as exc:                  # 单源失败只标注，不拖垮整页
                    logger.info("消息面数据源 %s 不可用：%s", name, exc)
                    unavailable.append({"key": name, "reason": str(exc)[:160]})
        except TimeoutError:
            # as_completed 超时后，把还没回来的源统一标成超时（已完成的照常保留）
            for future, name in futures.items():
                if name not in sources and not future.done():
                    unavailable.append({"key": name, "reason": "抓取超时"})

    vix = sources.get("vix")
    overseas = (sources.get("overseas") or {}).get("items") or []
    news = sources.get("news")
    calendar = sources.get("calendar")
    macro_data = sources.get("macro") or {}
    macro_sources = macro_data.get("sources") or {}
    quote = macro_sources.get("quote")
    global_items = (macro_sources.get("global") or {}).get("items") or []
    basis = macro_sources.get("basis")
    inventory = macro_sources.get("inventory")

    panel = {
        "symbol": key,
        "instrument": inst.to_dict(),
        "generatedAt": datetime.now(marketdata.CN_TZ).isoformat(timespec="seconds"),
        "elapsedMs": int((time.time() - started) * 1000),
        "vix": vix,
        "overseas": overseas,
        "global": global_items,
        "news": news,
        "calendar": (calendar or {}).get("items") or [],
        "calendarDate": (calendar or {}).get("date"),
        "quote": {k: quote.get(k) for k in
                  ("lastDate", "lastClose", "changePct1d", "changePct5d", "changePct20d",
                   "vol20Pct", "vol20Percentile", "openInterestChangePct",
                   "volumeChangePct", "drawdownFrom60dHighPct", "sampleDays")} if quote else None,
        "basis": {k: basis.get(k) for k in
                  ("date", "spotPrice", "dominantPrice", "basis", "basisRatePct")} if basis else None,
        "inventory": {k: inventory.get(k) for k in
                      ("date", "value", "change", "changePct5d")} if inventory else None,
        "temperature": _temperature(vix=vix, overseas=overseas,
                                    global_items=global_items, news=news, quote=quote),
        "unavailable": unavailable,
        "notice": NOTICE,
        "cached": False,
    }
    _write_cache(key, panel)
    return panel


def cached(symbol: str) -> dict:
    """只读缓存（不触发抓取），给页面首屏用。"""
    payload = _read_cache(str(symbol or "").strip())
    if not payload:
        return {"available": False, "symbol": symbol,
                "message": "还没有消息面缓存，点「刷新线索」抓一次。"}
    return {"available": True, **payload, "cached": True}


# --------------------------------------------------------------------------
# 交给模型的事实清单：只留数字、日期、口径，不塞长文本
# --------------------------------------------------------------------------
def facts(panel: dict) -> dict:
    """把面板压成模型读得动的 JSON；每个数字都带来源标签，缺失项显式列出。"""
    out: dict = {
        "品种": f"{(panel.get('instrument') or {}).get('name')}（{(panel.get('instrument') or {}).get('sector')}）",
        "数据抓取时间": panel.get("generatedAt"),
        "未获取的数据项": [item["key"] for item in panel.get("unavailable") or []],
        "口径": "情绪温度为本地复合代理指标；VIX 为 CBOE 真实数据；快讯倾向为关键词规则",
    }
    vix = panel.get("vix")
    if vix:
        out["恐慌指数VIX"] = {
            "来源": vix.get("source"), "日期": vix.get("date"), "收盘": vix.get("close"),
            "近1日变动(%)": vix.get("change1dPct"), "近5日变动(%)": vix.get("change5dPct"),
            "近20日变动(%)": vix.get("change20dPct"),
            "近一年分位(%)": vix.get("percentile1y"), "分级": vix.get("level"),
        }
    if panel.get("overseas"):
        out["外盘避险资产"] = [
            {"名称": item["name"], "涨跌幅(%)": item["changePct"], "日期": item.get("date")}
            for item in panel["overseas"]]
    if panel.get("global"):
        out["全球股指与美元"] = [
            {"名称": item.get("name"), "涨跌幅(%)": item.get("changePct")}
            for item in panel["global"]]
    temp = panel.get("temperature") or {}
    if temp.get("score") is not None:
        out["情绪温度"] = {
            "分值(0-100)": temp.get("score"), "等级": temp.get("level"),
            "分量": [{"名称": c["label"], "取值": c["value"], "权重": c["weight"],
                      "依据": c["display"]} for c in temp.get("components") or []],
            "缺失分量": temp.get("missing"), "公式": temp.get("formula"),
            "跨市场线索": temp.get("riskClues"),
        }
    news = panel.get("news")
    if news:
        out["消息面"] = {
            "分类": news.get("category"), "相关条数": news.get("relatedCount"),
            "偏多条数": news.get("bullCount"), "偏空条数": news.get("bearCount"),
            "倾向分(-1~1)": news.get("toneScore"), "判定方式": news.get("method"),
            "线索来源分布": news.get("scopeBreakdown"),
            "相关快讯": [{"时间": item["time"], "内容": item["text"][:150],
                          "倾向": item["tone"], "线索类型": item.get("scope")}
                         for item in (news.get("items") or [])[:16]],
            "宏观背景快讯": [item["text"][:120] for item in (news.get("marketItems") or [])[:6]],
        }
    if panel.get("calendar"):
        out["今日财经事件"] = [
            {"时间": item.get("time"), "地区": item.get("region"),
             "事件": item.get("event"), "重要性": item.get("stars")}
            for item in panel["calendar"][:10]]
    if panel.get("quote"):
        out["品种资金行为"] = panel["quote"]
    if panel.get("basis"):
        out["现货基差"] = panel["basis"]
    if panel.get("inventory"):
        out["库存"] = panel["inventory"]
    return out
