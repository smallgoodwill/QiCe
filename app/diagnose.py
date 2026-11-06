"""品种诊断：以**消息面 / 情绪**为主、技术面作交叉校验，再让模型解释。

和另外两个"诊断"的分工
----------------------
* 本模块（品种诊断）：你在页面上选哪个品种，就看那个品种的消息面线索与主力连续 K 线。
  消息面走 `context.py`（VIX、跨市场、快讯、财经日历、资金与供需），
  技术面走本模块的本地指标计算；模型只负责把事实讲成人话。
* `views.diagnostics()`（回放诊断）：只看当前那段模拟盘回放 —— 它回答的是
  "我这条策略在这段行情里表现如何"，与品种无关，页面已不再展示。
* `report.py`（每日诊断）：按交易日复盘成交，是复盘文字。

为什么以消息面为主
------------------
技术面数字自己会说话，页面上六个卡片已经足够；模型真正的增量在于
把「VIX 抬升 + 黄金大涨 + 快讯偏空 + 增仓下跌」这些**分散线索串成一条叙事**。
所以默认 prompt 要求消息面与情绪占 65% 以上篇幅，技术面只写一段交叉校验，
不许用均线 / ATR 复述凑字数。用户仍可切「只看技术面」回到原来的六段结构。

为什么数字必须本地算
--------------------
模型不能联网，也不会算 ATR/ADX 或 VIX 分位；把原始数据丢给它只会得到一段
正确但空洞的套话，甚至算错均线。这里先用 `indicators` / `context` 把事实钉死，
模型只做解释，并且 prompt 里明确要求"每个结论后面标依据数字与日期"、
"清单里没有的写未获取"。

样本口径
--------
`klines` 取的是**主力连续**（RB0 这类），换月会有跳空；分钟线只有最近若干天，
所以样本区间会明确写出来，页面上也标注"只看这段时间，不代表长期特征"。
"""
from __future__ import annotations

import json
import logging
import statistics
from datetime import datetime

from . import ai, context, indicators, instruments, klines, marketdata

logger = logging.getLogger(__name__)

DEFAULT_PERIOD = "1m"
DEFAULT_BARS = 900          # klines.fetch 的上限就是 900 根
MIN_BARS = 60               # 少于此数量算不出有意义的均线/ADX

NOTICE = ("统计样本为主力连续合约的近期分钟线，换月跳空与远近月价差会让价格"
          "与具体月份合约对不上；样本外推不成立，也不构成品种推荐。")

# 「只看技术面」模式沿用原来的六段结构；默认模式见下面的 CONTEXT_PROMPT
TECH_ONLY_PROMPT = """你是期货品种的技术面诊断员，为一个本地模拟交易平台写品种体检报告。

你会收到一份**已经算好的事实清单**（JSON）。铁律：
1. 只用清单里的数字。清单没有的项，写"数据未获取"，绝对不要用记忆或常识补数字。
2. 每个判断后面用括号标出依据的数字，例如"（MA20 高于 MA60 约 0.8%）"。
3. 不给买卖建议、不给目标价、不预测涨跌方向、不承诺收益。你只描述"现在是什么结构、风险在哪"。
4. 样本是主力连续分钟线，区间有限；不要把它说成长期趋势结论。
5. 中文 Markdown，六个二级标题，总长度 700–1000 字：
   ## 一、品种与样本
   ## 二、趋势结构
   ## 三、波动与风险
   ## 四、量能与参与度
   ## 五、关键位置
   ## 六、需要盯的变量
   最后单独一行：> 以上为技术面诊断，基于历史行情计算，不构成投资建议。
"""

CONTEXT_PROMPT = """你是期货市场的**消息面与情绪研究员**，为一个本地模拟交易平台写品种诊断。

你会收到一份**已经抓好并算好的事实清单**（JSON），分「消息面与情绪」「技术与行情」两部分。铁律：
1. 只用清单里的数字与快讯原文。清单没有的，写"数据未获取"；
   绝不用记忆补新闻、补政策、补价格、补库存数字。
2. 每个判断后面用括号标出依据数字**及其日期/时间**，例如
   "（VIX 20.83，2026-11-03，近一年分位 84.8%）""（COMEX 黄金 +2.71%，11-04 16:39）"。
3. 严格区分「事实」与「推断」：推断必须写"可能 / 倾向于 / 需要确认"，不得当成结论。
4. 口径必须如实说明：
   * 「情绪温度」是本地按公开公式加权的**复合代理指标**，不是 CBOE VIX；
     VIX 才是真实指数，两者不要混为一谈。
   * 快讯倾向来自**关键词词典**，不是语义模型、不是研报观点。
   * 消息面数据来自公开第三方接口，可能延迟或缺失。
5. 不给买卖建议、不给目标价、不预测涨跌方向、不承诺收益。
6. **篇幅分配是第一~四节合计不少于 65%**：把消息、恐慌指数、跨市场、情绪与资金
   串成一条有因果但不臆断的叙事。第五节只写一段，用技术面数字**交叉校验**
   消息面（一致 / 背离 / 尚无对应），不许复述均线、ATR 凑字数。
7. 中文 Markdown，六个二级标题，总长度 800–1000 字：
   ## 一、消息面主线
      写本品种相关的快讯：引用具体标题或内容、标时间、标线索类型（板块/宏观/定向），
      并说明偏多偏空计数与关键词判定方式；至少引用 3 条，不要泛泛而谈。
   ## 二、恐慌指数与跨市场避险
      写 VIX 最新值/日期/近 1·5·20 日变化/近一年分位/分级，再写黄金、白银、原油、
      美元指数、中美股指的涨跌，说明哪些组合指向避险升温、哪些指向风险偏好回升。
   ## 三、情绪温度与资金行为
      写温度分值与等级，逐个说明参与计算的分量、取值和权重，列出未获取的分量；
      结合持仓量变化、成交量变化、近 5 日涨跌解释"增仓/减仓 + 涨跌"的组合含义。
   ## 四、供需、库存与事件日历
      写基差、库存（有则写数值与日期，无则明确写未获取）、今日高重要性财经事件
      （时间/地区/事件/预期值），并指出哪个事件最可能改变当前叙事。
   ## 五、技术面交叉校验（只写一段）
      用区间涨跌、均线排列、ADX、ATR、量能比、关键位置，判断技术面与消息面
      是一致、背离还是样本太短无法判断，给出依据数字。
   ## 六、需要盯的变量与数据缺口
      列出 3–5 个下一步最该盯的变量（必须是清单里出现过的量），
      以及本次未获取、因此结论受限的数据项。
   最后单独一行：> 以上为公开数据整理与解读，不构成投资建议。
"""


# --------------------------------------------------------------------------
# 指标
# --------------------------------------------------------------------------
def _last(values) -> float | None:
    for value in reversed(values):
        if value is not None:
            return value
    return None


def _pct(value: float | None, base: float | None, digits: int = 3) -> float | None:
    if value is None or not base:
        return None
    return round((value / base - 1) * 100, digits)


def _adx(bars: list[dict], period: int = 14) -> dict:
    """Wilder ADX / +DI / -DI。

    平台原有的 `indicators` 只有 MA/ATR/RSI，趋势强度判断要么看均线排列（滞后），
    要么自己数涨跌 bar（噪声大）。ADX 是判断"有没有趋势"的标准口径，值得单独实现。

    平滑方式与 Wilder 原文一致：首值用 period 累加，之后递推
    `smooth = smooth - smooth/period + current`，避免用简单均值导致数值偏小。
    """
    if len(bars) < period * 2 + 1:
        return {"adx": None, "plusDi": None, "minusDi": None, "bars": len(bars)}

    true_ranges: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, len(bars)):
        high, low = bars[i]["high"], bars[i]["low"]
        prev_high, prev_low, prev_close = bars[i - 1]["high"], bars[i - 1]["low"], bars[i - 1]["close"]
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        up_move = high - prev_high
        down_move = prev_low - low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)

    if len(true_ranges) < period + 1:
        return {"adx": None, "plusDi": None, "minusDi": None, "bars": len(bars)}

    tr_smooth = sum(true_ranges[:period])
    plus_smooth = sum(plus_dm[:period])
    minus_smooth = sum(minus_dm[:period])
    dx_values: list[float] = []
    for i in range(period, len(true_ranges)):
        tr_smooth = tr_smooth - tr_smooth / period + true_ranges[i]
        plus_smooth = plus_smooth - plus_smooth / period + plus_dm[i]
        minus_smooth = minus_smooth - minus_smooth / period + minus_dm[i]
        if tr_smooth <= 0:
            continue
        plus_di = 100 * plus_smooth / tr_smooth
        minus_di = 100 * minus_smooth / tr_smooth
        total = plus_di + minus_di
        dx_values.append(100 * abs(plus_di - minus_di) / total if total else 0.0)

    if not dx_values:
        return {"adx": None, "plusDi": None, "minusDi": None, "bars": len(bars)}

    # ADX 本身再做一次 Wilder 平滑（至少凑够 period 个 DX 才有意义）
    adx_value = sum(dx_values[:period]) / min(period, len(dx_values))
    for value in dx_values[period:]:
        adx_value = (adx_value * (period - 1) + value) / period

    return {
        "adx": round(adx_value, 2),
        "plusDi": round(plus_di, 2),
        "minusDi": round(minus_di, 2),
        "bars": len(bars),
    }


def _alignment(ma5, ma10, ma20, ma60) -> str:
    """均线排列。只做"多头/空头/缠绕"三态，别造更多名词。"""
    values = [v for v in (ma5, ma10, ma20, ma60) if v is not None]
    if len(values) < 3:
        return "样本不足"
    if ma5 is not None and ma10 is not None and ma20 is not None and ma5 > ma10 > ma20 and (ma60 is None or ma20 > ma60):
        return "多头排列"
    if ma5 is not None and ma10 is not None and ma20 is not None and ma5 < ma10 < ma20 and (ma60 is None or ma20 < ma60):
        return "空头排列"
    return "均线缠绕"


def _strength(adx_value: float | None) -> str:
    if adx_value is None:
        return "样本不足"
    if adx_value >= 40:
        return "强趋势"
    if adx_value >= 25:
        return "有趋势"
    if adx_value >= 20:
        return "趋势偏弱"
    return "震荡"


def _streak(closes: list[float]) -> dict:
    """末尾连续同向 bar 数：判断"是不是一路单边推到样本末端"。"""
    count = 0
    sign = 0
    for i in range(len(closes) - 1, 0, -1):
        step = closes[i] - closes[i - 1]
        if step == 0:
            break
        current = 1 if step > 0 else -1
        if sign == 0:
            sign = current
        elif current != sign:
            break
        count += 1
    return {"direction": "up" if sign > 0 else ("down" if sign < 0 else "flat"), "bars": count}


# --------------------------------------------------------------------------
# 快照
# --------------------------------------------------------------------------
def snapshot(symbol: str, *, period: str = DEFAULT_PERIOD, limit: int = DEFAULT_BARS) -> dict:
    """纯本地计算的品种技术面快照（不调用模型、不抓宏观）。"""
    inst = instruments.guess(symbol)
    if not inst:
        raise ValueError("无法识别该品种，请从列表中选择，例如 rb2601 / 螺纹钢。")

    payload = klines.fetch(symbol, period=period, limit=limit)
    bars = payload.get("bars") or []
    if len(bars) < MIN_BARS:
        raise ValueError(f"该品种只取到 {len(bars)} 根 K 线，不足以做诊断（至少需要 {MIN_BARS} 根）。")

    closes = [bar["close"] for bar in bars]
    highs = [bar["high"] for bar in bars]
    lows = [bar["low"] for bar in bars]
    volumes = [bar.get("volume") or 0 for bar in bars]
    last_close = closes[-1]

    ma = {name: _last(indicators.sma(closes, span)) for name, span in
          (("ma5", 5), ("ma10", 10), ("ma20", 20), ("ma60", 60))}
    ma20_series = indicators.sma(closes, 20)
    ma20_prev = next((value for value in reversed(ma20_series[:-20]) if value is not None), None) \
        if len(ma20_series) > 20 else None

    atr_series = indicators.atr(bars, 14)
    atr_value = _last(atr_series)
    trend = _adx(bars)

    # 波动：用 bar 振幅（而不是收盘价标准差），分钟线上更贴近真实可交易幅度
    ranges = [(bar["high"] - bar["low"]) / bar["close"] * 100 for bar in bars if bar["close"]]
    avg_range = statistics.fmean(ranges) if ranges else 0.0
    moves = [(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, len(closes)) if closes[i - 1]]
    max_up = max(moves) if moves else 0.0
    max_down = min(moves) if moves else 0.0

    # 跳空：分钟线里开盘偏离上一根收盘超过 0.2% 基本只出现在夜盘/日盘切换
    gaps = []
    for i in range(1, len(bars)):
        prev_close = bars[i - 1]["close"]
        if not prev_close:
            continue
        change = (bars[i]["open"] / prev_close - 1) * 100
        if abs(change) >= 0.2:
            gaps.append({"timestamp": bars[i]["timestamp"], "pct": round(change, 3)})

    recent_volume = volumes[-20:]
    base_volume = volumes[-80:-20] or volumes
    avg_recent = statistics.fmean(recent_volume) if recent_volume else 0.0
    avg_base = statistics.fmean(base_volume) if base_volume else 0.0

    window_high, window_low = max(highs), min(lows)
    recent_high, recent_low = max(highs[-40:]), min(lows[-40:])

    def distance(target: float | None) -> float | None:
        return _pct(last_close, target) if target else None

    return {
        "available": True,
        "symbol": symbol,
        "instrument": inst.to_dict(),
        "period": period,
        "periodLabel": payload.get("periodLabel") or period,
        "continuous": payload.get("continuous"),
        "source": payload.get("source"),
        "fetchedAt": payload.get("fetchedAt"),
        "range": {
            "startTime": bars[0]["timestamp"],
            "endTime": bars[-1]["timestamp"],
            "bars": len(bars),
            "firstClose": closes[0],
            "lastClose": last_close,
            "changePct": _pct(last_close, closes[0]),
            "high": window_high,
            "low": window_low,
            "amplitudePct": _pct(window_high, window_low),
            "positionPct": round((last_close - window_low) / (window_high - window_low) * 100, 1)
            if window_high > window_low else None,
        },
        "trend": {
            **ma,
            "alignment": _alignment(ma["ma5"], ma["ma10"], ma["ma20"], ma["ma60"]),
            "ma20SlopePct": _pct(ma["ma20"], ma20_prev),
            "adx": trend["adx"],
            "plusDi": trend["plusDi"],
            "minusDi": trend["minusDi"],
            "strength": _strength(trend["adx"]),
            "streak": _streak(closes),
            "last40ChangePct": _pct(closes[-1], closes[-41]) if len(closes) > 40 else None,
        },
        "volatility": {
            "atr14": round(atr_value, 6) if atr_value is not None else None,
            "atrPct": round(atr_value / last_close * 100, 3) if atr_value and last_close else None,
            "avgBarRangePct": round(avg_range, 3),
            "maxUpBarPct": round(max_up, 3),
            "maxDownBarPct": round(max_down, 3),
            "gapCount": len(gaps),
            "gaps": gaps[-5:],
        },
        "volume": {
            "avg20": round(avg_recent, 2),
            "avgBase": round(avg_base, 2),
            "ratio": round(avg_recent / avg_base, 3) if avg_base else None,
            "last": round(volumes[-1], 2),
            "zeroVolumeBars": len([v for v in volumes[-60:] if not v]),
        },
        "levels": {
            "windowHigh": window_high,
            "windowLow": window_low,
            "recentHigh": recent_high,
            "recentLow": recent_low,
            "ma5": ma["ma5"],
            "ma10": ma["ma10"],
            "ma20": ma["ma20"],
            "ma60": ma["ma60"],
            "toWindowHighPct": distance(window_high),
            "toWindowLowPct": distance(window_low),
            "toRecentHighPct": distance(recent_high),
            "toRecentLowPct": distance(recent_low),
            "toMa20Pct": distance(ma["ma20"]),
            "toMa60Pct": distance(ma["ma60"]),
        },
        "notice": NOTICE,
        "generatedAt": datetime.now(marketdata.CN_TZ).isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------
# 交给模型
# --------------------------------------------------------------------------
def _facts(snap: dict) -> dict:
    inst = snap.get("instrument") or {}
    trend = snap.get("trend") or {}
    vol = snap.get("volatility") or {}
    levels = snap.get("levels") or {}
    rng = snap.get("range") or {}
    volume = snap.get("volume") or {}
    streak = trend.get("streak") or {}
    return {
        "品种": f"{inst.get('name')}（{str(inst.get('code') or '').upper()} · {inst.get('exchangeName')}）",
        "板块": inst.get("sector"),
        "合约写法": snap.get("symbol"),
        "行情来源": snap.get("source"),
        "主力连续": snap.get("continuous"),
        "样本": {
            "周期": snap.get("periodLabel"),
            "bar数": rng.get("bars"),
            "起": rng.get("startTime"),
            "止": rng.get("endTime"),
            "首收盘": rng.get("firstClose"),
            "末收盘": rng.get("lastClose"),
            "区间涨跌(%)": rng.get("changePct"),
            "区间最高": rng.get("high"),
            "区间最低": rng.get("low"),
            "区间振幅(%)": rng.get("amplitudePct"),
            "当前价在区间中的位置(%)": rng.get("positionPct"),
        },
        "趋势": {
            "均线排列": trend.get("alignment"),
            "MA5": trend.get("ma5"), "MA10": trend.get("ma10"),
            "MA20": trend.get("ma20"), "MA60": trend.get("ma60"),
            "MA20斜率(%)": trend.get("ma20SlopePct"),
            "ADX14": trend.get("adx"), "+DI": trend.get("plusDi"), "-DI": trend.get("minusDi"),
            "趋势强度判定": trend.get("strength"),
            "末端连续同向": f"{streak.get('direction')} {streak.get('bars')} 根",
            "近40根涨跌(%)": trend.get("last40ChangePct"),
        },
        "波动": {
            "ATR14": vol.get("atr14"),
            "ATR/价格(%)": vol.get("atrPct"),
            "平均单根振幅(%)": vol.get("avgBarRangePct"),
            "最大单根上涨(%)": vol.get("maxUpBarPct"),
            "最大单根下跌(%)": vol.get("maxDownBarPct"),
            "跳空次数(≥0.2%)": vol.get("gapCount"),
            "最近跳空": vol.get("gaps"),
        },
        "量能": {
            "近20根均量": volume.get("avg20"),
            "前60根均量": volume.get("avgBase"),
            "量能比": volume.get("ratio"),
            "最后一根量": volume.get("last"),
            "近60根零成交bar数": volume.get("zeroVolumeBars"),
        },
        "关键位置": {
            "样本最高": levels.get("windowHigh"), "样本最低": levels.get("windowLow"),
            "近40根最高": levels.get("recentHigh"), "近40根最低": levels.get("recentLow"),
            "距样本最高(%)": levels.get("toWindowHighPct"),
            "距样本最低(%)": levels.get("toWindowLowPct"),
            "距MA20(%)": levels.get("toMa20Pct"),
            "距MA60(%)": levels.get("toMa60Pct"),
        },
    }


def _context_facts(symbol: str) -> tuple[dict | None, dict | None, str | None]:
    """抓消息面 / 情绪面板（15 分钟缓存）。抓不到不算失败——技术面仍可出报告。"""
    try:
        panel = context.collect(symbol)
        return context.facts(panel), panel, "缓存" if panel.get("cached") else "实时抓取"
    except Exception as exc:                       # 外部接口失败不能拖垮诊断
        logger.info("品种诊断抓取消息面失败：%s", exc)
        return None, None, None


def analyze(symbol: str, *, period: str = DEFAULT_PERIOD,
            include_macro: bool = True, include_tech: bool = True) -> dict:
    """消息面 / 情绪（默认主体）+ 技术面校验 → 模型写诊断正文。

    `include_macro` 是历史参数名，实际开关的是 `context.py` 的消息面面板；
    `include_tech=False` 时只给模型一份技术面概要，用于「只看消息面」模式。
    """
    snap = snapshot(symbol, period=period)
    context_facts, context_panel, context_note = (None, None, None)
    if include_macro:
        context_facts, context_panel, context_note = _context_facts(symbol)

    facts: dict = {}
    if context_facts:
        facts["消息面与情绪"] = context_facts
    if include_tech:
        facts["技术与行情"] = _facts(snap)
    else:
        facts["技术与行情"] = {
            "说明": "本次只看消息面，完整技术面指标未纳入",
            "样本周期": snap.get("periodLabel"),
            "区间涨跌(%)": (snap.get("range") or {}).get("changePct"),
        }

    # 只有「只看技术面」或消息面确实抓不到时，才回退到原来的技术面六段骨架
    if not context_facts:
        prompt, scope = TECH_ONLY_PROMPT, "只看技术面"
    elif not include_tech:
        prompt, scope = CONTEXT_PROMPT, "只看消息面"
    else:
        prompt, scope = CONTEXT_PROMPT, "消息面 + 技术面"

    # deepseek-flash 这类推理模型的 max_tokens 含内部思考；消息面事实清单更长、
    # 要求写六段，4000 容易被推理耗光导致 content 为空，这里放宽到 8000
    result = ai.complete(prompt, json.dumps(facts, ensure_ascii=False, indent=1),
                         temperature=0.3,
                         max_tokens=8000 if context_facts else 4000)
    return {**snap, "analysis": result["text"], "model": result["model"],
            "provider": result["provider"], "facts": facts, "scope": scope,
            # 前端 AI 诊断后会用 context 直接刷新消息面板，避免两次抓取
            "context": context_panel,
            "contextAttached": bool(context_facts), "contextSource": context_note,
            # 兼容旧前端与自检：macroAttached 现在等价于「是否附上消息面」
            "macroAttached": bool(context_facts), "macroSource": context_note,
            "analyzedAt": datetime.now(marketdata.CN_TZ).isoformat(timespec="seconds")}
