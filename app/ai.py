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

from . import config, db

logger = logging.getLogger(__name__)

# 服务内存中的临时覆盖（页面表单提交），不落盘
_runtime: dict[str, str] = {"provider": "", "key": "", "model": ""}

SYSTEM_PROMPT = (
    "你是期货量化模拟平台的产品问答助手，只解释策略规则、指标、回测与模拟账户状态。"
    "平台状态上下文：{context}。"
    "如果上下文没有相关数据，明确说当前没有数据，不要猜测。"
    "你不得给出具体买卖建议、预测收益或承诺盈利；所有结果都要注明是模拟数据，不等同实盘。"
    "用清楚简洁的中文回答。"
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
    """给模型的"当前平台状态"摘要：只放必要字段，避免超长和敏感信息。"""
    latest = db.latest_state()
    if not latest:
        return {"paper_status": "当前没有已启动的模拟盘，也没有回放记录。"}
    state, total = latest
    return {
        "paper_status": state.get("status"),
        "strategy": state.get("strategyName"),
        "symbol": state.get("symbol"),
        "progress": f"{state.get('cursor', 0)}/{total} 根 bar",
        "current_time": state.get("currentTime"),
        "initial_capital": state.get("config", {}).get("capital"),
        "equity": state.get("equity"),
        "realized_pnl": state.get("realizedPnl"),
        "unrealized_pnl": state.get("unrealizedPnl"),
        "max_drawdown_pct": state.get("maxDrawdownPct"),
        "closed_trade_count": len(state.get("trades", [])),
        "recent_signals": state.get("signals", [])[-8:],
        "strategy_count": db.stats()["strategies"],
    }


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


def chat(raw_messages) -> dict:
    current = settings()
    if not current["key"]:
        raise ValueError(f"尚未配置 {current['label']} API Key，请在智能问答页填写或写入 .env。")
    messages = _clean_messages(raw_messages)
    system = SYSTEM_PROMPT.format(context=json.dumps(_context(), ensure_ascii=False))
    body = json.dumps({"model": current["model"],
                       "messages": [{"role": "system", "content": system}, *messages],
                       "temperature": 0.3, "max_tokens": 1200}, ensure_ascii=False).encode("utf-8")
    request = Request(current["endpoint"], data=body, method="POST",
                      headers={"Authorization": "Bearer " + current["key"],
                               "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise ValueError(f"{current['label']} 鉴权失败，请检查 Key 与模型权限。") from None
        if exc.code == 429:
            raise ValueError(f"{current['label']} 当前限流或额度不足，请稍后重试。") from None
        logger.warning("AI 服务 HTTP %s: %s", exc.code, exc.reason)
        raise ValueError(f"{current['label']} 服务返回 HTTP {exc.code}，请稍后重试。") from None
    except (URLError, TimeoutError):
        raise ValueError("无法连接当前 AI 服务，请检查网络后重试。") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("AI 服务返回内容无法解析，请重试。") from None

    try:
        answer = payload["choices"][0]["message"]["content"]
        if isinstance(answer, list):   # 少数兼容实现返回分段结构
            answer = "".join(part.get("text", "") for part in answer if isinstance(part, dict))
        answer = str(answer).strip()
    except (KeyError, IndexError, TypeError):
        raise ValueError("AI 服务返回结构无效，请稍后重试。") from None
    if not answer:
        raise ValueError("AI 服务未返回文本，请重试。")
    return {"answer": answer[:8000], "model": current["model"], "provider": current["provider"],
            "usage": payload.get("usage", {})}
