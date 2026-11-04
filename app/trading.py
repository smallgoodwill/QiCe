"""模拟运行（paper trading）：创建、单步推进、取消，以及给前端的状态视图。

运行记录逐条独立保存于 SQLite 的 trading_runs 表。浏览器保持打开时前端会定时调用
step 接口推进；即使关闭浏览器，下次打开仍从 cursor 继续。
"""
from __future__ import annotations

from . import config, engine

STATUS_LABELS = {"running": "运行中", "paused": "已暂停", "completed": "已完成", "stopped": "已取消"}


def create_run(payload: dict, strategy: dict) -> engine.TradingSession:
    bars = engine.validate_bars(payload.get("bars"))
    cfg = engine.normalize_config(payload.get("config"))
    return engine.TradingSession(bars, cfg, strategy=strategy,
                                 trade_prefix="PAPER", curve_limit=1000)


def step(session: engine.TradingSession) -> engine.TradingSession:
    return session.advance()


def cancel(session: engine.TradingSession) -> engine.TradingSession:
    """用户取消：按最后处理 bar 的收盘价模拟平仓，避免持仓静默消失。"""
    if session.status not in ("running", "paused"):
        return session
    index = max(0, min(len(session.bars) - 1, session.cursor - 1))
    session.force_close(index, "用户取消，按当前 bar 收盘模拟退出", session.bars[index]["close"])
    session.status = "stopped"
    session.last_message = "策略交易已取消。"
    return session


def pause(session: engine.TradingSession) -> engine.TradingSession:
    if session.status == "running":
        session.status = "paused"
        session.last_message = "模拟运行已暂停。"
    return session


def resume(session: engine.TradingSession) -> engine.TradingSession:
    if session.status == "paused":
        session.status = "running"
        session.last_message = "模拟运行已继续。"
    return session


def _progress(state: dict, total_bars: int) -> float:
    return round(state.get("cursor", 0) / total_bars * 100, 2) if total_bars else 0.0


def snapshot(state: dict, total_bars: int | None = None) -> dict:
    """单条运行的完整视图（交易页、仪表盘共用）。"""
    total = total_bars if total_bars is not None else len(state.get("bars") or [])
    cfg = state["config"]
    return {
        "active": True,
        "runId": state["runId"],
        "status": state["status"],
        "statusLabel": STATUS_LABELS.get(state["status"], state["status"]),
        "strategyId": state.get("strategyId"),
        "strategyName": state.get("strategyName"),
        "symbol": state.get("symbol"),
        "cursor": state["cursor"],
        "totalBars": total,
        "progressPct": _progress(state, total),
        "currentTime": state.get("currentTime"),
        "initialCapital": cfg["capital"],
        "equity": round(state.get("equity", cfg["capital"]), 2),
        "realizedPnl": round(state.get("realizedPnl", 0.0), 2),
        "unrealizedPnl": round(state.get("unrealizedPnl", 0.0), 2),
        "maxDrawdownPct": round(state.get("maxDrawdownPct", 0.0), 4),
        "position": state.get("position"),
        "trades": state.get("trades", []),
        "signals": state.get("signals", [])[-100:],
        "equityCurve": state.get("equityCurve", [])[-1000:],
        "lastMessage": state.get("lastMessage"),
        "contract": {"multiplier": cfg["multiplier"], "qty": cfg["qty"],
                     "feePerSide": cfg["feePerSide"], "slippage": cfg["slippage"],
                     "tickSize": cfg["tickSize"], "slippageTicks": cfg["slippageTicks"]},
    }


def run_summary(state: dict, total_bars: int | None = None) -> dict:
    """运行列表用的轻量视图（不含成交明细与权益曲线）。"""
    total = total_bars if total_bars is not None else len(state.get("bars") or [])
    cfg = state["config"]
    return {
        "runId": state["runId"],
        "createdAt": state.get("createdAt"),
        "status": state["status"],
        "statusLabel": STATUS_LABELS.get(state["status"], state["status"]),
        "strategyId": state.get("strategyId"),
        "strategyName": state.get("strategyName"),
        "symbol": state.get("symbol"),
        "cursor": state["cursor"],
        "totalBars": total,
        "progressPct": _progress(state, total),
        "currentTime": state.get("currentTime"),
        "initialCapital": cfg["capital"],
        "equity": round(state.get("equity", cfg["capital"]), 2),
        "realizedPnl": round(state.get("realizedPnl", 0.0), 2),
        "unrealizedPnl": round(state.get("unrealizedPnl", 0.0), 2),
        "maxDrawdownPct": round(state.get("maxDrawdownPct", 0.0), 4),
        "position": state.get("position"),
        "lastMessage": state.get("lastMessage"),
        "tradeCount": len(state.get("trades", [])),
    }


def is_active(state: dict) -> bool:
    return state.get("status") in ("running", "paused")


def active_count(runs: list[dict]) -> int:
    return sum(1 for run in runs if is_active(run))


def ensure_capacity(runs: list[dict]) -> None:
    if active_count(runs) >= config.MAX_ACTIVE_RUNS:
        raise PermissionError(f"同时运行的策略最多 {config.MAX_ACTIVE_RUNS} 条，请先取消一条。")
