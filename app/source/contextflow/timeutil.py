"""時刻まわりの共通ヘルパ。全モジュールがここを使い、tz の扱いを揃える。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta


def now() -> datetime:
    """ローカルタイムゾーン付きの現在時刻。"""
    return datetime.now().astimezone()


def today() -> date:
    return now().date()


def day_range(target: date) -> tuple[datetime, datetime]:
    """その日の [00:00, 翌00:00) をローカル tz 付きで返す。"""
    tz = now().tzinfo
    start = datetime.combine(target, time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


def ensure_aware(value: datetime) -> datetime:
    """naive な datetime にローカル tz を付ける。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=now().tzinfo)
    return value


def parse_hhmm(text: str, base: date | None = None) -> datetime:
    """'13:05' や '2026-09-23 13:05' を tz 付き datetime へ。"""
    text = text.strip()
    if " " in text or "T" in text:
        return ensure_aware(datetime.fromisoformat(text.replace(" ", "T")))
    hour, _, minute = text.partition(":")
    target = base or today()
    tz = now().tzinfo
    return datetime.combine(target, time(int(hour), int(minute or 0)), tzinfo=tz)


def minutes_between(start: datetime, end: datetime) -> int:
    return int(round((end - start).total_seconds() / 60))


def overlap_sec(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> int:
    """2区間の重なり秒数。重ならなければ 0。"""
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    return max(0, int((end - start).total_seconds()))


def hhmm(value: datetime) -> str:
    return value.strftime("%H:%M")


def fmt_minutes(total_min: int) -> str:
    """分を '2h 10m' 形式へ。"""
    hours, minutes = divmod(max(0, int(total_min)), 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"
