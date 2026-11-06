"""智能问答：DeepSeek / GLM 的 OpenAI 兼容接口。

安全问题：API Key 只在服务端使用，永不回传给浏览器；只回传"是否已配置"。
上下文修正：旧实现读的是已废弃的单例模拟盘表，导致明明跑完一整段回放
仍回答"当前没有模拟盘"。现在统一走 db.latest_state()，与仪表盘同源。
"""
from __future__ import annotations

import json
import logging
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import config, db, engine

logger = logging.getLogger(__name__)

# 服务内存中的临时覆盖（页面表单提交），不落盘
_runtime: dict[str, str] = {"provider": "", "key": "", "model": ""}

SYSTEM_PROMPT = (
    "你是期货量化模拟平台的分析助手。你能解释策略规则、指标口径、回测与模拟结果，"
    "也能依据下面的平台数据分析某条策略表现好或不好的**具体原因**，并给出参数层面的改进方向。"
    "平台状态上下文：{context}。"
    "硬性边界：不给具体买卖建议、不预测点位或收益、不承诺盈利；"
    "上下文里没有的数字一律不要猜，直接说当前没有这项数据。"
    "所有结果都来自模拟或历史行情，必须说明不等同实盘。用简洁清楚的中文回答。"
)


def settings() -> dict:
    """解析当前生效的服务商 / Key / 模型。"""
    provider = _runtime["provider"] or config.default_ai_provider()
    spec = config.AI_PROVIDERS[provider]
    key = os.environ.get(spec["key_env"], "").strip()
    model = (os.environ.get(spec["model_env"], "").strip() or spec["default_model"])
    if _runtime["provider"] == provider:
        key = _runtime["key"] or key
        model = _runtime["model"] or model
    source = "memory" if (_runtime["key"] and _runtime["provider"] == provider) else ("environment" if key else None)
    return {"provider": provider, "label": spec["label"], "key": key, "model": model, "source": source,
            "endpoint": spec["endpoint"]}


def status() -> dict:
    current = settings()
    return {"configured": bool(current["key"]), "provider": current["provider"],
            "providerLabel": current["label"], "model": current["model"],
            "keySource": current["source"],
            "supported": [{"value": name, "label": spec["label"], "defaultModel": spec["default_model"]}
                          for name, spec in config.AI_PROVIDERS.items()]}


def configure(payload: dict) -> dict:
    provider = str(payload.get("provider", config.default_ai_provider())).strip().lower()
    if provider not in config.AI_PROVIDERS:
        raise ValueError("请选择 DeepSeek 或 GLM。")
    spec = config.AI_PROVIDERS[provider]
    key = str(payload.get("apiKey", "")).strip() or os.environ.get(spec["key_env"], "").strip()
    if not 20 <= len(key) <= 300:
        raise ValueError("请填写有效 API Key，或先在 .env 中配置对应服务商的 Key。")
    model = str(payload.get("model", "")).strip()
    if not model or len(model) > 100:
        raise ValueError("请填写有效的模型名称。")
    _runtime.update({"provider": provider, "key": key, "model": model})
    return {"ok": True, **status()}


def _context() -> dict:
    """给模型的"当前平台状态"摘要：只放必要字段，避免超长和敏感信息。

    覆盖三类事实：当前运行的进度、最近一次区间模拟的结果、策略库里有哪些参数。
    只喂这些结构化数字，模型才可能回答"我这条策略为什么亏"这种问题；
    以前只给一个运行状态，它只能泛泛而谈。
    """
    from . import instruments

    context: dict = {"策略数量": db.stats()["strategies"]}

    latest = db.latest_state()
    if latest:
        state, total = latest
        context["当前模拟盘"] = {
            "状态": state.get("status"),
            "策略": state.get("strategyName"),
            "合约": state.get("symbol"),
            "进度": f"{state.get('cursor', 0)}/{total} 根 bar",
            "当前时间": state.get("currentTime"),
            "权益": state.get("equity"),
            "已实现盈亏": state.get("realizedPnl"),
            "未实现盈亏": state.get("unrealizedPnl"),
            "最大回撤%": state.get("maxDrawdownPct"),
            "已平仓笔数": len(state.get("trades", [])),
        }
    else:
        context["当前模拟盘"] = "没有正在运行的模拟盘"

    # 最近一次区间模拟（tradePrefix=SIM）：问答里"我上次跑的那条策略"指的就是它
    for state, total in db.list_trading_run_states():
        if state.get("tradePrefix") != "SIM":
            continue
        trades = state.get("trades") or []
        wins = [t for t in trades if (t.get("netPnl") or 0) > 0]
        losses = [t for t in trades if (t.get("netPnl") or 0) < 0]
        net = sum(t.get("netPnl") or 0 for t in trades)
        symbol = state.get("symbol") or ""
        inst = instruments.guess(symbol)
        context["最近一次区间模拟"] = {
            "策略": state.get("strategyName"),
            "合约": symbol,
            "品种": inst.name if inst else None,
            "乘数": inst.multiplier if inst else None,
            "bar 数": total,
            "成交笔数": len(trades),
            "盈/亏笔数": f"{len(wins)}/{len(losses)}",
            "净盈亏": round(net, 2),
            "手续费合计": round(sum(t.get("fees") or 0 for t in trades), 2),
            "最大回撤%": state.get("maxDrawdownPct"),
            # 用 public_config 还原百分数口径，否则模型会把 0.4% 读成 0.004%
            "参数": engine.public_config(state.get("config") or {}) if state.get("config") else None,
        }
        break

    # 策略库只给参数，不给整包 payload，控制上下文长度
    context["策略库"] = [
        {"名称": item.get("name"), "品种": item.get("symbol"),
         "均线": f"{item.get('maType', 'EMA')}({item.get('fast')}/{item.get('slow')})",
         "ATR过滤": item.get("atr"), "ATR周期": item.get("atrPeriod"),
         "RSI过滤": item.get("rsi")}
        for item in db.list_strategies()[:8]
    ]

    # 最近一篇诊断报告的结论，方便回答"上次报告说我什么"
    reports = db.list_ai_reports(3)
    if reports:
        context["最近诊断报告"] = [{"日期": r["date"], "合约": r["symbol"],
                              "结论": r.get("summary")} for r in reports]
    return context


def _clean_messages(raw) -> list[dict]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 16:
        raise ValueError("对话消息数量无效（1–16 条），请重新发送。")
    cleaned = []
    for message in raw:
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
            continue
        content = str(message.get("content", "")).strip()
        if content and len(content) <= 3000:
            cleaned.append({"role": message["role"], "content": content})
    if not any(m["role"] == "user" for m in cleaned):
        raise ValueError("请输入有效问题。")
    return cleaned


def _post(current: dict, body: dict) -> dict:
    """发一次请求并返回原始 JSON。错误统一翻译成人能看懂的中文。"""
    request = Request(current["endpoint"], data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                      method="POST",
                      headers={"Authorization": "Bearer " + current["key"],
                               "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=90) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise ValueError(f"{current['label']} 鉴权失败，请检查 Key 与模型权限。") from None
        if exc.code == 429:
            raise ValueError(f"{current['label']} 当前限流或额度不足，请稍后重试。") from None
        if exc.code == 400:
            raise ValueError(f"{current['label']} 拒绝了这次请求（模型名或不支持的特性有误）。") from None
        logger.warning("AI 服务 HTTP %s: %s", exc.code, exc.reason)
        raise ValueError(f"{current['label']} 服务返回 HTTP {exc.code}，请稍后重试。") from None
    except (URLError, TimeoutError):
        raise ValueError("无法连接当前 AI 服务，请检查网络后重试。") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("AI 服务返回内容无法解析，请重试。") from None


def _answer_of(payload: dict, current: dict) -> str:
    try:
        answer = payload["choices"][0]["message"]["content"]
        if isinstance(answer, list):   # 少数兼容实现返回分段结构
            answer = "".join(part.get("text", "") for part in answer if isinstance(part, dict))
        answer = str(answer).strip()
    except (KeyError, IndexError, TypeError):
        raise ValueError("AI 服务返回结构无效，请稍后重试。") from None
    if not answer:
        raise ValueError("AI 服务未返回文本，请重试。")
    return answer


def complete(system: str, user: str, *, temperature: float = 0.3, max_tokens: int = 1600,
             json_mode: bool = False) -> dict:
    """单轮补全，供策略生成 / 诊断报告 / 宏观分析复用。

    json_mode 会要求服务商返回严格 JSON（DeepSeek 与 GLM 都支持 response_format）。
    返回 {text, model, provider, usage, finishReason}，调用方自己解析 text。

    ⚠️ max_tokens 是**含推理**的总额度：deepseek-flash 这类推理模型会先花掉上千个
    completion token 做内部思考，额度给小了正文会被直接挤断（finish_reason=length），
    而且思考内容不返回，看起来就像"模型只写了一半"。所以这类长文本任务按 4000 起步。
    """
    current = settings()
    if not current["key"]:
        raise ValueError(f"尚未配置 {current['label']} API Key，请到「智能问答」页填写或写入 .env。")
    body = {
        "model": current["model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature, "max_tokens": max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    payload = _post(current, body)
    choice = (payload.get("choices") or [{}])[0]
    return {"text": _answer_of(payload, current), "model": current["model"],
            "provider": current["provider"], "usage": payload.get("usage", {}),
            "finishReason": choice.get("finish_reason")}


def parse_json(text: str) -> dict:
    """从模型输出里抠出 JSON。

    即使开了 json_mode，个别兼容实现仍会包一层 ```json 代码块，这里兜住。
    """
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1] if "```" in cleaned[3:] else cleaned[3:]
        if cleaned.lstrip().lower().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
    cleaned = cleaned.strip().strip("`").strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("AI 返回的内容不是有效 JSON，请重试或换个说法。") from None
        try:
            data = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            raise ValueError("AI 返回的内容不是有效 JSON，请重试或换个说法。") from None
    if not isinstance(data, dict):
        raise ValueError("AI 返回的 JSON 结构不对，请重试。")
    return data


def chat(raw_messages) -> dict:
    current = settings()
    if not current["key"]:
        raise ValueError(f"尚未配置 {current['label']} API Key，请在智能问答页填写或写入 .env。")
    messages = _clean_messages(raw_messages)
    system = SYSTEM_PROMPT.format(context=json.dumps(_context(), ensure_ascii=False))
    payload = _post(current, {"model": current["model"],
                              "messages": [{"role": "system", "content": system}, *messages],
                              "temperature": 0.3, "max_tokens": 1200})
    answer = _answer_of(payload, current)
    return {"answer": answer[:8000], "model": current["model"], "provider": current["provider"],
            "usage": payload.get("usage", {})}
