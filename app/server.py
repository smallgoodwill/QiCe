"""进程入口：`python -m app.server [--port 8765]`。

只负责三件事：配置日志、绑定端口、优雅退出。业务全部在 app/ 其它模块。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler

from . import __version__, api, config, runner

# 启动后自动开浏览器的开关：设成 1/true/yes 可禁用（CI、无头环境用）
_NO_OPEN_ENV = "QICE_NO_OPEN"

# 端口被占时，占用者不是本服务 → 正常失败并建议换端口
EXIT_PORT_BUSY = 1
# 端口被占，且占用者就是本服务（用户重复点 run.bat）→ 复用，不算失败
EXIT_ALREADY_RUNNING = 3

_SERVICE_NAME = "qice-local-paper-lab"

# 本机地址绝不能走系统代理：Windows 上 HTTP_PROXY/Clash 之类会让 127.0.0.1 的探测
# 白等到超时才返回，甚至返回代理的错误页。这里固定用直连 opener。
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _port_open(host: str, port: int, timeout: float = 0.15) -> bool:
    """朴素 TCP 探活。占用者是谁还不知道，只判断"有没有人在听"。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_running(host: str, port: int) -> dict | None:
    """探测该端口上是否已有**本服务**在跑。

    只认 /api/health 里 service 字段对得上的响应；占用者是别的程序时返回 None，
    交给正常的「端口被占」错误处理（提示换端口）。
    """
    probe_host = "127.0.0.1" if host in ("localhost", "::1") else host
    if not _port_open(probe_host, port):
        return None
    url = f"http://{probe_host}:{port}/api/health"
    try:
        with _DIRECT.open(url, timeout=0.8) as resp:
            if resp.status != 200:
                return None
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) and payload.get("service") == _SERVICE_NAME else None


def setup_logging(verbose: bool = False) -> None:
    config.ensure_dirs()
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    stream.setLevel(logging.DEBUG if verbose else logging.INFO)

    log_file = RotatingFileHandler(config.LOG_DIR / "qice.log", maxBytes=2_000_000,
                                  backupCount=3, encoding="utf-8")
    log_file.setFormatter(formatter)
    log_file.setLevel(logging.DEBUG)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()
    root.addHandler(stream)
    root.addHandler(log_file)
    logging.getLogger("vnpy").setLevel(logging.WARNING)   # vn.py 自身日志很吵


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="期策 · 本地期货量化纸面模拟平台")
    parser.add_argument("--host", default=config.DEFAULT_HOST, help="监听地址，默认仅本机回环")
    parser.add_argument("--port", type=int, default=config.DEFAULT_PORT, help="监听端口")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("--open", action="store_true",
                        help="启动后在默认浏览器中打开页面")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    logger = logging.getLogger("qice.server")

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning("服务将监听 %s，这会暴露到本机以外。本项目是本地演示环境，请勿公网暴露。", args.host)

    url = f"http://{args.host}:{args.port}"

    # 重复启动是最常见的"报错"：其实上一个实例还好好的在跑，
    # 这里先认人 —— 是自己的服务就直接复用，别让用户以为坏了。
    running = probe_running(args.host, args.port)
    if running is not None:
        logger.warning("端口 %s 上已经有一个实例在运行（v%s，策略 %s 个 / 模拟 %s 次），无需重复启动。",
                       args.port, running.get("version", "?"),
                       (running.get("counts") or {}).get("strategies", "?"),
                       (running.get("counts") or {}).get("tradingRuns", "?"))
        logger.warning("直接打开：%s", url)
        logger.warning("要重启服务，先关掉原来那个黑窗口（Ctrl+C），再运行本命令。")
        if args.open and os.environ.get(_NO_OPEN_ENV, "").lower() not in ("1", "true", "yes"):
            webbrowser.open(url)
        return EXIT_ALREADY_RUNNING

    try:
        server = api.make_server(args.host, args.port)
    except OSError as exc:
        # 端口被占是最常见的启动失败原因，给一条能直接照做的提示
        logger.error("无法绑定 %s:%s —— %s", args.host, args.port, exc)
        logger.error("这个端口被别的程序占用了，换个端口重试：")
        logger.error("    run.bat 8766    或    python -m app.server --port 8766")
        return EXIT_PORT_BUSY

    logger.info("期策 v%s 已启动：%s", __version__, url)
    logger.info("数据目录：%s", config.RUNTIME_DIR)
    logger.info("仅限本机教学演示，按 Ctrl+C 停止。")

    # 把上次留在库里的策略运行态接回来（统一恢复成"已暂停"，
    # 毕竟服务重启意味着中间断了一段行情，直接续跑会接出一个解释不了的成交价）
    try:
        restored = runner.restore()
        if restored:
            logger.info("策略常驻交易：恢复 %s 条运行态，均为暂停状态，确认后可在策略库点「恢复交易」。", restored)
    except Exception:                              # noqa: BLE001 - 恢复失败不该挡住服务启动
        logger.exception("恢复策略运行态失败，跳过。")

    if args.open and os.environ.get(_NO_OPEN_ENV, "").lower() not in ("1", "true", "yes"):
        # 延迟一点再开，等 serve_forever 真正进入 accept 循环
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在停止……")
    finally:
        server.server_close()
        from . import simnow
        if simnow.simnow.connected or simnow.simnow.connecting:
            simnow.simnow.disconnect()
        logger.info("本地服务已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
