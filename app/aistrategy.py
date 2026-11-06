"""AI 策略生成：把一句人话翻译成"改了哪几个参数"，并强制走人工确认。

设计原则（这是本模块全部价值所在）
----------------------------------
1. **模型只能改白名单参数**。合约乘数、最小变动是合约属性，让模型"调一下"
   等于把盈亏算错一个数量级，所以这类字段直接不给它。
2. **以 config 为准重算 diff**。模型很爱在 changes 里编造"我把 X 从 1 改成了 2"，
   实际 config 里没改。这里一律忽略模型自述，用前后快照比出来。
3. **越界值钳制而不是报错**。模型给出 fast=0 时，钳到下限并留一条 warning，
   用户看到"已把 0 修正为 2"比看到一句 400 更有用。
4. **不落库**。draft() 只产出提案，必须前端弹窗确认后才由 /api/strategies 写入，
   而且默认另存新策略，不动原策略。
"""
from __future__ import annotations

import logging

from . import ai, engine, instruments

logger = logging.getLogger(__name__)

NOTICE = ("AI 提案只改参数，不验证盈利能力。采纳前请用同一段行情跑一次模拟，"
          "与旧参数对比后再决定是否入库。")

# 字段 -> (中文名, 类型, 下限, 上限, 给模型看的说明)
PARAMS: dict[str, tuple] = {
    "fast": ("快线周期", "int", 2, 200, "短周期均线，越小越敏感、信号越多也越容易假信号"),
    "slow": ("慢线周期", "int", 3, 500, "长周期均线，决定趋势方向，必须大于快线"),
    "maType": ("均线类型", "enum", ("EMA", "SMA"), None, "EMA 反应快，SMA 更平滑"),
    "atr": ("ATR 波动过滤", "bool", None, None, "开启后只在波动够大时才开仓，能过滤横盘噪音"),
    "atrPeriod": ("ATR 周期", "int", 2, 100, "计算平均真实波幅的窗口"),
    "atrMaxPct": ("ATR 上限(%)", "float", 0.01, 10.0, "超过该波动率不开仓，用来躲极端行情"),
    "rsi": ("RSI 过滤", "bool", None, None, "开启后结合相对强弱指标避免追高"),
    "rsiPeriod": ("RSI 周期", "int", 2, 100, "RSI 计算窗口"),
    "rsiUpper": ("RSI 上限", "float", 50.0, 100.0, "RSI 高于该值不追多"),
    "feePerSide": ("单边手续费", "float", 0.0, 500.0, "每手每边的手续费，影响高频策略的可行性"),
    "slippage": ("滑点", "float", 0.0, 200.0, "每次成交的价格损耗，按最小变动价位计"),
    "qty": ("手数", "int", 1, 100, "每次开仓的手数，放大收益也放大回撤"),
    "capital": ("初始资金", "float", 10_000, 100_000_000, "模拟账户起始资金，影响收益率分母"),
    "dailyLossLimit": ("单日亏损上限", "float", 0.0, 1_000_000, "当日已实现亏损超过该值就不再开仓，0 表示关闭"),
}

# 明确不能由模型修改的字段，写进提示词，减少它瞎编
LOCKED = {
    "multiplier": "合约乘数由品种决定（螺纹 10、股指 300…），不是策略参数",
    "tickSize": "最小变动价位同样由合约决定",
    "slippageTicks": "按最小变动价位计价的滑点档位，由品种决定",
}

SYSTEM_PROMPT = """你是期货量化策略的参数调优助手，服务于一个本地纸面模拟平台（不发真实委托）。

你的任务是：读懂用户用中文提出的策略改进诉求，只调整下面这些参数，并解释为什么。

可用参数（键名: 说明 / 取值范围）：
{param_help}

不能修改的字段（改了会算错盈亏）：
{locked_help}

硬性规则：
1. 必须满足 2 ≤ fast < slow ≤ 500；若用户的诉求需要快线不小于慢线，请把两者一起调整。
2. 只能使用上面列出的参数键，不得新增字段、不得输出 multipliers 之类的东西。
3. 不了解的事实不要编造；不要预测收益、不要承诺盈利。
4. config 必须是**完整的**新参数对象（未改动的字段也要原样带上）。
5. 只输出 JSON，不要输出任何解释性文字或代码块标记。

JSON 结构：
{{
  "name": "不超过 20 字的策略名，体现这次改动，例如「EMA 13/34 + ATR 过滤」",
  "summary": "一句话说明这次调整想解决什么问题",
  "config": {{ "fast": 13, "slow": 34, "maType": "EMA", "atr": true, "...": "其余字段完整保留" }},
  "changes": [{{ "param": "fast", "from": 9, "to": 13, "reason": "为什么这么改" }}],
  "expectedEffect": "预计对信号频率/回撤/胜率的影响方向（不要给具体数字或收益承诺）",
  "risks": ["改动可能带来的副作用", "第二条"]
}}"""


def _cast(key: str, value):
    """按白名单把模型给的字符串/数字收敛成正确类型。"""
    label, kind, low, high, _ = PARAMS[key]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "1", "yes", "on", "是", "开")
    if kind == "enum":
        text = str(value).strip().upper()
        return text if text in low else None
    if kind == "int":
        try:
            number = int(round(float(value)))
        except (TypeError, ValueError):
            return None
        return max(int(low), min(int(high), number))
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(max(float(low), min(float(high), number)), 6)


def _param_help() -> str:
    lines = []
    for key, (label, kind, low, high, hint) in PARAMS.items():
        if kind == "bool":
            span = "true/false"
        elif kind == "enum":
            span = "/".join(low)
        elif high is None:
            span = "见说明"
        else:
            span = f"{low}–{high}"
        lines.append(f"- {key}（{label}，取值 {span}）：{hint}")
    return "\n".join(lines)


def _locked_help() -> str:
    return "\n".join(f"- {key}：{why}" for key, why in LOCKED.items())


def _fmt(key: str, value) -> str:
    """把参数值格式化成人能读的展示串。"""
    if key in PARAMS and PARAMS[key][1] == "bool":
        return "开启" if value else "关闭"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _diff(base: dict, proposed: dict) -> list[dict]:
    """以前后快照为准重算差异，完全不信模型自述的 changes。"""
    rows = []
    for key, (label, kind, _low, _high, _hint) in PARAMS.items():
        before, after = base.get(key), proposed.get(key)
        if before is None or after is None or before == after:
            continue
        row = {"key": key, "label": label, "from": before, "to": after,
               "fromText": _fmt(key, before), "toText": _fmt(key, after)}
        if kind in ("int", "float") and isinstance(before, (int, float)) and isinstance(after, (int, float)):
            delta = after - before
            row["delta"] = round(delta, 6)
            row["deltaText"] = f"{delta:+g}"
        rows.append(row)
    return rows


def draft(payload: dict, strategy: dict | None) -> dict:
    """生成一份参数提案（不落库）。前端确认后才写策略库。"""
    prompt = str(payload.get("prompt") or "").strip()
    if not (4 <= len(prompt) <= 800):
        raise ValueError("请用 4–800 字说明你想怎么调整策略。")

    symbol = str(payload.get("symbol") or (strategy or {}).get("symbol") or "").strip()
    inst = instruments.guess(symbol)
    if inst:
        symbol = f"{inst.name} · {inst.code.upper()}"

    # 当前参数：策略库里那份，叠加页面上的临时修改（用户在表单里改过再让 AI 参谋）
    raw_current: dict = {}
    if isinstance(strategy, dict):
        raw_current.update(strategy)
    if isinstance(payload.get("config"), dict):
        raw_current.update(payload["config"])
    base_cfg = engine.public_config(engine.normalize_config(raw_current))

    user_prompt = (
        f"品种：{symbol or '未指定'}\n"
        f"当前参数：{base_cfg}\n"
        f"用户诉求：{prompt}\n"
        "请给出调整后的完整参数与理由。"
    )

    result = ai.complete(SYSTEM_PROMPT.format(param_help=_param_help(),
                                              locked_help=_locked_help()),
                         user_prompt, temperature=0.2, max_tokens=1600, json_mode=True)
    data = ai.parse_json(result["text"])

    warnings: list[str] = []
    proposed_raw = data.get("config") if isinstance(data.get("config"), dict) else {}
    if not proposed_raw:
        raise ValueError("AI 没有返回参数，请换个说法重试。")

    # 只保留白名单字段；模型塞进来的其他键（含 multiplier）一律丢弃并告知
    dropped = [key for key in proposed_raw if key not in PARAMS]
    if dropped:
        warnings.append("已忽略 AI 返回的非法字段：" + "、".join(dropped[:6]))

    proposed: dict = {}
    for key in PARAMS:
        if key not in proposed_raw:
            proposed[key] = base_cfg.get(key)          # 没给就沿用原值，不留空洞
            continue
        raw_value = proposed_raw[key]
        value = _cast(key, raw_value)
        if value is None:
            warnings.append(f"「{PARAMS[key][0]}」的取值 {raw_value!r} 无法识别，已沿用原值。")
            value = base_cfg.get(key)
        elif value != raw_value and not isinstance(raw_value, bool):
            warnings.append(f"「{PARAMS[key][0]}」超出允许范围，已修正为 {_fmt(key, value)}。")
        proposed[key] = value

    # 均线关系是引擎的硬约束，钳制后可能被破坏，这里兜住
    if proposed["fast"] >= proposed["slow"]:
        proposed["slow"] = min(500, proposed["fast"] + 1)
        warnings.append(f"快线不小于慢线，已把慢线调整为 {proposed['slow']}。")

    # 差异以快照为准重算，但**理由**要用模型自己说的：它知道自己在想什么
    reasons: dict[str, str] = {}
    for item in (data.get("changes") or []):
        if isinstance(item, dict):
            key = str(item.get("param") or "").strip()
            reason = str(item.get("reason") or "").strip()
            if key in PARAMS and reason:
                reasons[key] = reason[:300]
    changes = _diff(base_cfg, proposed)
    for row in changes:
        row["reason"] = reasons.get(row["key"], "")
    unexplained = [row["label"] for row in changes if not row["reason"]]
    if unexplained:
        warnings.append("以下改动 AI 没有给出理由：" + "、".join(unexplained[:5]))
    risks = [str(item)[:300] for item in (data.get("risks") or []) if str(item).strip()][:5]
    name = str(data.get("name") or "").strip()[:40] or "AI 调参策略"

    return {
        "base": base_cfg,
        "proposal": {
            "name": name,
            "summary": str(data.get("summary") or "").strip()[:400],
            "expectedEffect": str(data.get("expectedEffect") or "").strip()[:600],
            "config": proposed,
            "changes": changes,
            "risks": risks,
        },
        "symbol": symbol,
        "warnings": warnings,
        "model": result["model"],
        "usage": result.get("usage"),
        "notice": NOTICE,
        # 供前端"确认入库"时直接用，避免它自己拼参数把 locked 字段又填回去
        "savePayload": {
            "name": name,
            "symbol": symbol,
            **proposed,
        },
    }
