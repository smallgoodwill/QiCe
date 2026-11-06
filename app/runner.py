"""策略级常驻纸上交易（paper trading）。

和「区间模拟」的区别只有一个：区间模拟是给定一段行情、一次跑完就结束；
常驻交易是策略挂在那里，每来一根新 bar 判一次，持续产出成交。

两者共用 `engine.TradingSession`，撮合口径完全一致，差别只有两点：

1. `continuous=True` —— 不在最后一根 bar 强制平仓（最新一根是"当下"，不是"结束"）；
2. 行情靠 `feed()` 增量追加，而不是构造时一次给全。

**边界**：全程本地记账，不连任何柜台、不发任何委托。
所谓"启动策略"只是让这个进程开始按规则记账，不会动真实账户一分钱。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from . import db, engine, instruments, klines, simulate

logger = logging.getLogger("qice.runner")

# 每次 tick 的间隔。分钟线最细也是 1 分钟一根，15 秒足够跟上，
# 又不会把上游接口打得太频繁（klines 自身还有 12 秒缓存）。
TICK_SECONDS = 15.0
# 每次拉多少根。要够慢线预热，也要够长到能看出均线关系；
# 320 根 1 分钟线约等于一个半交易小时。
WINDOW = 320
# 落库节流：状态每 tick 都在变，但没必要每 15 秒写一次 SQLite
PERSIST_SECONDS = 30.0
# 连续失败这么多次就自动暂停，免得半夜接口挂了还在空转刷日志
MAX_ERRORS = 5

# 策略库里 timeframe 是给人看的字符串，这里映射到行情周期
PERIOD_OF = {
    "1 分钟": "1m", "5 分钟": "5m", "15 分钟": "15m", "30 分钟": "30m", "60 分钟": "60m",
    # 分钟线接口没有日线。日线策略只能按 60 分钟近似推进，这点必须写在界面上，
    # 否则"日线策略在跑 1 分钟信号"是骗人的。
    "日线": "60m",
}
PERIOD_LABEL = {"1m": "1 分钟", "5m": "5 分钟", "15m": "15 分钟", "30m": "30 分钟", "60m": "60 分钟"}
STATUS_LABEL = {"running": "交易中", "paused": "已暂停", "stopped": "已停止"}

_LOCK = threading.RLock()
_RUNTIMES: dict[str, "Runtime"] = {}
_THREAD: threading.Thread | None = None


@dataclass
class Runtime:
    strategy_id: str
    strategy_name: str
    symbol: str                      # 策略里记的原始写法，如「沪深300股指 · IF2503」
    continuous: str = ""             # 实际跟的主力连续代码，如 RB0
    instrument: str = ""             # 品种代码，如 rb
    period: str = "5m"
    notice: str = ""
    status: str = "running"
    session: engine.TradingSession | None = None
    started_at: str = ""
    updated_at: str = ""
    error: str = ""
    errors: int = 0
    last_persist: float = 0.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# 配置换算（与区间模拟同一条路）
# --------------------------------------------------------------------------
def _cfg_for(strategy: dict, inst: instruments.Instrument | None) -> dict:
    """策略参数 → 引擎配置。

    乘数和最小变动是**合约属性**不是策略属性，以品种表为准；
    策略库里存的那份可能过时（早期默认 300，跑螺纹会放大 60 倍）。
    """
    raw = {**simulate.DEFAULT_CONFIG}
    if inst:
        raw["multiplier"] = inst.multiplier
        raw["tickSize"] = inst.tick
    raw.update({key: value for key, value in strategy.items()
                if key not in ("multiplier", "tickSize")})
    return engine.normalize_config(raw)


def _period_for(strategy: dict, override: str | None) -> tuple[str, str]:
    """定下推进周期，并给出需要提示给用户的话（如果有）。"""
    if override:
        if override not in PERIOD_LABEL:
            raise ValueError(f"不支持的周期「{override}」，可选：{'、'.join(PERIOD_LABEL)}。")
        return override, ""
    label = str(strategy.get("timeframe") or "5 分钟")
    period = PERIOD_OF.get(label, "5m")
    if label == "日线":
        return period, "该策略周期是「日线」，分钟线接口没有日线数据，常驻交易按 60 分钟推进（近似，不等价）。"
    return period, ""


# --------------------------------------------------------------------------
# 启停
# --------------------------------------------------------------------------
def start(strategy_id: str, *, period: str | None = None) -> dict:
    """让一条策略上线开始纸上交易。已在运行则直接返回现状（不重复建）。"""
    with _LOCK:
        existing = _RUNTIMES.get(strategy_id)
        if existing and existing.status in ("running", "paused"):
            return _snapshot(existing)

    strategy = db.get_strategy(strategy_id)
    if not strategy:
        raise ValueError("策略不存在，可能已被删除。")

    raw_symbol = str(strategy.get("symbol") or "").strip()
    if not raw_symbol:
        raise ValueError("这条策略没有绑定品种，无法启动。请先编辑策略并选择品种。")

    period, notice = _period_for(strategy, period)
    try:
        first = klines.fetch(raw_symbol, period=period, limit=WINDOW, force=True)
    except klines.KlineError as exc:
        raise ValueError(f"取不到「{raw_symbol}」的行情：{exc}") from None

    bars = first["bars"]
    inst = instruments.get(first["instrument"])
    cfg = _cfg_for(strategy, inst)
    need = max(60, cfg["slow"] + 5)
    if len(bars) < need:
        raise ValueError(f"可用行情只有 {len(bars)} 根，至少需要 {need} 根"
                         f"（慢线 {cfg['slow']} 根预热 + 5 根成交）。稍后再启动，或改用更短的周期。")
    # 洗一遍：补齐 event_blackout 等可选字段、校验时间严格递增与 OHLC 关系。
    # 分钟线接口给的是裸 OHLCV，不洗的话引擎读可选字段会直接 KeyError。
    bars = engine.validate_bars(bars)

    session = engine.TradingSession(
        bars, cfg,
        strategy={"id": strategy_id, "name": strategy.get("name") or "未命名策略",
                  "symbol": first["continuous"]},
        trade_prefix="STRAT", curve_limit=2000, continuous=True)
    session.meta = {"source": first.get("source"), "notice": first.get("notice")}
    # 初始把窗口内的历史一次回放完：启动瞬间就能看到指标状态和最近一笔，
    # 而不是从空图开始等十几分钟。
    while session.cursor < len(session.bars):
        session.advance()
    session.mark_latest()

    runtime = Runtime(
        strategy_id=strategy_id,
        strategy_name=strategy.get("name") or "未命名策略",
        symbol=raw_symbol, continuous=first["continuous"], instrument=first["instrument"],
        period=period, notice=notice, status="running", session=session,
        started_at=_now(), updated_at=_now())
    with _LOCK:
        _RUNTIMES[strategy_id] = runtime
    _persist(runtime, force=True)
    _ensure_thread()
    logger.info("策略「%s」已上线：%s · %s", runtime.strategy_name, first["continuous"], period)
    return _snapshot(runtime)


def pause(strategy_id: str) -> dict:
    """暂停交易：停止判定信号与开平仓，持仓保留、浮盈继续跟最新价。"""
    runtime = _require(strategy_id)
    with _LOCK:
        if runtime.status == "running":
            runtime.status = "paused"
            runtime.updated_at = _now()
            if runtime.session:
                runtime.session.last_message = "交易已暂停：不再开平仓，持仓保留。"
    _persist(runtime, force=True)
    return _snapshot(runtime)


def resume(strategy_id: str) -> dict:
    """恢复交易：从现在这一根 bar 起重新判定，暂停期间错过的信号不补做。"""
    runtime = _require(strategy_id)
    with _LOCK:
        if runtime.status != "paused":
            raise ValueError("只有已暂停的策略可以恢复交易。")
        runtime.status = "running"
        runtime.updated_at = _now()
        if runtime.session:
            # 暂停期间行情照收（feed），所以这里只是把 cursor 推到最新一根：
            # 中间那些 bar 不产生成交，否则"暂停"就等于没停过。
            skipped = runtime.session.skip_to(len(runtime.session.bars) - 1)
            runtime.session.last_message = (
                f"交易已恢复，跳过暂停期间的 {skipped} 根 bar。" if skipped else "交易已恢复。")
    _persist(runtime, force=True)
    return _snapshot(runtime)


def stop(strategy_id: str) -> dict:
    """下线：清掉运行态。已产生的成交归档进 trading_runs，事后仍可逐笔查。

    运行态（strategy_runtime）随 stop 删除，成交原本就存在运行态的 state_json 里，
    不先归档就等于把「历史成交仍保留在服务端」变成空话——所以这里先把会话
    整份存成一条已完成的 trading_run（tradePrefix 仍是 STRAT，查询时能认出来源），
    再删运行态。
    """
    runtime = _require(strategy_id)
    with _LOCK:
        runtime.status = "stopped"
        runtime.updated_at = _now()
        if runtime.session:
            runtime.session.last_message = "策略已停止。"
            if runtime.session.trades:
                # 归档记录标成已完成：不能再被 step/恢复，也不会被当成活动运行
                runtime.session.status = "completed"
                try:
                    db.save_trading_run(runtime.session)
                except Exception:              # noqa: BLE001 - 归档失败不阻塞下线
                    logger.exception("归档常驻成交失败：%s", strategy_id)
        _RUNTIMES.pop(strategy_id, None)
    db.delete_strategy_runtime(strategy_id)       # 只删运行态；成交已在上面归档
    return {"strategyId": strategy_id, "status": "stopped", "statusLabel": STATUS_LABEL["stopped"]}


def forget(strategy_id: str) -> None:
    """策略被删除时顺手清掉运行态（不报错）。"""
    with _LOCK:
        runtime = _RUNTIMES.pop(strategy_id, None)
        if runtime:
            runtime.status = "stopped"
    db.delete_strategy_runtime(strategy_id)


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------
def _require(strategy_id: str) -> Runtime:
    with _LOCK:
        runtime = _RUNTIMES.get(strategy_id)
    if not runtime:
        raise ValueError("这条策略当前没有在运行，请先启动策略。")
    return runtime


def _position(session: engine.TradingSession) -> dict | None:
    pos = session.position
    if not pos:
        return None
    index = max(0, min(session.cursor - 1, len(session.bars) - 1))
    bar = session.bars[index] if session.bars else None
    return {
        "side": "做多", "qty": pos["qty"],
        "entryTime": pos["entryTime"], "entryPrice": round(pos["entryPrice"], 2),
        "lastPrice": round(bar["close"], 2) if bar else None,
        "unrealizedPnl": session.unrealized_pnl,
        "holdBars": max(0, index - pos["entryIdx"]),
    }


def _snapshot(runtime: Runtime) -> dict:
    session = runtime.session
    trades = session.trades if session else []
    return {
        "strategyId": runtime.strategy_id,
        "strategyName": runtime.strategy_name,
        "status": runtime.status,
        "statusLabel": STATUS_LABEL.get(runtime.status, runtime.status),
        "symbol": runtime.symbol,
        "continuous": runtime.continuous,
        "instrument": runtime.instrument,
        "period": runtime.period,
        "periodLabel": PERIOD_LABEL.get(runtime.period, runtime.period),
        "notice": runtime.notice,
        "startedAt": runtime.started_at,
        "updatedAt": runtime.updated_at,
        "error": runtime.error,
        "tradeCount": len(trades),
        "netPnl": round(sum(t["netPnl"] for t in trades), 2),
        "equity": round(session.equity, 2) if session else None,
        "realizedPnl": round(session.realized_pnl, 2) if session else 0.0,
        "unrealizedPnl": round(session.unrealized_pnl, 2) if session else 0.0,
        "maxDrawdownPct": round(session.max_drawdown_pct, 2) if session else 0.0,
        "currentTime": session.current_time if session else None,
        "bars": len(session.bars) if session else 0,
        "cursor": session.cursor if session else 0,
        "position": _position(session) if session else None,
        "trades": trades[-8:][::-1] if trades else [],
        "signals": (session.signals[-6:][::-1] if session else []),
        "lastMessage": session.last_message if session else "",
        "tickSeconds": TICK_SECONDS,
    }


def list_all() -> dict:
    with _LOCK:
        items = [_snapshot(runtime) for runtime in _RUNTIMES.values()]
    order = {"running": 0, "paused": 1}
    items.sort(key=lambda item: (order.get(item["status"], 2), item["strategyName"]))
    return {"items": items, "count": len(items),
            "running": sum(1 for item in items if item["status"] == "running"),
            "paused": sum(1 for item in items if item["status"] == "paused")}


def detail(strategy_id: str) -> dict:
    return _snapshot(_require(strategy_id))


def trade_history(strategy_id: str) -> list[dict] | None:
    """在跑会话的全量成交（内存里最新一份）。这条策略不在跑时返回 None。

    和 `_snapshot` 的区别：卡片轮询只要最近 8 笔，交易记录页要全部；
    落库节流是 30 秒一次，点开详情时优先读内存，免得刚平的仓查不到。
    """
    with _LOCK:
        runtime = _RUNTIMES.get(strategy_id)
    return list(runtime.session.trades) if runtime and runtime.session else None


# --------------------------------------------------------------------------
# 推进
# --------------------------------------------------------------------------
def tick_once() -> None:
    """推进一轮。对外暴露成同步接口，自检里不用等后台线程。"""
    with _LOCK:
        runtimes = list(_RUNTIMES.values())
    for runtime in runtimes:
        _tick(runtime)


def _tick(runtime: Runtime) -> None:
    """拉最新行情 → 追加 → 推进。

    网络请求放在锁外（可能要几秒），只把毫秒级的撮合放进锁里，
    否则一次慢请求会把查状态的接口一起堵住。
    """
    try:
        data = klines.fetch(runtime.continuous or runtime.symbol,
                            period=runtime.period, limit=WINDOW)
    except Exception as exc:                      # noqa: BLE001 - 上游任何异常都不该拖垮调度
        with _LOCK:
            runtime.errors += 1
            runtime.error = f"{type(exc).__name__}: {exc}"
            if runtime.errors >= MAX_ERRORS and runtime.status == "running":
                runtime.status = "paused"
                if runtime.session:
                    runtime.session.last_message = f"连续 {MAX_ERRORS} 次取不到行情，已自动暂停：{exc}"
                logger.warning("策略「%s」连续取数失败，已自动暂停", runtime.strategy_name)
            runtime.updated_at = _now()
        _persist(runtime)
        return

    with _LOCK:
        if runtime.status == "stopped" or not runtime.session:
            return
        session = runtime.session
        session.feed(data.get("bars") or [])
        # 常驻交易只增不减，跑久了 bars 会堆到上万根；先裁掉最老的一批再推进
        session.trim()
        if runtime.status == "running":
            while session.cursor < len(session.bars):
                session.advance()
        # 没有新 bar 也要重估浮盈，否则浮盈会冻在上一个价位
        session.mark_latest()
        runtime.error = ""
        runtime.errors = 0
        runtime.updated_at = _now()
    _persist(runtime)


def _persist(runtime: Runtime, *, force: bool = False) -> None:
    # 已经停止的策略不要再写回运行态：否则它刚被 delete 掉，
    # 一个正在飞的 tick 又会把记录写回来，重启后就多出一条幽灵运行态。
    if runtime.status == "stopped":
        return
    now = time.monotonic()
    if not force and now - runtime.last_persist < PERSIST_SECONDS:
        return
    runtime.last_persist = now
    try:
        with _LOCK:
            # state() 不含行情，market_data() 才是 bars + 指标序列，
            # 合并成一份才能被 TradingSession.restore() 直接吃回去
            full = {}
            if runtime.session:
                full = {**runtime.session.state(), **runtime.session.market_data()}
            record = {
                "strategyId": runtime.strategy_id, "status": runtime.status,
                "symbol": runtime.continuous or runtime.symbol, "period": runtime.period,
                "startedAt": runtime.started_at, "updatedAt": runtime.updated_at,
                "strategyName": runtime.strategy_name, "state": full,
            }
        db.save_strategy_runtime(record)
    except Exception:                              # noqa: BLE001 - 落库失败不该影响交易循环
        logger.exception("保存策略运行态失败：%s", runtime.strategy_id)


def _ensure_thread() -> None:
    global _THREAD
    with _LOCK:
        if _THREAD and _THREAD.is_alive():
            return
        _THREAD = threading.Thread(target=_loop, name="qice-strategy-runner", daemon=True)
        _THREAD.start()


def _loop() -> None:
    # 启动时先等一轮，避开服务刚起来时的初始化抖动
    time.sleep(2.0)
    while True:
        try:
            tick_once()
        except Exception:                          # noqa: BLE001 - 调度线程不能死
            logger.exception("策略调度轮次异常")
        time.sleep(TICK_SECONDS)


# --------------------------------------------------------------------------
# 服务重启后的恢复
# --------------------------------------------------------------------------
def restore() -> int:
    """把上次留在库里的运行态读回来。

    刻意**不自动恢复成运行中**：服务重启意味着中间断了一段行情，此时继续跑
    会把断档前后的价格硬接在一起，成交价根本没法解释。统一恢复成"已暂停"，
    等用户确认后手动恢复——那一跳会被 skip_to 显式跳过并记一条信号。
    """
    count = 0
    for record in db.list_strategy_runtimes():
        state = record.get("state") or {}
        if not state.get("bars"):
            continue
        try:
            strategy = db.get_strategy(record["strategyId"]) or {}
            session = engine.TradingSession.restore(state)
            session.continuous = True
            inst = instruments.get(record.get("symbol") or "")
            runtime = Runtime(
                strategy_id=record["strategyId"],
                strategy_name=record.get("strategyName") or strategy.get("name") or "未命名策略",
                symbol=str(strategy.get("symbol") or record.get("symbol") or ""),
                continuous=record.get("symbol") or "",
                instrument=inst.code if inst else "",
                period=record.get("period") or "5m",
                status="paused",
                session=session,
                started_at=record.get("startedAt") or _now(),
                updated_at=_now())
            session.last_message = "服务重启过，交易已自动暂停；确认后点「恢复交易」继续。"
            with _LOCK:
                _RUNTIMES[record["strategyId"]] = runtime
            count += 1
        except Exception:                          # noqa: BLE001 - 单条坏数据不该拖垮启动
            logger.exception("恢复策略运行态失败：%s", record.get("strategyId"))
    if count:
        logger.info("已恢复 %s 条策略的运行态（均为暂停状态）", count)
    return count
