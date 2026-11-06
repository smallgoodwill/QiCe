"""HTTP 层：API 路由 + 静态资源白名单。

这次重构修复的两个高危问题
----------------------------
1. **密钥泄露**：旧实现直接把项目目录当静态根，`GET /.env` 能下载到 `.env`
   （内含 DeepSeek Key 与 SimNow 账号密码），`/server.py`、`/.venv/` 也全可读。
   现在静态根固定为 `web/`，只放行白名单后缀，任何以 `.` 开头的路径一律 404。
2. **CSRF / DNS rebinding**：本机服务默认没有来源校验，任意网页都能 fetch 到
   `127.0.0.1:8765` 改配置、跑回测。现在校验 Host 与 Origin，
   写操作强制 `Content-Type: application/json`（跨站简单请求无法伪造该头）。
"""
from __future__ import annotations

import errno
import json
import logging
import mimetypes
import queue as queue_module
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import (__version__, ai, aistrategy, backtest, config, context, db, diagnose, engine,
               feed, history, instruments, klines, macro, marketdata, report, runner, simnow,
               simulate, ticks, trading, views)

logger = logging.getLogger(__name__)

STORE_LOCK = threading.Lock()

# 只放行前端真正会用到的后缀；源码、配置、数据文件一律不在 web/ 下
ALLOWED_STATIC_SUFFIXES = {".html", ".htm", ".js", ".mjs", ".css", ".svg",
                           ".png", ".jpg", ".jpeg", ".webp", ".ico", ".woff2", ".map"}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data:; "
                                "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"),
}


class ApiError(Exception):
    """带 HTTP 状态码的业务异常。"""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


class Handler(BaseHTTPRequestHandler):
    server_version = f"qice/{__version__}"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    allowed_hosts: set[str] = set()
    allowed_origins: set[str] = set()

    # ---- 基础设施 --------------------------------------------------------
    def log_message(self, fmt, *args):  # 关掉默认的 stderr 逐请求刷屏
        logger.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, body: bytes, status: int, content_type: str, cache: str = "no-store",
              extra_headers: dict | None = None) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            for key, value in SECURITY_HEADERS.items():
                self.send_header(key, value)
            # 报告导出的 Content-Disposition 走这里；文件名是中文，必须按 RFC 5987 编码
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if not getattr(self, "_head_only", False):
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass   # 浏览器取消请求属于正常情况

    def _json(self, data, status: int = 200) -> None:
        self._send(json.dumps(data, ensure_ascii=False).encode("utf-8"), status,
                   "application/json; charset=utf-8")

    def _guard(self) -> None:
        """Host 防 DNS rebinding，Origin 防跨站请求。"""
        host = (self.headers.get("Host") or "").strip().lower()
        if host and host not in self.allowed_hosts:
            raise ApiError(403, "仅允许通过 127.0.0.1 或 localhost 访问本服务。")
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin and origin != "null" and origin not in self.allowed_origins:
            raise ApiError(403, "拒绝来自其他站点的跨域请求。")

    def _read_json(self) -> dict:
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise ApiError(415, "请求必须使用 application/json。")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Content-Length 无效。") from None
        if length <= 0:
            raise ApiError(400, "请求内容为空。")
        if length > config.MAX_BODY_BYTES:
            raise ApiError(413, f"请求内容超过 {config.MAX_BODY_BYTES // 1024 // 1024} MB 限制。")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "请求必须是有效 JSON。") from None
        if not isinstance(payload, dict):
            raise ApiError(400, "请求体必须是 JSON 对象。")
        return payload

    # ---- 方法分发 --------------------------------------------------------
    def do_HEAD(self):
        self._head_only = True
        self.do_GET()

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            self._guard()
            if path == "/api/live/stream":
                self._sse()                    # 长连接，不走 _json 一次性响应
            elif path == "/api/kline/stream":
                self._sse_kline()              # 秒级 K 线流：hello 全量 + 每秒形成中 bar
            elif path.startswith("/api/reports/") and path.endswith("/export"):
                self._export_report(path)      # 需要 Content-Disposition，单独处理
            elif path.startswith("/api/"):
                status, payload = self._route_get(path)
                self._json(payload, status)
            else:
                self._static(path)
        except ApiError as exc:
            self._json({"error": exc.message}, exc.status)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass                               # 浏览器关掉 SSE / 取消请求属于正常情况
        except Exception:                                  # noqa: BLE001 - 兜底不泄露栈
            logger.exception("处理 GET %s 失败", path)
            self._json({"error": "服务内部错误，请查看 data/runtime/logs/ 下的日志。"}, 500)

    # ---- 实时推送（SSE） --------------------------------------------------
    def _query(self) -> dict:
        raw = parse_qs(urlparse(self.path).query)
        return {key: values[0] for key, values in raw.items() if values}

    def _sse(self) -> None:
        """Server-Sent Events 长连接。

        选 SSE 而不是 WebSocket：单向推送、浏览器自带断线重连、标准库就能实现，
        本地单用户场景没有必要为双向通信引入额外依赖。
        """
        if getattr(self, "_head_only", False):
            raise ApiError(405, "HEAD 不支持实时推送。")
        query = self._query()
        symbol = (query.get("symbol") or "").strip()
        queue = feed.hub.open_stream()
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            for key, value in SECURITY_HEADERS.items():
                self.send_header(key, value)
            self.end_headers()
            self._sse_event("hello", {"status": feed.hub.status(),
                                      "bars": feed.hub.bars(symbol, "1m", 500)})
            while True:
                try:
                    event = queue.get(timeout=15)
                except queue_module.Empty:
                    self.wfile.write(b": ping\n\n")   # 心跳，防代理超时断流
                    self.wfile.flush()
                    continue
                self._sse_event(event.get("type", "message"), event)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            feed.hub.close_stream(queue)
            logger.debug("SSE 连接结束：%s", symbol or "-")

    def _sse_event(self, name: str, payload) -> None:
        body = f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        self.wfile.write(body.encode("utf-8"))
        self.wfile.flush()

    def _sse_kline(self) -> None:
        """秒级 K 线流：`hello` 推全量窗口，之后每秒推「正在形成的那根 bar」。

        与 `_sse`（采集器 tick 聚合）互补：这条不依赖采集器，任意品种随连随用，
        主页「实时 K 线」靠它按秒跳动。客户端断开由写失败感知。
        """
        if getattr(self, "_head_only", False):
            raise ApiError(405, "HEAD 不支持实时推送。")
        query = self._query()
        symbol = (query.get("symbol") or "").strip()
        period = (query.get("period") or "1m").lower()
        if not symbol:
            raise ApiError(400, "缺少 symbol 参数。")
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            for key, value in SECURITY_HEADERS.items():
                self.send_header(key, value)
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

        try:
            try:
                data = klines.fetch(symbol, period=period, limit=320)
                tick = ticks.snapshot(symbol)
            except (klines.KlineError, ticks.TickError) as exc:
                self._sse_event("error", {"error": str(exc)})
                return
            base = data["bars"][-1] if data["bars"] else None
            forming = ticks.FormingBar(symbol, period, base)
            forming._prev_day_vol = tick.get("dayVolume")   # noqa: SLF001 - 首 tick 不计增量
            self._sse_event("hello", {
                "symbol": symbol, "continuous": data["continuous"],
                "period": period, "periodLabel": data["periodLabel"],
                "bars": data["bars"], "tick": tick,
                "source": data["source"], "notice": ticks.NOTICE,
            })
            errors = 0
            while True:
                try:
                    tick = ticks.snapshot(symbol)
                    errors = 0
                except ticks.TickError as exc:
                    errors += 1
                    if errors >= 5:            # 连续取不到就明确报错断流，让前端退回轮询
                        self._sse_event("error", {"error": str(exc)})
                        return
                    time.sleep(2)
                    continue
                self._sse_event("bar", {"bar": forming.feed(tick), "tick": tick})
                time.sleep(1)                  # 按秒推送；上游 0.8 秒缓存兜住并发客户端
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            logger.debug("K 线 SSE 结束：%s %s", symbol, period)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            self._guard()
            status, payload = self._route_post(path)
            self._json(payload, status)
        except ApiError as exc:
            self._json({"error": exc.message}, exc.status)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
        except PermissionError as exc:
            self._json({"error": str(exc)}, 409)
        except Exception:                                  # noqa: BLE001
            logger.exception("处理 POST %s 失败", path)
            self._json({"error": "服务内部错误，请查看 data/runtime/logs/ 下的日志。"}, 500)

    def do_DELETE(self):
        path = urlparse(self.path).path
        try:
            self._guard()
            status, payload = self._route_delete(path)
            self._json(payload, status)
        except ApiError as exc:
            self._json({"error": exc.message}, exc.status)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
        except Exception:                                  # noqa: BLE001
            logger.exception("处理 DELETE %s 失败", path)
            self._json({"error": "服务内部错误，请查看 data/runtime/logs/ 下的日志。"}, 500)

    # ---- 读接口 ----------------------------------------------------------
    def _export_report(self, path: str) -> None:
        """报告导出：/api/reports/<id>/export?format=md|html

        中文文件名不能用裸的 filename=，浏览器会乱码或截断，
        必须同时给 filename* 的 RFC 5987 编码版本。
        """
        parts = [segment for segment in path.strip("/").split("/") if segment]
        if len(parts) != 4:
            raise ApiError(404, "导出路径不正确。")
        report_id = unquote(parts[2])
        fmt = (self._query().get("format") or "md").lower()
        if fmt not in ("md", "html"):
            raise ApiError(400, "只支持 md 或 html 格式。")
        try:
            filename, body, content_type = (report.export_html(report_id) if fmt == "html"
                                            else report.export_markdown(report_id))
        except ValueError as exc:
            raise ApiError(404, str(exc)) from None
        quoted = quote(filename)
        self._send(body.encode("utf-8"), 200, content_type, cache="no-store",
                   extra_headers={"Content-Disposition":
                                  f"attachment; filename=\"{quoted}\"; filename*=UTF-8''{quoted}"})

    def _route_get(self, path: str) -> tuple[int, object]:
        if path == "/api/health":
            stats = db.stats()
            return 200, {"ok": True, "service": "qice-local-paper-lab", "version": __version__,
                         "mode": "paper-only", "storage": "sqlite", "counts": {
                             "strategies": stats["strategies"], "backtests": stats["backtests"],
                             "tradingRuns": stats["tradingRuns"], "activeRuns": stats["activeRuns"]},
                         "runtimes": {"ai": bool(ai.status()["configured"]),
                                      "simnow": simnow.simnow.available()}}
        if path == "/api/simnow/status":
            return 200, simnow.simnow.status()
        if path == "/api/instruments":
            return 200, {"groups": instruments.all_instruments(),
                         "exchanges": instruments.EXCHANGES,
                         "sectors": list(instruments.SECTORS)}
        if path.startswith("/api/instruments/"):
            # /api/instruments/rb/contracts —— 选完品种再按当前月份生成可订阅合约
            parts = [seg for seg in path.split("/") if seg]
            if len(parts) == 4 and parts[3] == "contracts":
                inst = instruments.get(parts[2])
                if not inst:
                    raise ApiError(404, "未知品种。")
                return 200, {"instrument": inst.to_dict(),
                             "contracts": instruments.active_contracts(inst)}
            raise ApiError(404, "未知 API 路径。")
        if path == "/api/live/status":
            return 200, feed.hub.status()
        if path == "/api/live/bars":
            query = self._query()
            bars = feed.hub.bars(query.get("symbol", ""), query.get("period", "1m"),
                                 int(query.get("limit", 500)))
            return 200, {"symbol": query.get("symbol") or feed.hub.symbol,
                         "period": query.get("period", "1m"), "bars": bars}
        if path == "/api/dashboard":
            return 200, views.dashboard()
        if path == "/api/diagnostics":
            return 200, views.diagnostics()
        if path == "/api/diagnostics/context":
            # 品种诊断的消息面 / 情绪面板：15 分钟缓存，force=1 强制重抓
            query = self._query()
            symbol = (query.get("symbol") or "").strip()
            if not symbol:
                raise ApiError(400, "请先选择品种。")
            try:
                if query.get("force") in ("1", "true", "yes"):
                    return 200, context.collect(symbol, force=True)
                cached = context.cached(symbol)
                return 200, context.collect(symbol) if not cached.get("available") else cached
            except ValueError as exc:
                raise ApiError(400, str(exc)) from None
        if path == "/api/diagnostics/instrument":
            # 品种诊断：只看所选品种自己的行情，纯本地计算，秒级返回
            query = self._query()
            symbol = (query.get("symbol") or "").strip()
            if not symbol:
                raise ApiError(400, "请先选择品种。")
            try:
                return 200, diagnose.snapshot(symbol,
                                              period=(query.get("period") or "1m").lower(),
                                              limit=int(query.get("limit", diagnose.DEFAULT_BARS)))
            except klines.KlineError as exc:
                raise ApiError(502, str(exc)) from None
            except ValueError as exc:
                raise ApiError(400, str(exc)) from None
        if path == "/api/ai/status":
            return 200, ai.status()
        if path == "/api/reports":
            return 200, report.listing(int(self._query().get("limit", 30)))
        if path.startswith("/api/reports/"):
            try:
                return 200, report.get(unquote(path.rsplit("/", 1)[-1]))
            except ValueError as exc:
                raise ApiError(404, str(exc)) from None
        if path == "/api/kline/tick":
            # 秒级实时快照（一次性）：SSE 不可用时的兜底轮询 / 自检用
            query = self._query()
            symbol = (query.get("symbol") or "").strip()
            if not symbol:
                raise ApiError(400, "缺少 symbol 参数。")
            try:
                data = ticks.snapshot(symbol)
            except ticks.TickError as exc:
                raise ApiError(502, str(exc)) from None
            if query.get("forming") in ("1", "true"):
                try:
                    data["forming"] = ticks.forming(
                        symbol, (query.get("period") or "1m").lower())
                except (ticks.TickError, klines.KlineError):
                    pass                            # 快照已拿到，合成失败不整体报错
            return 200, data
        if path == "/api/kline":
            # 仪表盘自选品种用：按需拉主力连续分钟线，和采集器在不在跑无关
            query = self._query()
            symbol = (query.get("symbol") or "").strip()
            if not symbol:
                raise ApiError(400, "缺少 symbol 参数。")
            try:
                return 200, klines.fetch(symbol,
                                          period=(query.get("period") or "1m").lower(),
                                          limit=int(query.get("limit", 320)),
                                          force=query.get("force") in ("1", "true"))
            except klines.KlineError as exc:
                raise ApiError(502, str(exc)) from None
        if path.startswith("/api/macro/"):
            # 只读缓存，不触发抓取：打开页面不应该等十几秒
            return 200, macro.cached(unquote(path.rsplit("/", 1)[-1]))
        if path == "/api/strategies":
            return 200, db.list_strategies()
        # 放在 /api/strategies 之后：前端每 5 秒轮询一次，只读内存，不碰数据库
        if path == "/api/strategies/trading":
            return 200, runner.list_all()
        if path.startswith("/api/strategies/") and path.endswith("/trading"):
            strategy_id = [s for s in path.strip("/").split("/") if s][2]
            return 200, runner.detail(strategy_id)
        if path.startswith("/api/strategies/") and path.endswith("/trades"):
            # 策略交易记录：常驻 / 模拟 / 回测的成交汇总（策略卡片「详细」入口）
            strategy_id = [s for s in path.strip("/").split("/") if s][2]
            result = views.strategy_trades(strategy_id)
            if not db.get_strategy(strategy_id) and not result["trades"]:
                raise ApiError(404, "策略不存在。")
            return 200, result
        if path == "/api/backtests":
            return 200, db.list_backtests(30)
        if path.startswith("/api/backtests/"):
            result = db.get_backtest(path.rsplit("/", 1)[-1])
            if not result:
                raise ApiError(404, "回测记录不存在。")
            return 200, result
        if path == "/api/trading/runs":
            return 200, [trading.run_summary(state, total) for state, total in db.list_trading_run_states()]
        if path == "/api/trading/trades":
            trades = db.list_trading_run_trades()
            trades.sort(key=lambda trade: trade.get("exitTime", ""), reverse=True)
            return 200, trades
        if path.startswith("/api/trading/runs/") and path.endswith("/bars"):
            # 回放对齐用：把某次运行当时的行情原样取出来，供 K 线页把买卖点画准
            run_id = path[len("/api/trading/runs/"):-len("/bars")].strip("/")
            record = db.get_trading_run(run_id)
            if not record:
                raise ApiError(404, "交易运行记录不存在。")
            bars = record.get("bars") or []
            if not bars:
                raise ApiError(404, "该运行没有保存行情快照。")
            return 200, {"runId": run_id, "symbol": record.get("symbol"),
                         "strategyName": record.get("strategyName"),
                         "bars": bars, "count": len(bars)}
        if path.startswith("/api/trading/runs/"):
            record = db.get_trading_run(path.rsplit("/", 1)[-1])
            if not record:
                raise ApiError(404, "交易运行记录不存在。")
            return 200, trading.snapshot(record, len(record.get("bars") or []))
        if path == "/api/market/history":
            # 回测页改品种/日期/周期时先探测：这段真实行情能不能取、有多少根。
            # 不返回 bars，避免只为显示一行提示就把上千根 K 线传给浏览器。
            query = self._query()
            return 200, history.probe(query.get("symbol"), query.get("start"), query.get("end"),
                                      (query.get("period") or "1d").lower(),
                                      force=query.get("force") in ("1", "true"))
        if path == "/api/market/sample":
            # 仅供本地自检 / 脚本回归使用；页面回测已改为按时间取真实历史行情
            bars = marketdata.sample_bars()
            return 200, {"name": config.SAMPLE_CSV.name, "bars": bars, "count": len(bars),
                         "notice": "内置合成演示行情，仅用于本地自检，不代表任何真实品种走势。"}
        if path == "/api/simulate/options":
            return 200, simulate.options()
        if path == "/api/simulate/history":
            return 200, simulate.history()
        if path.startswith("/api/simulate/"):
            try:
                return 200, simulate.get(unquote(path.rsplit("/", 1)[-1]))
            except ValueError as exc:
                raise ApiError(404, str(exc)) from None
        raise ApiError(404, "未知 API 路径。")

    # ---- 写接口 ----------------------------------------------------------
    def _route_post(self, path: str) -> tuple[int, object]:
        if path == "/api/ai/config":
            return 200, ai.configure(self._read_json())
        if path == "/api/ai/chat":
            body = self._read_json()
            return 200, ai.chat(body.get("messages", []))
        if path == "/api/ai/strategy/draft":
            body = self._read_json()
            strategy = db.get_strategy(str(body.get("strategyId") or "")) if body.get("strategyId") else None
            try:
                return 200, aistrategy.draft(body, strategy)
            except ValueError as exc:
                raise ApiError(400, str(exc)) from None
        if path == "/api/reports":
            try:
                return 201, report.generate(self._read_json())
            except ValueError as exc:
                raise ApiError(400, str(exc)) from None
        if path == "/api/macro/analyze":
            body = self._read_json()
            symbol = str(body.get("symbol") or "").strip()
            if not symbol:
                raise ApiError(400, "请先选择品种。")
            try:
                return 200, macro.analyze(symbol, force=bool(body.get("force")))
            except ValueError as exc:
                raise ApiError(400, str(exc)) from None
        if path == "/api/diagnostics/instrument/analyze":
            body = self._read_json()
            symbol = str(body.get("symbol") or "").strip()
            if not symbol:
                raise ApiError(400, "请先选择品种。")
            try:
                return 200, diagnose.analyze(symbol,
                                             period=str(body.get("period") or "1m").lower(),
                                             include_macro=body.get("includeMacro", True) is not False,
                                             include_tech=body.get("includeTech", True) is not False)
            except klines.KlineError as exc:
                raise ApiError(502, str(exc)) from None
            except ValueError as exc:
                raise ApiError(400, str(exc)) from None
        if path == "/api/live/start":
            body = self._read_json()
            driver = str(body.get("driver") or "sim").lower()
            symbol = str(body.get("symbol") or "").strip()
            if driver == "ctp":
                return 200, feed.hub.start_ctp(symbol, str(body.get("exchange") or "CFFEX"))
            if driver != "sim":
                raise ApiError(400, "未知行情源，仅支持 sim 或 ctp。")
            return 200, feed.hub.start_sim(symbol, body.get("basePrice"), body.get("speedup", 20))
        if path == "/api/live/stop":
            self._read_json()
            return 200, feed.hub.stop()
        if path == "/api/simnow/connect":
            return 202, simnow.simnow.connect(self._read_json())
        if path == "/api/simnow/disconnect":
            self._read_json()
            return 200, simnow.simnow.disconnect()
        if path == "/api/simnow/subscribe":
            body = self._read_json()
            return 200, simnow.simnow.subscribe(body.get("symbol"), body.get("exchange"))
        if path == "/api/strategies":
            return 201, self._save_strategy(self._read_json())
        # 策略级常驻纸上交易：启动 / 暂停 / 恢复 / 停止
        if path.startswith("/api/strategies/") and path.endswith(("/start", "/pause", "/resume", "/stop")):
            parts = [segment for segment in path.strip("/").split("/") if segment]
            if len(parts) != 4:
                raise ApiError(404, "未知策略操作。")
            strategy_id, action = parts[2], parts[3]
            body = self._read_json()          # 空对象也要发：挡住跨站简单请求
            if action == "start":
                return 200, runner.start(strategy_id, period=body.get("period"))
            if action == "pause":
                return 200, runner.pause(strategy_id)
            if action == "resume":
                return 200, runner.resume(strategy_id)
            return 200, runner.stop(strategy_id)
        if path == "/api/backtests":
            try:
                result = backtest.run_backtest(self._read_json())
            except history.HistoryError as exc:
                # 上游挂了 / 区间超出可得窗口：给前端一句能照着改的话，不是 500
                raise ApiError(exc.status, str(exc)) from None
            db.save_backtest(result)
            return 201, result
        if path == "/api/simulate":
            return 201, self._simulate(self._read_json())
        if path == "/api/trading/runs":
            return 201, self._create_run(self._read_json())
        if path.startswith("/api/trading/runs/"):
            # 形如 /api/trading/runs/<runId>/<action>，共 5 段
            parts = [segment for segment in path.strip("/").split("/") if segment]
            if len(parts) != 5 or parts[4] not in ("step", "cancel", "pause", "resume"):
                raise ApiError(404, "未知交易操作。")
            self._read_json()          # 统一要求 JSON 空对象，挡住跨站简单请求
            return 200, self._mutate_run(parts[3], parts[4])
        raise ApiError(404, "未知 API 路径。")

    def _route_delete(self, path: str) -> tuple[int, object]:
        if path.startswith("/api/reports/"):
            report_id = path.rsplit("/", 1)[-1]
            if not db.delete_ai_report(report_id):
                raise ApiError(404, "报告不存在。")
            return 200, {"ok": True, "id": report_id}
        if path.startswith("/api/strategies/"):
            parts = [segment for segment in path.strip("/").split("/") if segment]
            if len(parts) != 3:
                raise ApiError(404, "未知策略路径。")
            strategy_id = parts[2]
            if not db.delete_strategy(strategy_id):
                raise ApiError(404, "策略不存在。")
            # 策略没了，挂在上面的常驻交易也必须下线，否则会继续对着幽灵策略记账
            runner.forget(strategy_id)
            return 200, {"ok": True, "id": strategy_id}
        if path.startswith("/api/trading/runs/"):
            run_id = path.rsplit("/", 1)[-1]
            if not db.delete_trading_run(run_id):
                raise ApiError(404, "运行记录不存在，或仍在运行中（请先取消）。")
            return 200, {"ok": True, "runId": run_id}
        raise ApiError(404, "未知 API 路径。")

    # ---- 业务动作 --------------------------------------------------------
    @staticmethod
    def _save_strategy(body: dict) -> dict:
        scope = body.get("strategy", body)
        name = str(scope.get("name", "")).strip()
        if not name or len(name) > 48:
            raise ApiError(400, "策略名称需为 1–48 个字符。")
        cfg = engine.normalize_config(scope)      # 复用同一套参数校验
        payload = {**scope, "name": name, "fast": cfg["fast"], "slow": cfg["slow"],
                   "maType": cfg["maType"], "atrPeriod": cfg["atrPeriod"],
                   "atrMaxPct": round(cfg["atrMaxPct"] * 100, 4),
                   "rsiPeriod": cfg["rsiPeriod"], "rsiUpper": cfg["rsiUpper"]}
        if scope.get("id"):
            existing = db.get_strategy(str(scope["id"]))
            if existing:
                payload["createdAt"] = existing.get("createdAt")
        return db.save_strategy(payload)

    @staticmethod
    def _simulate(body: dict) -> dict:
        """策略区间模拟：一次跑完并落库，返回买卖点、逐笔盈亏与按日汇总。"""
        strategy_id = str(body.get("strategyId") or "").strip()
        strategy = None
        if strategy_id:
            strategy = db.get_strategy(strategy_id)
            if not strategy:
                raise ApiError(400, "所选策略不存在，请刷新策略库后重试。")
        with STORE_LOCK:
            result = simulate.run(body, strategy)
        logger.info("区间模拟 %s（策略 %s，%d 根 bar，%d 笔成交）",
                    result["runId"], result["strategyName"], result["barCount"],
                    len(result["trades"]))
        return result

    @staticmethod
    def _create_run(body: dict) -> dict:
        strategy_id = str(body.get("strategyId", ""))
        strategy = db.get_strategy(strategy_id)
        if not strategy:
            raise ApiError(400, "请从策略库选择一条已保存的策略。")
        with STORE_LOCK:
            trading.ensure_capacity([state for state, _ in db.list_trading_run_states()])
            session = trading.create_run(body, strategy)
            db.save_trading_run(session)
        logger.info("启动模拟运行 %s（策略 %s，%d 根 bar）",
                    session.run_id, strategy.get("name"), len(session.bars))
        return trading.run_summary(session.state(), len(session.bars))

    @staticmethod
    def _mutate_run(run_id: str, action: str) -> dict:
        with STORE_LOCK:
            record = db.get_trading_run(run_id)
            if not record:
                raise ApiError(404, "交易运行记录不存在。")
            session = engine.TradingSession.restore(record)
            if action == "step":
                trading.step(session)
            elif action == "cancel":
                trading.cancel(session)
            elif action == "pause":
                trading.pause(session)
            else:
                trading.resume(session)
            db.save_trading_run(session)
        return trading.snapshot(session.state(), len(session.bars))

    # ---- 静态资源 --------------------------------------------------------
    def _static(self, path: str) -> None:
        relative = unquote(path or "/")
        if relative in ("", "/"):
            relative = "/index.html"
        if "\\" in relative or "\x00" in relative:
            raise ApiError(404, "未找到资源。")
        segments = [segment for segment in relative.split("/") if segment]
        if any(segment.startswith(".") for segment in segments):   # .env / .venv / .git 一律不可达
            raise ApiError(404, "未找到资源。")
        web_root = config.WEB_DIR.resolve()
        target = (web_root / "/".join(segments)).resolve()
        if target != web_root and web_root not in target.parents:
            raise ApiError(404, "未找到资源。")
        if not target.is_file() or target.suffix.lower() not in ALLOWED_STATIC_SUFFIXES:
            raise ApiError(404, "未找到资源。")
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type.endswith(("javascript", "json")):
            content_type += "; charset=utf-8"
        # 本地服务不做 CDN 缓存：改完前端代码刷新就要生效，
        # 否则浏览器会拿 max-age 里的旧 JS，页面表现和源码对不上（排查成本极高）。
        self._send(target.read_bytes(), 200, content_type, cache="no-cache")


def _port_taken(host: str, port: int) -> bool:
    """探测端口上是否已有进程在监听。

    为什么要单独探一次：Windows 上 `allow_reuse_address=True`（socketserver 默认）
    实际是 SO_REUSEADDR，其语义等价于 SO_REUSEPORT —— **第二个进程能绑定成功**，
    于是两个实例各自持有独立 SQLite 连接，请求随机落到其中一个，表现为
    "刚建的策略时有时无"。这里先主动探测，把这个静默故障变成一条明确报错。
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.3)
    try:
        return probe.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        probe.close()


def make_server(host: str = config.DEFAULT_HOST, port: int = config.DEFAULT_PORT) -> ThreadingHTTPServer:
    db.initialize()
    if _port_taken(host, port):
        raise OSError(errno.EADDRINUSE, f"端口 {host}:{port} 已有进程在监听（是不是已经开着一个实例？）")
    origins = {f"http://{name}:{port}" for name in ("127.0.0.1", "localhost")}
    Handler.allowed_hosts = {f"{name}:{port}" for name in ("127.0.0.1", "localhost")}
    Handler.allowed_origins = origins
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
