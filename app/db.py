"""SQLite 持久化层（单用户本地服务）。

表结构
    strategies     策略库，payload_json 存完整策略对象
    backtests      回测结果，summary_json 供列表页快速读取，result_json 存完整曲线
    trading_runs   模拟运行，state_json 存状态、market_data_json 存行情与指标
    schema_version 版本号，便于后续增量迁移

约定：每次调用独立连接（配合 WAL + busy_timeout），不共享长连接，
避免 HTTP 多线程下跨线程复用 sqlite3.Connection。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from . import config

SCHEMA_VERSION = 4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def initialize() -> None:
    config.ensure_dirs()
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS strategies (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_strategies_updated ON strategies(updated_at DESC);
            CREATE TABLE IF NOT EXISTS backtests (
                id TEXT PRIMARY KEY,
                strategy_id TEXT,
                created_at TEXT NOT NULL,
                source_bars INTEGER NOT NULL,
                summary_json TEXT NOT NULL,
                result_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_backtests_created ON backtests(created_at DESC);
            CREATE TABLE IF NOT EXISTS trading_runs (
                run_id TEXT PRIMARY KEY,
                strategy_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                total_bars INTEGER NOT NULL DEFAULT 0,
                state_json TEXT NOT NULL,
                market_data_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_trading_runs_created ON trading_runs(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_trading_runs_status ON trading_runs(status);
            -- 实时行情落地：只存已收口的 1 分钟 bar，每分钟每合约最多一次写入
            CREATE TABLE IF NOT EXISTS live_bars (
                symbol TEXT NOT NULL,
                ts TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'ctp',
                PRIMARY KEY (symbol, ts)
            );
        """)
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
        elif row["version"] < SCHEMA_VERSION:
            _migrate(conn, row["version"])
            conn.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))


def _migrate(conn: sqlite3.Connection, from_version: int) -> None:
    """增量迁移。

    v2 → v3：移除废弃的单例模拟盘表（已被 trading_runs 取代）。
    v3 → v4：live_bars 增加 source 列。加列前已存在的行无法判断来自真实柜台
             还是内置模拟源（两者混在同一张表里，时间戳会互相穿插），因此统一
             标为 legacy 并排除在查询之外——宁可少画几根，不能把模拟数据当真实行情。
    """
    if from_version < 3:
        conn.execute("DROP TABLE IF EXISTS paper_state")
    if from_version < 4:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(live_bars)")}
        if "source" not in columns:
            conn.execute("ALTER TABLE live_bars ADD COLUMN source TEXT NOT NULL DEFAULT 'legacy'")


# --------------------------------------------------------------------------
# 策略库
# --------------------------------------------------------------------------
def save_strategy(strategy: dict) -> dict:
    now = strategy.get("updatedAt") or _now()
    created = strategy.get("createdAt") or now
    sid = str(strategy.get("id") or uuid.uuid4())
    payload = {**strategy, "id": sid, "createdAt": created, "updatedAt": now}
    with _connect() as conn:
        conn.execute("""INSERT INTO strategies(id,name,created_at,updated_at,payload_json)
            VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,
            updated_at=excluded.updated_at,payload_json=excluded.payload_json""",
            (sid, str(payload.get("name", "")), created, now,
             json.dumps(payload, ensure_ascii=False)))
    return payload


def list_strategies() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT payload_json FROM strategies ORDER BY updated_at DESC").fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


def get_strategy(strategy_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT payload_json FROM strategies WHERE id=?", (strategy_id,)).fetchone()
    return json.loads(row["payload_json"]) if row else None


def delete_strategy(strategy_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM strategies WHERE id=?", (strategy_id,))
        return cur.rowcount > 0


# --------------------------------------------------------------------------
# 回测
# --------------------------------------------------------------------------
def save_backtest(result: dict) -> None:
    with _connect() as conn:
        conn.execute("""INSERT OR REPLACE INTO backtests
            (id,strategy_id,created_at,source_bars,summary_json,result_json) VALUES(?,?,?,?,?,?)""",
            (result["id"], result.get("strategyId"), result.get("createdAt") or _now(),
             int(result.get("sourceBars", 0)),
             json.dumps(result.get("summary", {}), ensure_ascii=False),
             json.dumps(result, ensure_ascii=False)))


def list_backtests(limit: int = 30) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("""SELECT id,strategy_id,created_at,source_bars,summary_json,result_json
            FROM backtests ORDER BY created_at DESC LIMIT ?""", (limit,)).fetchall()
    out = []
    for row in rows:
        result = json.loads(row["result_json"])
        out.append({"id": row["id"], "strategyId": row["strategy_id"],
                    "strategyName": result.get("strategyName"),
                    "createdAt": row["created_at"], "sourceBars": row["source_bars"],
                    "summary": json.loads(row["summary_json"]),
                    "startTime": result.get("startTime"), "endTime": result.get("endTime")})
    return out


def get_backtest(backtest_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT result_json FROM backtests WHERE id=?", (backtest_id,)).fetchone()
    return json.loads(row["result_json"]) if row else None


# --------------------------------------------------------------------------
# 模拟运行
# --------------------------------------------------------------------------
def save_trading_run(session) -> None:
    """接受 engine.TradingSession，行情与状态分列存放。"""
    state = session.state()
    market = session.market_data()
    with _connect() as conn:
        conn.execute("""INSERT INTO trading_runs
            (run_id,strategy_id,status,created_at,updated_at,total_bars,state_json,market_data_json)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET
            status=excluded.status, updated_at=excluded.updated_at,
            total_bars=excluded.total_bars, state_json=excluded.state_json""",
            (session.run_id, session.strategy_id or "", session.status, session.created_at, _now(),
             len(session.bars), json.dumps(state, ensure_ascii=False),
             json.dumps(market, ensure_ascii=False)))


def get_trading_run(run_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT state_json,market_data_json FROM trading_runs WHERE run_id=?",
                           (run_id,)).fetchone()
    if not row:
        return None
    return {**json.loads(row["state_json"]), **json.loads(row["market_data_json"])}


def list_trading_run_states() -> list[tuple[dict, int]]:
    """返回 [(state, total_bars)]，按创建时间倒序。不解析行情，保证列表接口轻量。"""
    with _connect() as conn:
        rows = conn.execute("SELECT state_json,total_bars FROM trading_runs ORDER BY created_at DESC").fetchall()
    return [(json.loads(row["state_json"]), row["total_bars"]) for row in rows]


def list_trading_run_trades() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT state_json FROM trading_runs ORDER BY created_at DESC").fetchall()
    return [trade for row in rows for trade in json.loads(row["state_json"]).get("trades", [])]


def latest_state(*, prefer_active: bool = True) -> tuple[dict, int] | None:
    """取"当前关注"的运行：优先运行中/已暂停，否则取最新一条。"""
    states = list_trading_run_states()
    if not states:
        return None
    if prefer_active:
        for state, total in states:
            if state.get("status") in ("running", "paused"):
                return state, total
    return states[0]


def delete_trading_run(run_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM trading_runs WHERE run_id=? AND status NOT IN ('running','paused')",
                           (run_id,))
        return cur.rowcount > 0


def stats() -> dict[str, Any]:
    with _connect() as conn:
        def scalar(sql: str) -> int:
            row = conn.execute(sql).fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        return {
            "strategies": scalar("SELECT COUNT(*) FROM strategies"),
            "backtests": scalar("SELECT COUNT(*) FROM backtests"),
            "tradingRuns": scalar("SELECT COUNT(*) FROM trading_runs"),
            "activeRuns": scalar("SELECT COUNT(*) FROM trading_runs WHERE status IN ('running','paused')"),
            "liveBars": scalar("SELECT COUNT(*) FROM live_bars"),
            "dbPath": str(config.DB_PATH),
        }


# --------------------------------------------------------------------------
# 实时行情 K 线（只存已收口的 bar，未收口的那根由内存聚合器维护）
# --------------------------------------------------------------------------
def save_live_bar(symbol: str, bar: dict, source: str = "ctp") -> None:
    with _connect() as conn:
        conn.execute("""INSERT INTO live_bars(symbol,ts,open,high,low,close,volume,source)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(symbol,ts) DO UPDATE SET
            open=excluded.open,high=excluded.high,low=excluded.low,
            close=excluded.close,volume=excluded.volume,source=excluded.source""",
            (symbol, bar["timestamp"], float(bar["open"]), float(bar["high"]),
             float(bar["low"]), float(bar["close"]), float(bar.get("volume") or 0), source))


def recent_live_bars(symbol: str, limit: int = 500, source: str | None = "ctp") -> list[dict]:
    """取最近的已收口 bar。

    source 为 None 时不过滤（仅用于排查），默认只取真实柜台行情，
    避免内置模拟源生成的 bar 与真行情混在一张图上。
    """
    sql = "SELECT ts,open,high,low,close,volume FROM live_bars WHERE symbol=?"
    params: list[Any] = [symbol]
    if source:
        sql += " AND source=?"
        params.append(source)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [{"timestamp": row["ts"], "open": row["open"], "high": row["high"],
             "low": row["low"], "close": row["close"], "volume": row["volume"]}
            for row in reversed(rows)]
