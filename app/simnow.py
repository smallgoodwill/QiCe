"""Optional SimNow/CTP connection. Secrets are not returned by the API or persisted to SQLite."""
from __future__ import annotations

import importlib.util
import os
import threading
from datetime import datetime


class SimNowManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.engine = None
        self.event_engine = None
        self.connected = False
        self.market_connected = False
        self.trade_connected = False
        self.connecting = False
        self.error = ""
        self.settings = None
        self.subscriptions = set()
        self.last_tick = None
        self.last_log = "尚未连接。"
        self._gateway_name = "CTP"

    @staticmethod
    def available():
        return importlib.util.find_spec("vnpy") is not None and importlib.util.find_spec("vnpy_ctp") is not None

    def status(self):
        with self.lock:
            return {"available": self.available(), "connected": self.connected,
                    "marketConnected": self.market_connected, "tradeConnected": self.trade_connected,
                    "connecting": self.connecting, "error": self.error,
                    "lastLog": self.last_log, "subscriptions": sorted(self.subscriptions),
                    "lastTick": self.last_tick,
                    "credentialsConfigured": all(os.environ.get(key, "").strip() for key in
                        ("SIMNOW_USERNAME", "SIMNOW_PASSWORD", "SIMNOW_BROKER_ID", "SIMNOW_MD_ADDRESS", "SIMNOW_TD_ADDRESS"))}

    def connect(self, payload):
        required = ("username", "password", "brokerId", "mdAddress", "tdAddress")
        env_names = {"username": "SIMNOW_USERNAME", "password": "SIMNOW_PASSWORD",
                     "brokerId": "SIMNOW_BROKER_ID", "mdAddress": "SIMNOW_MD_ADDRESS",
                     "tdAddress": "SIMNOW_TD_ADDRESS"}
        values = {key: str(payload.get(key, "")).strip() or os.environ.get(env_names[key], "").strip()
                  for key in required}
        if any(not values[key] for key in required):
            raise ValueError("请填写账号、密码、经纪商代码、行情前置和交易前置。")
        env_optional = {"appId": "SIMNOW_APP_ID", "authCode": "SIMNOW_AUTH_CODE", "productInfo": "SIMNOW_PRODUCT_INFO"}
        optional = {key: str(payload.get(key, "")).strip() or os.environ.get(env_optional[key], "").strip()
                    for key in env_optional}
        if not self.available():
            raise ValueError("本机尚未安装 vn.py CTP 网关。请按 README 安装后重启服务。")
        with self.lock:
            if self.connected or self.connecting:
                raise ValueError("已有 SimNow 连接或正在连接，请先断开。")
            self.connecting, self.error = True, ""
            self.market_connected = self.trade_connected = self.connected = False
            # Deliberately omit the password from settings/status and clear it after connect call.
            self.settings = {"用户名": values["username"], "密码": values["password"],
                "经纪商代码": values["brokerId"], "行情服务器": values["mdAddress"],
                "交易服务器": values["tdAddress"], "产品名称": optional["appId"],
                # SimNow's normal simulated front uses CTP's production protocol mode.
                # This is not a live account selector; the account/fronts remain SimNow.
                "产品信息": optional["productInfo"], "授权编码": optional["authCode"], "柜台环境": "实盘"}
        threading.Thread(target=self._connect_worker, daemon=True).start()
        return {"ok": True, "connecting": True}

    def _connect_worker(self):
        try:
            from vnpy.event import Event, EventEngine
            from vnpy.trader.engine import MainEngine
            from vnpy.trader.event import EVENT_LOG, EVENT_TICK
            from vnpy_ctp import CtpGateway
            event_engine = EventEngine()
            main_engine = MainEngine(event_engine)
            main_engine.add_gateway(CtpGateway)
            event_engine.register(EVENT_LOG, self._on_log)
            event_engine.register(EVENT_TICK, self._on_tick)
            with self.lock:
                settings = dict(self.settings or {})
            main_engine.connect(settings, self._gateway_name)
            with self.lock:
                self.event_engine, self.engine = event_engine, main_engine
                self.settings = {k: v for k, v in settings.items() if k != "密码"}
                self.connecting = False
                self.last_log = "已提交 CTP 连接；等待柜台和行情前置回报。"
        except Exception as exc:
            with self.lock:
                self.error = f"CTP 初始化失败：{type(exc).__name__}: {exc}"
                self.connecting, self.connected = False, False
                self.settings = None

    def _on_log(self, event):
        msg = getattr(event, "data", None)
        content = str(getattr(msg, "msg", msg or ""))[:500]
        with self.lock:
            self.last_log = content
            lowered = content.lower()
            is_market = "行情服务器" in content or "行情接口" in content or "market" in lowered
            is_trade = "交易服务器" in content or "trade" in lowered
            if any(marker in content for marker in ("接口报错", "登录失败", "授权验证失败", "客户端认证失败")) or "login failed" in lowered:
                self.error = content
            if "登录成功" in content or "login success" in lowered:
                if is_market: self.market_connected = True
                if is_trade: self.trade_connected = True
                self.connected = self.market_connected and self.trade_connected
                self.connecting = not self.connected
            if "连接断开" in content or "disconnected" in lowered:
                if is_market: self.market_connected = False
                if is_trade: self.trade_connected = False
                self.connected = False
                self.connecting = False
            if "登录失败" in content or "login failed" in lowered:
                self.error, self.connected, self.connecting = content, False, False
                if is_market: self.market_connected = False
                if is_trade: self.trade_connected = False

    def _on_tick(self, event):
        tick = getattr(event, "data", None)
        if not tick:
            return
        with self.lock:
            self.last_tick = {"symbol": getattr(tick, "vt_symbol", ""),
                "lastPrice": getattr(tick, "last_price", None), "volume": getattr(tick, "volume", None),
                "datetime": getattr(tick, "datetime", None).isoformat(sep=" ") if getattr(tick, "datetime", None) else "",
                "bidPrice": getattr(tick, "bid_price_1", None), "askPrice": getattr(tick, "ask_price_1", None)}

    def subscribe(self, symbol, exchange):
        symbol, exchange = str(symbol).strip(), str(exchange).strip()
        if not symbol or not exchange:
            raise ValueError("请输入合约代码和交易所代码。")
        with self.lock:
            engine = self.engine
        if not engine:
            raise ValueError("请先连接 SimNow。")
        from vnpy.trader.object import SubscribeRequest
        from vnpy.trader.constant import Exchange
        try:
            venue = Exchange(exchange)
        except ValueError:
            raise ValueError("交易所代码无效，请使用 SHFE、DCE、CZCE、CFFEX、INE 或 GFEX。")
        engine.subscribe(SubscribeRequest(symbol=symbol, exchange=venue), self._gateway_name)
        with self.lock:
            self.subscriptions.add(f"{symbol}.{exchange}")
        return self.status()

    def disconnect(self):
        with self.lock:
            engine, event_engine = self.engine, self.event_engine
            self.engine = self.event_engine = None
            self.connected = self.connecting = self.market_connected = self.trade_connected = False
            self.settings = None
            self.subscriptions.clear()
            self.last_tick = None
            self.last_log = "已请求断开。"
        if engine:
            try:
                engine.close()
            except Exception as exc:
                with self.lock:
                    self.error = f"断开时出现错误：{exc}"
        return self.status()


simnow = SimNowManager()
