"""sources/calendar_sync.py

カレンダー取得元（classic Outlook COM / .ics / 将来の Microsoft Graph）を差し替え可能にし、
直近1週間ぶんの予定を日次で取り込んで Activity（layer=PLANNED）として保存する。

取得元の生成 → 期間の決定 → 取得 → フィルタ/マスク/日分割 → 日ごとの保存、という
一連の流れをこのモジュールにまとめる。取得元の実体（OutlookComProvider 等）には
依存せず、CalendarProvider インターフェースだけを見る。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Mapping, Optional, Sequence

from contextflow.config import AppConfig
from contextflow.contracts.calendar import CalendarEvent, CalendarFilter, CalendarProvider
from contextflow.contracts.models import Activity, ActivityLayer, ActivityType, Source
from contextflow.sources.calendar_ics import IcsFileProvider
from contextflow.storage.db import Database
from contextflow.storage.repositories import ActivityRepository, CalendarLabelRepository
from contextflow.timeutil import day_range, today


# ---------------------------------------------------------------------------
# 取得元の生成
# ---------------------------------------------------------------------------


def create_provider(config: AppConfig, name: Optional[str] = None) -> CalendarProvider:
    """設定からカレンダー取得元を1つ組み立てる。

    name 未指定時は config の calendar.provider（既定 "outlook"）を使う。
    "outlook" は classic Outlook COM 経由（calendar_outlook.OutlookComProvider、別モジュールで
    実装）を遅延 import する。import に失敗した場合、または check().available が False の
    場合は、日本語1行の警告を標準エラーへ出し ics（IcsFileProvider）へ退避する
    （判断エンジンの退避チェーンと同じ考え方）。
    """
    provider_name = name or config.get("calendar.provider") or "outlook"

    if provider_name == "ics":
        return IcsFileProvider(config)

    if provider_name == "outlook":
        try:
            from contextflow.sources.calendar_outlook import OutlookComProvider

            provider: CalendarProvider = OutlookComProvider(config)
            status = provider.check()
        except Exception as exc:  # noqa: BLE001 - 未実装・未インストールでも止めないため広く捕捉
            print(
                f"警告: outlook 取得元が使えないため ics へ退避: {exc}",
                file=sys.stderr,
            )
            return IcsFileProvider(config)

        if not status.available:
            print(
                f"警告: outlook 取得元が使えないため ics へ退避: {status.message}",
                file=sys.stderr,
            )
            return IcsFileProvider(config)
        return provider

    raise ValueError(f"未知のカレンダー取得元: {provider_name}")


# ---------------------------------------------------------------------------
# 取得期間の決定
# ---------------------------------------------------------------------------


def fetch_window(
    config: AppConfig,
    *,
    days: Optional[int] = None,
    days_back: Optional[int] = None,
    base: Optional[date] = None,
) -> tuple[datetime, datetime]:
    """取得対象の [start, end) を返す。

    既定は config の calendar.fetch_days(7) / calendar.fetch_days_back(1)。
    base 省略時は今日。戻り値は [base - days_back 日の00:00, base + days 日の00:00)。
    """
    fetch_days = days if days is not None else int(config.get("calendar.fetch_days", 7))
    fetch_days_back = (
        days_back if days_back is not None else int(config.get("calendar.fetch_days_back", 1))
    )
    base_date = base or today()

    start, _ = day_range(base_date - timedelta(days=fetch_days_back))
    end, _ = day_range(base_date + timedelta(days=fetch_days))
    return start, end


# ---------------------------------------------------------------------------
# CalendarEvent -> Activity 変換（フィルタ・マスク・日分割）
# ---------------------------------------------------------------------------


def _build_filter(config: AppConfig) -> CalendarFilter:
    """config の calendar.skip_* から CalendarFilter を組み立てる。"""
    return CalendarFilter(
        skip_all_day=bool(config.get("calendar.skip_all_day", True)),
        skip_declined=bool(config.get("calendar.skip_declined", True)),
        skip_cancelled=bool(config.get("calendar.skip_cancelled", True)),
        skip_free=bool(config.get("calendar.skip_free", True)),
    )


def _apply_filter(
    events: Sequence[CalendarEvent], config: AppConfig
) -> tuple[list[CalendarEvent], dict[str, int]]:
    """CalendarFilter を適用し、残った予定と除外理由の件数を返す。"""
    return _build_filter(config).apply(list(events))


def _compile_mask_patterns(config: AppConfig) -> list[tuple["re.Pattern[str]", str]]:
    """privacy.mask_patterns（正規表現 -> 置換文字列）をコンパイルする。

    collector/collector.py の mask_title と同じ考え方だが、あのモジュールを import せず
    config から直接読んで自前で適用する（実装層どうしの依存を増やさないため）。
    """
    compiled: list[tuple["re.Pattern[str]", str]] = []
    for rule in config.get("privacy.mask_patterns", []) or []:
        pattern = rule.get("pattern", "")
        replacement = rule.get("replacement", "")
        if not pattern:
            continue
        compiled.append((re.compile(pattern), replacement))
    return compiled


def _mask_text(text: str, patterns: list[tuple["re.Pattern[str]", str]]) -> str:
    masked = text
    for compiled, replacement in patterns:
        masked = compiled.sub(replacement, masked)
    return masked


def _split_event_by_day(event: CalendarEvent) -> list[CalendarEvent]:
    """日をまたぐ予定を日境界で分割する（calendar_ics.py の _split_event_by_day と同じ方針）。

    各区間が単一日の [00:00, 翌00:00) に収まるようにし、replace_between の削除範囲
    （日単位）が他日へ漏れ出さないようにする。
    """
    if event.end <= event.start:
        return [event]

    pieces: list[CalendarEvent] = []
    cur = event.start
    while cur < event.end:
        _, day_end = day_range(cur.date())
        piece_end = min(event.end, day_end)
        pieces.append(replace(event, start=cur, end=piece_end))
        cur = piece_end
    return pieces


def _event_to_activity(
    event: CalendarEvent, activity_type: ActivityType = ActivityType.MEETING
) -> Activity:
    """CalendarEvent を Activity（layer=PLANNED / source=CALENDAR）へ変換する。"""
    return Activity(
        start_at=event.start,
        end_at=event.end,
        activity_type=activity_type,
        layer=ActivityLayer.PLANNED,
        source=Source.CALENDAR,
        confidence=1.0,
        summary=event.subject,
        detail={
            "location": event.location,
            "organizer": event.organizer,
            "uid": event.uid,
            "busy_status": event.busy_status,
            "response_status": event.response_status,
        },
    )


def events_to_activities(
    events: Sequence[CalendarEvent],
    config: AppConfig,
    labels: Optional[Mapping[str, ActivityType]] = None,
) -> list[Activity]:
    """予定を Activity(layer=PLANNED) へ変換する。

    順序: CalendarFilter で除外 → mask_subject が true なら件名・場所をマスク
    （件名は日次サマリへ出力されるため）→ 日をまたぐ予定は日境界で分割。

    `labels` は uid -> ActivityType（`CalendarLabelRepository.all()` の戻り値）。
    calendar sync は日ごとに PLANNED 行をまるごと削除→再挿入するため、予定の行自体に
    種別を持たせると再取得のたびに消えてしまう。そのため種別は `calendar_labels`
    テーブルへ別に保持し、「取得後・利用前」にあたるこの変換のタイミングで当てはめる。
    ここより後段（build のマージ / gaps の推定 / タイムライン表示 / 将来の先読み）は
    すべてこの変換結果を見るだけなので、この関門1箇所で全処理に効く。
    `labels` 省略時・uid が該当しない場合は従来どおり MEETING。
    """
    kept, _rejected = _apply_filter(events, config)

    patterns = (
        _compile_mask_patterns(config)
        if bool(config.get("calendar.mask_subject", True))
        else []
    )
    label_map = labels or {}

    activities: list[Activity] = []
    for event in kept:
        masked = event
        if patterns:
            masked = replace(
                event,
                subject=_mask_text(event.subject, patterns),
                location=_mask_text(event.location, patterns),
            )
        activity_type = label_map.get(event.uid, ActivityType.MEETING) if event.uid else (
            ActivityType.MEETING
        )
        for piece in _split_event_by_day(masked):
            activities.append(_event_to_activity(piece, activity_type))
    return activities


# ---------------------------------------------------------------------------
# 同期本体
# ---------------------------------------------------------------------------


@dataclass
class SyncResult:
    """sync_calendar の結果。CLI がそのまま表示できる形。"""

    provider: str
    start: datetime
    end: datetime
    fetched: int
    rejected: dict[str, int]  # 除外理由 -> 件数
    saved: list[Activity]
    message: str  # 人間向けの日本語1行


def _clip_to_window(
    event: CalendarEvent, start: datetime, end: datetime
) -> Optional[CalendarEvent]:
    """予定を [start, end) の範囲内へ切り詰める。重ならなければ None を返す。

    取得元は「重なる予定」を返すだけで範囲の端で切り詰めているとは限らないため、
    ここで切り詰めてから日ごとに分割する。範囲外の日を replace_between で
    誤って触ってしまわないようにするため。
    """
    clipped_start = max(event.start, start)
    clipped_end = min(event.end, end)
    if clipped_end <= clipped_start:
        return None
    return replace(event, start=clipped_start, end=clipped_end)


def sync_calendar(
    db: Database,
    config: AppConfig,
    *,
    provider: Optional[CalendarProvider] = None,
    days: Optional[int] = None,
    days_back: Optional[int] = None,
    base: Optional[date] = None,
) -> SyncResult:
    """取得元から予定を取り込み、日ごとに Activity（layer=PLANNED）として保存する。

    - 取得範囲は fetch_window で決める。
    - 取得範囲内で予定が0件の日も、その日の PLANNED を空で置き換える
      （予定が削除された場合に古い予定が残らないようにするため）。範囲外の日には触れない。
    - provider.fetch が RuntimeError を投げたら、そのまま上位へ伝える。
    - 削除→再挿入という手順のため、複数回実行しても件数は増えない（冪等）。
    - `calendar_labels`（uid -> 種別）を読み、events_to_activities へ渡す。
      人が付けた種別は uid キーでこの別テーブルに残っているため、PLANNED 行を
      作り直しても再取得のたびに消えない。
    """
    active_provider = provider or create_provider(config)
    start, end = fetch_window(config, days=days, days_back=days_back, base=base)

    events = active_provider.fetch(start, end)

    clipped_events: list[CalendarEvent] = []
    for event in events:
        clipped = _clip_to_window(event, start, end)
        if clipped is not None:
            clipped_events.append(clipped)

    _kept, rejected = _apply_filter(clipped_events, config)
    labels = CalendarLabelRepository(db).all()
    activities = events_to_activities(clipped_events, config, labels)

    # 取得範囲に含まれる全ての日を、予定の有無に関わらず対象にする
    # （0件の日も空で置き換えることで、削除された予定を古いまま残さない）。
    by_day: dict[date, list[Activity]] = {}
    cur_day = start.date()
    while cur_day < end.date():
        by_day[cur_day] = []
        cur_day += timedelta(days=1)
    for activity in activities:
        by_day.setdefault(activity.start_at.date(), []).append(activity)

    repo = ActivityRepository(db)
    saved: list[Activity] = []
    for day, day_activities in sorted(by_day.items()):
        day_start, day_end = day_range(day)
        repo.replace_between(day_start, day_end, day_activities, layer=ActivityLayer.PLANNED)
        saved.extend(day_activities)

    rejected_total = sum(rejected.values())
    message = (
        f"カレンダー取得元 {active_provider.name}: "
        f"{len(events)}件取得 → 除外{rejected_total}件 → {len(saved)}件保存"
        f"（{start.date()}〜{(end - timedelta(days=1)).date()}）"
    )

    return SyncResult(
        provider=active_provider.name,
        start=start,
        end=end,
        fetched=len(events),
        rejected=rejected,
        saved=saved,
        message=message,
    )
