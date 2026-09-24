"""sources/calendar_ics.py

カレンダー（.ics）を「予定」として取り込む。標準ライブラリのみで書いた最小限の ICS パーサ。
参照資料の方針どおり、カレンダーは「実際にやったこと」ではなく「予定」として扱う
（layer=PLANNED）。実績との差分は上位レイヤ（pipeline 側）でマージする。
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from contextflow.config import AppConfig
from contextflow.contracts.calendar import CalendarEvent, CalendarProvider, ProviderStatus
from contextflow.contracts.models import Activity, ActivityLayer, ActivityType, Source
from contextflow.storage.db import Database
from contextflow.storage.repositories import ActivityRepository
from contextflow.timeutil import day_range, ensure_aware, now

# SUMMARY 等のテキストで使われるエスケープ（RFC5545 TEXT 型）
_ESCAPE_MAP = {
    "\\n": "\n",
    "\\N": "\n",
    "\\,": ",",
    "\\;": ";",
    "\\\\": "\\",
}
_ESCAPE_RE = re.compile(r"\\[nN,;\\]")


def _unescape_text(value: str) -> str:
    """`\\,` `\\;` `\\n` などのエスケープを解除する。"""
    return _ESCAPE_RE.sub(lambda m: _ESCAPE_MAP[m.group(0)], value)


def _unfold_lines(text: str) -> list[str]:
    """行継続（次行が空白/タブで始まる場合の折り返し）を先に連結する。"""
    unfolded: list[str] = []
    for raw_line in text.splitlines():
        if raw_line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += raw_line[1:]
        else:
            unfolded.append(raw_line)
    return unfolded


def _split_property(line: str) -> tuple[str, dict[str, str], str]:
    """`DTSTART;TZID=Asia/Tokyo:20260923T130000` のような1行を
    (プロパティ名, パラメータ dict, 値) に分解する。
    """
    if ":" not in line:
        return "", {}, ""
    left, _, value = line.partition(":")
    name, *param_parts = left.split(";")
    params: dict[str, str] = {}
    for part in param_parts:
        if "=" in part:
            key, _, val = part.partition("=")
            params[key.strip().upper()] = val.strip()
    return name.strip().upper(), params, value


def _parse_ics_datetime(value: str, params: dict[str, str]) -> tuple[datetime, bool]:
    """DTSTART/DTEND の値を tz 付き datetime に変換する。

    対応する3形式:
      - `TZID=...:20260923T130000`（TZID を実タイムゾーンとして解決してローカル tz へ変換。
        tzdata が無く解決できない環境ではローカル tz の壁時計時刻とみなす）
      - `20260923T040000Z`（UTC として解釈しローカル tz へ変換）
      - `VALUE=DATE:20260923`（終日）
    戻り値の bool は終日イベントかどうか。
    """
    value = value.strip()
    is_all_day = params.get("VALUE", "").upper() == "DATE" or (
        len(value) == 8 and value.isdigit()
    )
    if is_all_day:
        d = datetime.strptime(value, "%Y%m%d").date()
        local_tz = now().tzinfo
        return datetime.combine(d, time.min, tzinfo=local_tz), True

    if value.endswith("Z"):
        naive = datetime.strptime(value[:-1], "%Y%m%dT%H%M%S")
        aware_utc = naive.replace(tzinfo=timezone.utc)
        return aware_utc.astimezone(), False

    naive = datetime.strptime(value, "%Y%m%dT%H%M%S")
    tzid = params.get("TZID")
    if tzid:
        try:
            # TZID を実際のタイムゾーンとして解決する（Asia/Tokyo 以外でも正しく扱うため）。
            source_tz = ZoneInfo(tzid)
        except Exception:
            # この Windows 環境には IANA タイムゾーンDB（tzdata）が入っておらず、
            # 未知の TZID では ZoneInfoNotFoundError になることがある。
            # その場合は例外にせず、従来どおりローカル tz の壁時計時刻として解釈する
            # （フォールバック。tzdata さえ入れば自動的に本来の tz で解決されるようになる）。
            source_tz = None
        if source_tz is not None:
            return naive.replace(tzinfo=source_tz).astimezone(), False

    # TZID が無い、または解決できなかった場合はローカル tz の壁時計時刻として扱う
    return ensure_aware(naive), False


def _finalize_event(raw: dict[str, Any]) -> Optional[dict]:
    """VEVENT ブロックから収集した生データを parse_ics の出力形式へ変換する。"""
    if "dtstart" not in raw:
        return None
    start_value, start_params = raw["dtstart"]
    try:
        start_dt, start_all_day = _parse_ics_datetime(start_value, start_params)
    except ValueError:
        return None

    default_gap = timedelta(days=1) if start_all_day else timedelta(hours=1)
    end_dt = start_dt + default_gap
    if "dtend" in raw:
        end_value, end_params = raw["dtend"]
        try:
            end_dt, _ = _parse_ics_datetime(end_value, end_params)
        except ValueError:
            end_dt = start_dt + default_gap

    return {
        "summary": _unescape_text(raw.get("summary", "")),
        "start": start_dt,
        "end": end_dt,
        "location": _unescape_text(raw.get("location", "")),
        "uid": raw.get("uid", ""),
    }


def parse_ics(text: str) -> list[dict]:
    """ICS テキストから VEVENT を抽出する。

    標準ライブラリのみの最小パーサ。BEGIN:VEVENT 〜 END:VEVENT の間の
    SUMMARY / DTSTART / DTEND / LOCATION / UID だけを見る。
    戻り値: [{'summary', 'start', 'end', 'location', 'uid'}, ...]
    """
    events: list[dict] = []
    in_event = False
    current: dict[str, Any] = {}

    for line in _unfold_lines(text):
        stripped = line.strip()
        if not stripped:
            continue
        upper = stripped.upper()

        if upper == "BEGIN:VEVENT":
            in_event = True
            current = {}
            continue
        if upper == "END:VEVENT":
            if in_event:
                finalized = _finalize_event(current)
                if finalized is not None:
                    events.append(finalized)
            in_event = False
            current = {}
            continue
        if not in_event:
            continue

        name, params, value = _split_property(stripped)
        if name == "SUMMARY":
            current["summary"] = value
        elif name == "LOCATION":
            current["location"] = value
        elif name == "DTSTART":
            current["dtstart"] = (value, params)
        elif name == "DTEND":
            current["dtend"] = (value, params)
        elif name == "UID":
            current["uid"] = value

    return events


def _event_to_activity(event: dict) -> Activity:
    """parse_ics の1件を Activity（layer=PLANNED / source=CALENDAR）へ変換する。"""
    return Activity(
        start_at=event["start"],
        end_at=event["end"],
        activity_type=ActivityType.MEETING,
        layer=ActivityLayer.PLANNED,
        source=Source.CALENDAR,
        confidence=1.0,
        summary=event.get("summary", ""),
        detail={"location": event.get("location", "")},
    )


def _clip_event(event: dict, day_start: datetime, day_end: datetime) -> Optional[dict]:
    """イベントの区間を [day_start, day_end) へクリップする。重ならなければ None。

    target 指定時に、日をまたぐイベント（例: 23:00-翌01:00）の一部だけを
    その日の分として取り込むために使う。
    """
    clipped_start = max(event["start"], day_start)
    clipped_end = min(event["end"], day_end)
    if clipped_end <= clipped_start:
        return None
    clipped = dict(event)
    clipped["start"] = clipped_start
    clipped["end"] = clipped_end
    return clipped


def _split_event_by_day(event: dict) -> list[dict]:
    """日をまたぐイベントを日境界で分割する。

    各区間が単一日の [00:00, 翌00:00) に収まるようにし、
    replace_between の削除範囲（日単位）が他日へ漏れ出さないようにする。
    """
    start = event["start"]
    end = event["end"]
    if end <= start:
        # 不正・ゼロ長区間はそのまま1件として扱う（分割不要）
        return [event]

    pieces: list[dict] = []
    cur = start
    while cur < end:
        _, day_end = day_range(cur.date())
        piece_end = min(end, day_end)
        piece = dict(event)
        piece["start"] = cur
        piece["end"] = piece_end
        pieces.append(piece)
        cur = piece_end
    return pieces


def import_ics_dir(
    db: Database, config: AppConfig, target: date | None = None
) -> list[Activity]:
    """`config.path("calendar_dir")` 配下の *.ics を全て読み、Activity として保存する。

    - target 指定時: 削除範囲・対象イベントともに target の1日（day_range(target)）
      に限定する。その日に重なるイベントだけを、その日の範囲へクリップして置き換える。
      （日をまたぐ予定や他日の予定が同じグループに混ざり、他日の planned が
      巻き添えで全消しされる不具合の対策）
    - target 省略時: 取り込み対象イベントが実際に存在する日だけを、日ごとに
      day_range 単位で置き換える（イベントの無い日の既存 planned は消さない）。
    - いずれの場合も、日をまたぐイベントは日境界で分割してから各日の区間に入れるため、
      削除範囲が分割後の区間の日からはみ出すことはない。
    - 同じ .ics を複数回取り込んでも、対象範囲の削除→再挿入という手順のため
      重複せず（冪等）、対象外の日には一切触れない。

    ディレクトリが無い・ファイルが無い場合は空リストを返す（例外にしない）。
    """
    try:
        calendar_dir = config.path("calendar_dir")
    except KeyError:
        return []
    if not calendar_dir.is_dir():
        return []

    ics_files = sorted(calendar_dir.glob("*.ics"))
    if not ics_files:
        return []

    raw_events: list[dict] = []
    for ics_path in ics_files:
        try:
            text = ics_path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        raw_events.extend(parse_ics(text))

    if not raw_events:
        return []

    repo = ActivityRepository(db)
    saved: list[Activity] = []

    if target is not None:
        # target の1日だけを対象に、削除範囲もその1日に限定して置き換える。
        day_start, day_end = day_range(target)
        day_activities: list[Activity] = []
        for event in raw_events:
            clipped = _clip_event(event, day_start, day_end)
            if clipped is not None:
                day_activities.append(_event_to_activity(clipped))
        repo.replace_between(day_start, day_end, day_activities, layer=ActivityLayer.PLANNED)
        saved.extend(day_activities)
        return saved

    # target 省略時: 日をまたぐイベントは日境界で分割し、
    # 実際にイベントが存在する日だけを日ごとに replace_between する。
    by_day: dict[date, list[Activity]] = {}
    for event in raw_events:
        for piece in _split_event_by_day(event):
            activity = _event_to_activity(piece)
            by_day.setdefault(activity.start_at.date(), []).append(activity)

    for day, day_activities in sorted(by_day.items()):
        day_start, day_end = day_range(day)
        repo.replace_between(day_start, day_end, day_activities, layer=ActivityLayer.PLANNED)
        saved.extend(day_activities)

    return saved


# ---------------------------------------------------------------------------
# CalendarProvider 実装: .ics ファイル
# ---------------------------------------------------------------------------


def _raw_event_is_all_day(raw: dict) -> bool:
    """parse_ics の1件が終日予定かどうかを推定する。

    parse_ics は VALUE=DATE を判定したフラグ自体は返さないため、
    `_parse_ics_datetime` が終日予定を `time.min`（ローカル tz の 00:00）へ
    正規化して返す性質を利用し、開始・終了とも 00:00 ちょうどかで判定する。
    """
    return raw["start"].time() == time.min and raw["end"].time() == time.min


def _raw_event_to_calendar_event(raw: dict) -> CalendarEvent:
    """parse_ics の1件を CalendarEvent へ変換する。

    .ics からは出欠・キャンセル情報が取れないため busy_status / response_status は
    "unknown" とする。UID は parse_ics が拾った値をそのまま使う（無ければ空文字。
    種別ラベル機能は uid の無い予定には対応できない）。
    """
    return CalendarEvent(
        start=raw["start"],
        end=raw["end"],
        subject=raw.get("summary", ""),
        location=raw.get("location", ""),
        organizer="",
        is_all_day=_raw_event_is_all_day(raw),
        is_cancelled=False,
        is_recurring=False,
        busy_status="unknown",
        response_status="unknown",
        uid=raw.get("uid", ""),
        provider="ics",
    )


class IcsFileProvider(CalendarProvider):
    """paths.calendar_dir に置かれた .ics を読む取得元。"""

    name = "ics"

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def check(self) -> ProviderStatus:
        """ディレクトリと *.ics の有無を日本語で説明する。"""
        try:
            calendar_dir = self._config.path("calendar_dir")
        except KeyError:
            return ProviderStatus(available=False, message="paths.calendar_dir が未設定")
        if not calendar_dir.is_dir():
            return ProviderStatus(
                available=False,
                message=f"カレンダーディレクトリが存在しない: {calendar_dir}",
            )
        ics_files = sorted(calendar_dir.glob("*.ics"))
        if not ics_files:
            return ProviderStatus(
                available=False,
                message=f"{calendar_dir} に .ics ファイルが無い",
            )
        return ProviderStatus(
            available=True,
            message=f"{len(ics_files)} 件の .ics ファイルを検出: {calendar_dir}",
        )

    def fetch(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        """`parse_ics` を再利用し、[start, end) に重なるものだけ返す。"""
        status = self.check()
        if not status.available:
            raise RuntimeError(status.message)

        calendar_dir = self._config.path("calendar_dir")
        events: list[CalendarEvent] = []
        for ics_path in sorted(calendar_dir.glob("*.ics")):
            try:
                text = ics_path.read_text(encoding="utf-8-sig")
            except OSError:
                continue
            for raw in parse_ics(text):
                if raw["end"] <= start or raw["start"] >= end:
                    continue  # [start, end) に重ならない
                events.append(_raw_event_to_calendar_event(raw))
        return events
