"""进程入口：`python -m app.server [--port 8765]`。

只负责三件事：配置日志、绑定端口、优雅退出。业务全部在 app/ 其它模块。
"""
from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler

from . import __version__, api, config


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
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    logger = logging.getLogger("qice.server")

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        logger.warning("服务将监听 %s，这会暴露到本机以外。本项目是本地演示环境，请勿公网暴露。", args.host)

    server = api.make_server(args.host, args.port)
    logger.info("期策 v%s 已启动：http://%s:%s", __version__, args.host, args.port)
    logger.info("数据目录：%s", config.RUNTIME_DIR)
    logger.info("仅限本机教学演示，按 Ctrl+C 停止。")
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
