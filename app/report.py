"""每日 AI 诊断报告：把一天的成交摊开，让模型按固定骨架做复盘，并支持导出。

和 views.diagnostics() 的区别
-----------------------------
diagnostics() 是**纯计算**的体检表（胜率、回撤这些数），适合印在页面上；
本模块是**带解释**的复盘：同样的数字，回答的是"今天哪几笔做得好、哪几笔是失误、
参数该怎么改"。前者不会错，后者会有判断，所以生成时把事实快照一并存进 meta_json，
日后可以核对"当时是依据什么写下的这段话"。

报告骨架固定为五段（概览 / 妙手 / 失误点 / 改进建议 / 明日观察），
目的是让每天的报告能横向比较，而不是每天换一种写法。
"""
from __future__ import annotations

import html
import json
import logging
import uuid
from datetime import datetime

from . import ai, db, engine

logger = logging.getLogger(__name__)

NOTICE = ("报告基于模拟盘成交记录生成，只评价策略表现，不构成投资建议；"
          "模型可能看错数据，关键数字请以页面统计为准。")

SECTIONS = ("今日概览", "妙手", "失误点", "策略改进建议", "明日观察")
SKELETON = "\n".join(f"## {name}" for name in SECTIONS)

REPORT_PROMPT = f"""你是期货交易复盘教练，负责给交易员写当日复盘。

你会收到一份 JSON：当天（或该段行情）的成交明细与统计。铁律：
1. **只依据 JSON 里的数据**。没成交就写"今日无成交"，不要编造交易；不要引用没给的行情。
2. 引用具体证据：哪一笔、几点几分、什么价格、盈亏多少。空泛的"要注意风险"没有价值。
3. 不给买卖建议、不给点位、不预测涨跌、不承诺收益。
4. 评价要具体到策略规则层面：信号时机、过滤条件（ATR/RSI）、持仓时长、费用占比。
5. 用中文 Markdown，严格按这个骨架输出，五个二级标题一个都不能少：
{SKELETON}
6. 每段 2–5 句，全篇 600–900 字。最后加一行：> 本报告由 AI 依据模拟数据生成，不构成投资建议。
"""


# --------------------------------------------------------------------------
# 事实准备
# --------------------------------------------------------------------------
def _latest_sim_run() -> str | None:
    for state, _total in db.list_trading_run_states():
        if state.get("tradePrefix") == "SIM":
            return state.get("runId")
    return None


def _pick_run(run_id: str | None) -> dict:
    """取一次运行记录；不指定就用最近一次区间模拟。"""
    target = run_id or _latest_sim_run()
    if not target:
        raise ValueError("还没有模拟记录。请先到「交易 → 模拟盘」跑一段行情，再生成报告。")
    record = db.get_trading_run(target)
    if not record:
        raise ValueError("找不到这条模拟记录，可能已被删除。")
    return record


def _compact_trade(trade: dict) -> dict:
    """只留复盘需要的字段，避免把整个 trade 结构塞进提示词。"""
    return {
        "开仓时间": str(trade.get("entryTime", ""))[:16],
        "开仓价": trade.get("entryPrice"),
        "平仓时间": str(trade.get("exitTime", ""))[:16],
        "平仓价": trade.get("exitPrice"),
        "手数": trade.get("qty"),
        "毛盈亏": trade.get("grossPnl"),
        "手续费": trade.get("fees"),
        "净盈亏": trade.get("netPnl"),
        "退出原因": trade.get("exitReason"),
    }


def build_facts(payload: dict) -> dict:
    """汇总当日（或整段）的成交与统计，供提示词和入库使用。"""
    record = _pick_run(str(payload.get("runId") or "") or None)
    bars = record.get("bars") or []
    all_trades = record.get("trades") or []
    if not bars:
        raise ValueError("这条记录没有保存行情快照，无法复盘。")

    # 默认复盘"最后一个有行情的交易日"，而不是今天——演示行情可能run在历史日期
    default_day = str(bars[-1].get("timestamp", ""))[:10]
    day = str(payload.get("date") or default_day)[:10]
    trades = [t for t in all_trades if str(t.get("exitTime", "")).startswith(day)]
    day_bars = [b for b in bars if str(b.get("timestamp", "")).startswith(day)]
    if not day_bars:
        raise ValueError(f"{day} 这一天没有行情数据，请换一天或换一条记录。")

    # ⚠️ 库里存的是**内部口径**的配置（atrMaxPct 已是小数），
    # 这里再 normalize 一次会把 0.4% 变成 0.004%，报告里就会写出一个错两个数量级的阈值。
    raw_cfg = dict(record.get("config") or {})
    if "atrMaxPct" not in raw_cfg:
        raw_cfg = engine.normalize_config(raw_cfg)
    public_cfg = engine.public_config(raw_cfg)
    wins = [t for t in trades if (t.get("netPnl") or 0) > 0]
    losses = [t for t in trades if (t.get("netPnl") or 0) < 0]
    win_amount = sum(t.get("netPnl") or 0 for t in wins)
    loss_amount = abs(sum(t.get("netPnl") or 0 for t in losses))
    net = sum(t.get("netPnl") or 0 for t in trades)
    fees = sum(t.get("fees") or 0 for t in trades)

    reasons: dict[str, int] = {}
    for trade in trades:
        key = str(trade.get("exitReason") or "未标注")
        reasons[key] = reasons.get(key, 0) + 1

    def hold_minutes(trade: dict) -> int | None:
        try:
            start = datetime.fromisoformat(str(trade.get("entryTime")))
            end = datetime.fromisoformat(str(trade.get("exitTime")))
        except (TypeError, ValueError):
            return None
        return max(0, round((end - start).total_seconds() / 60))

    holds = [value for value in (hold_minutes(t) for t in trades) if value is not None]
    first_close = day_bars[0]["close"]
    last_close = day_bars[-1]["close"]

    stats = {
        "date": day,
        "tradeCount": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "winRatePct": round(len(wins) / len(trades) * 100, 1) if trades else None,
        "netPnl": round(net, 2),
        "grossPnl": round(sum(t.get("grossPnl") or 0 for t in trades), 2),
        "fees": round(fees, 2),
        "feeShareOfGrossPct": (round(fees / abs(sum(t.get("grossPnl") or 0 for t in trades)) * 100, 1)
                               if trades and sum(t.get("grossPnl") or 0 for t in trades) else None),
        "avgWin": round(win_amount / len(wins), 2) if wins else 0,
        "avgLoss": round(sum(t.get("netPnl") or 0 for t in losses) / len(losses), 2) if losses else 0,
        "profitFactor": (round(win_amount / loss_amount, 2) if loss_amount
                         else ("Infinity" if win_amount else None)),
        "bestTrade": round(max((t.get("netPnl") or 0 for t in trades), default=0), 2),
        "worstTrade": round(min((t.get("netPnl") or 0 for t in trades), default=0), 2),
        "avgHoldMinutes": round(sum(holds) / len(holds)) if holds else 0,
        "longestHoldMinutes": max(holds) if holds else 0,
        "exitReasons": reasons,
        "priceChangePct": round((last_close / first_close - 1) * 100, 2) if first_close else None,
        "barCount": len(day_bars),
        "equity": record.get("equity"),
        "maxDrawdownPct": round(float(record.get("maxDrawdownPct") or 0), 2),
        "symbol": record.get("symbol"),
        "strategyName": record.get("strategyName"),
        "runId": record.get("runId"),
        "parameters": {key: public_cfg[key] for key in
                       ("fast", "slow", "maType", "atr", "atrPeriod", "atrMaxPct",
                        "rsi", "rsiPeriod", "rsiUpper", "feePerSide", "slippage", "qty")},
    }
    stats["summaryLine"] = (f"{day} · {stats['tradeCount']} 笔 · 净 {stats['netPnl']:+.2f} · "
                            f"胜率 {stats['winRatePct'] if stats['winRatePct'] is not None else '—'}%")

    signals = [s for s in (record.get("signals") or [])
               if str(s.get("timestamp", "")).startswith(day)]
    return {"stats": stats, "trades": trades, "signals": signals[-12:],
            "record": record, "day": day}


# --------------------------------------------------------------------------
# 生成
# --------------------------------------------------------------------------
def generate(payload: dict) -> dict:
    """生成一份报告并落库（同一天同一运行会覆盖）。"""
    facts = build_facts(payload)
    stats = facts["stats"]

    prompt_facts = {
        "品种合约": stats["symbol"],
        "复盘日期": stats["date"],
        "策略名称": stats["strategyName"],
        "策略参数": stats["parameters"],
        "当日统计": {key: stats[key] for key in
                 ("tradeCount", "wins", "losses", "winRatePct", "netPnl", "grossPnl", "fees",
                  "feeShareOfGrossPct", "avgWin", "avgLoss", "profitFactor", "bestTrade",
                  "worstTrade", "avgHoldMinutes", "longestHoldMinutes", "exitReasons",
                  "priceChangePct", "barCount", "equity", "maxDrawdownPct")},
        "逐笔成交": [_compact_trade(t) for t in facts["trades"]],
        # 引擎里的信号只有 {timestamp, type, reason}，字段名不是 side/price
        "当日信号": [{"时间": str(s.get("timestamp", ""))[11:16],
                  "类型": s.get("type"),
                  "价": s.get("price"),
                  "说明": s.get("reason")} for s in facts["signals"]],
        "说明": "盈亏为模拟账户口径，已扣除手续费与滑点；单边单向做多。",
    }

    content = ""
    result: dict = {}
    # 推理模型会先花掉大量 token 思考，正文容易只写一半，这里校验骨架完整性并重试一次
    for attempt in range(2):
        result = ai.complete(REPORT_PROMPT, json.dumps(prompt_facts, ensure_ascii=False, indent=1),
                             temperature=0.4 + attempt * 0.2, max_tokens=4200)
        content = result["text"].strip()
        missing = [name for name in SECTIONS if name not in content]
        if not missing and len(content) >= 300:
            break
        logger.warning("报告骨架不完整（缺 %s，长度 %s），第 %s 次重试",
                       missing or "无", len(content), attempt + 1)
    else:
        missing = [name for name in SECTIONS if name not in content]
        if missing:
            raise ValueError("AI 返回的报告不完整（缺少：" + "、".join(missing)
                             + "），请稍后重试，或在「智能问答」页换成其他模型。")

    saved = db.save_ai_report({
        "id": str(uuid.uuid4()),
        "date": stats["date"],
        "runId": stats["runId"],
        "symbol": stats["symbol"],
        "strategyId": (facts["record"] or {}).get("strategyId"),
        "strategyName": stats["strategyName"],
        "content": content,
        "meta": {"stats": stats, "model": result["model"], "notice": NOTICE,
                 "headline": stats["summaryLine"],
                 "trades": [_compact_trade(t) for t in facts["trades"]]},
    })
    return get(saved["id"])


def get(report_id: str) -> dict:
    report = db.get_ai_report(report_id)
    if not report:
        raise ValueError("报告不存在。")
    return {**report, "notice": NOTICE}


def listing(limit: int = 30) -> dict:
    return {"items": db.list_ai_reports(limit), "notice": NOTICE}


# --------------------------------------------------------------------------
# 导出
# --------------------------------------------------------------------------
def _filename(report: dict, suffix: str) -> str:
    symbol = str(report.get("symbol") or "")
    safe = "".join(ch for ch in symbol if ch.isalnum()) or "合约"
    return f"诊断报告-{report.get('date')}-{safe}.{suffix}"


def export_markdown(report_id: str) -> tuple[str, str, str]:
    """返回 (文件名, 正文, MIME)。"""
    report = get(report_id)
    stats = (report.get("meta") or {}).get("stats") or {}
    head = [f"# {report['date']} 交易诊断报告", ""]
    if stats.get("strategyName"):
        head.append(f"- 策略：{stats['strategyName']}")
    if stats.get("symbol"):
        head.append(f"- 合约：{stats['symbol']}")
    if stats.get("summaryLine"):
        head.append(f"- 当日：{stats['summaryLine']}")
    head.append("")
    body = "\n".join(head) + report["content"]
    body += "\n\n---\n\n> " + NOTICE + "\n"
    return _filename(report, "md"), body, "text/markdown; charset=utf-8"


def _inline(text: str) -> str:
    """处理行内 **粗体** 与 `代码`，先转义再替换，避免把模型输出的尖括号吃进 HTML。"""
    escaped = html.escape(text, quote=False)
    while "**" in escaped:
        escaped = escaped.replace("**", "<strong>", 1).replace("**", "</strong>", 1)
    while "`" in escaped:
        escaped = escaped.replace("`", "<code>", 1).replace("`", "</code>", 1)
    return escaped


def markdown_to_html(markdown: str) -> str:
    """极小 Markdown 渲染器：标题/列表/引用/分隔线/段落。

    不引第三方库（项目原则是标准库优先），模型输出也只用到这几种语法。
    """
    blocks: list[str] = []
    list_open = False
    for raw in markdown.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            if list_open:
                blocks.append("</ul>")
                list_open = False
            continue
        if stripped.startswith("<"):            # 已有 HTML 片段就原样保留
            blocks.append(stripped)
            continue
        if stripped.startswith("#"):
            level = min(6, len(stripped) - len(stripped.lstrip("#")))
            if list_open:
                blocks.append("</ul>")
                list_open = False
            blocks.append(f"<h{level}>{_inline(stripped[level:].strip())}</h{level}>")
        elif stripped.startswith(("- ", "* ", "+ ")):
            if not list_open:
                blocks.append("<ul>")
                list_open = True
            blocks.append(f"<li>{_inline(stripped[2:])}</li>")
        elif stripped.startswith(">"):
            if list_open:
                blocks.append("</ul>")
                list_open = False
            blocks.append(f"<blockquote>{_inline(stripped.lstrip('> '))}</blockquote>")
        elif stripped in ("---", "***", "___"):
            if list_open:
                blocks.append("</ul>")
                list_open = False
            blocks.append("<hr>")
        else:
            if list_open:
                blocks.append("</ul>")
                list_open = False
            blocks.append(f"<p>{_inline(stripped)}</p>")
    if list_open:
        blocks.append("</ul>")
    return "\n".join(blocks)


HTML_SHELL = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{title}</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ margin: 0; padding: 48px 20px; background: #f4f6fa; color: #1b2430;
         font: 15px/1.75 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }}
  main {{ max-width: 820px; margin: 0 auto; background: #fff; border-radius: 16px;
          padding: 44px 52px; box-shadow: 0 10px 40px #10203a14; }}
  h1 {{ font-size: 26px; margin: 0 0 4px; letter-spacing: -.4px; }}
  h2 {{ font-size: 18px; margin: 34px 0 10px; padding-left: 12px;
        border-left: 4px solid #2563eb; }}
  h3 {{ font-size: 16px; margin: 22px 0 8px; }}
  p {{ margin: 10px 0; }}
  ul {{ margin: 10px 0; padding-left: 22px; }}
  li {{ margin: 5px 0; }}
  blockquote {{ margin: 16px 0; padding: 10px 16px; background: #f1f5fb;
                border-left: 3px solid #94a3b8; color: #47556b; font-size: 14px; }}
  code {{ background: #eef2f8; border-radius: 4px; padding: 1px 5px; font-size: 13px; }}
  hr {{ border: 0; border-top: 1px solid #e3e8f0; margin: 28px 0; }}
  .meta {{ color: #64748b; font-size: 13px; margin-bottom: 22px; }}
  @media print {{
    body {{ background: #fff; padding: 0; }}
    main {{ box-shadow: none; border-radius: 0; padding: 0 8mm; max-width: none; }}
    h2 {{ page-break-after: avoid; }}
  }}
</style></head><body><main>
<div class="meta">{meta}</div>
{body}
</main></body></html>"""


def export_html(report_id: str) -> tuple[str, str, str]:
    report = get(report_id)
    stats = (report.get("meta") or {}).get("stats") or {}
    meta_bits = [f"{report['date']}"]
    if stats.get("symbol"):
        meta_bits.append(str(stats["symbol"]))
    if stats.get("strategyName"):
        meta_bits.append(str(stats["strategyName"]))
    if stats.get("summaryLine"):
        meta_bits.append(str(stats["summaryLine"]))
    page = HTML_SHELL.format(
        title=f"{report['date']} 交易诊断报告",
        meta=" · ".join(html.escape(bit) for bit in meta_bits),
        body=markdown_to_html(report["content"]) + f"\n<hr>\n<blockquote>{html.escape(NOTICE)}</blockquote>")
    return _filename(report, "html"), page, "text/html; charset=utf-8"
