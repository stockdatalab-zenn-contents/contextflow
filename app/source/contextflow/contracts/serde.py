"""dataclass <-> JSON 変換の共通ヘルパ。

datetime / date / Enum を素直な JSON 値へ落とすだけの薄い層。
外部ライブラリには依存しない。
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime
from enum import Enum
from typing import Any


def to_jsonable(value: Any) -> Any:
    """dataclass・Enum・datetime を含む任意の値を JSON 化可能な形へ変換。"""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: to_jsonable(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    return value


def parse_datetime(text: Any) -> Any:
    """ISO8601 文字列を datetime へ。None・datetime はそのまま返す。"""
    if text is None or isinstance(text, datetime):
        return text
    return datetime.fromisoformat(str(text))


def parse_date(text: Any) -> Any:
    """ISO8601 文字列を date へ。None・date はそのまま返す。"""
    if text is None or isinstance(text, date) and not isinstance(text, datetime):
        return text
    if isinstance(text, datetime):
        return text.date()
    return date.fromisoformat(str(text))
