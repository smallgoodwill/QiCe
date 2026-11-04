"""行情 CSV 读取。

只做一件事：把 CSV 解析成 engine 能接受的 bar 列表，并复用同一套校验，
避免"浏览器解析"和"服务端解析"两套规则再次跑偏。
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

from . import config, engine

REQUIRED_FIELDS = ("timestamp", "open", "high", "low", "close", "volume")


def parse_csv_text(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    missing = [name for name in REQUIRED_FIELDS if name not in (reader.fieldnames or [])]
    if missing:
        raise ValueError("CSV 缺少字段：" + "、".join(missing))
    rows = []
    for line_no, row in enumerate(reader, start=2):
        if not any((value or "").strip() for value in row.values()):
            continue
        try:
            rows.append({"timestamp": (row["timestamp"] or "").strip(),
                         "open": float(row["open"]), "high": float(row["high"]),
                         "low": float(row["low"]), "close": float(row["close"]),
                         "volume": float(row["volume"]),
                         "event_blackout": (row.get("event_blackout") or "").strip().lower()
                                           in ("1", "true", "yes", "y")})
        except (TypeError, ValueError):
            raise ValueError(f"CSV 第 {line_no} 行数值格式无效。") from None
    return engine.validate_bars(rows)


def load_csv(path: Path) -> list[dict]:
    return parse_csv_text(Path(path).read_text(encoding="utf-8-sig"))


def sample_bars() -> list[dict]:
    """内置合成样例行情，用于零数据时跑通全链路（明确标注非真实行情）。"""
    if not config.SAMPLE_CSV.exists():
        raise ValueError("内置样例行情不存在，请检查 data/samples/ 目录。")
    return load_csv(config.SAMPLE_CSV)
