"""tests/test_calendar.py

「Outlook からの予定取得」まわりのテスト。

対象:
  - contracts/calendar.py の CalendarFilter（除外ルール）
  - sources/calendar_outlook.py の OutlookComProvider / format_restrict_datetime / to_event
  - sources/calendar_sync.py の create_provider / fetch_window / events_to_activities /
    sync_calendar

標準ライブラリの unittest のみを使用する。実際の Outlook 起動・COM の実呼び出し・
ネットワークアクセスは一切行わない。OutlookComProvider は `dispatch=...` で
偽の COM オブジェクトへ差し替えてテストする（このモジュール内に偽物を定義する）。

DB・ファイルは `tempfile.TemporaryDirectory` を使い、プロジェクト内（特に app/data）は
汚さない。実際の設定ファイル（config.toml）は読んでよいが書き換えない。設定を変えたい
箇所は AppConfig のインスタンスを作って data を直接組み立てる（tests/test_mode.py と
同じ方針）。
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.conftest_path import add_source_path

add_source_path()

from contextflow.config import AppConfig  # noqa: E402
from contextflow.contracts.calendar import (  # noqa: E402
    CalendarEvent,
    CalendarFilter,
    CalendarProvider,
    ProviderStatus,
)
from contextflow.contracts.models import (  # noqa: E402
    Activity,
    ActivityLayer,
    ActivityType,
    Source,
)
from contextflow.sources.calendar_ics import IcsFileProvider  # noqa: E402
from contextflow.sources.calendar_outlook import (  # noqa: E402
    OutlookComProvider,
    format_restrict_datetime,
    to_event,
)
from contextflow.sources.calendar_sync import (  # noqa: E402
    create_provider,
    events_to_activities,
    fetch_window,
    sync_calendar,
)
from contextflow.storage.db import Database  # noqa: E402
from contextflow.storage.repositories import (  # noqa: E402
    ActivityRepository,
    CalendarLabelRepository,
)
from contextflow.timeutil import day_range  # noqa: E402

# 固定オフセットの JST。zoneinfo は Windows 環境で tzdata が無いと落ちるため使わない
# （tests/test_sources.py と同じ方針）。
JST = timezone(timedelta(hours=9))


def _dt(hour: int, minute: int = 0, day: int = 23) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=JST)


# ---------------------------------------------------------------------------
# CalendarFilter
# ---------------------------------------------------------------------------


class CalendarFilterTest(unittest.TestCase):
    def _base_event(self, **overrides) -> CalendarEvent:
        base = dict(
            start=_dt(9),
            end=_dt(10),
            subject="s",
            busy_status="busy",
            response_status="accepted",
            is_all_day=False,
            is_cancelled=False,
        )
        base.update(overrides)
        return CalendarEvent(**base)

    def test_each_reason_is_rejected_with_counts(self) -> None:
        ev_ok = self._base_event()
        ev_all_day = self._base_event(is_all_day=True)
        ev_declined = self._base_event(response_status="declined")
        ev_cancelled = self._base_event(is_cancelled=True)
        ev_free = self._base_event(busy_status="free")
        ev_invalid = self._base_event(start=_dt(10), end=_dt(9))  # end <= start

        kept, rejected = CalendarFilter().apply(
            [ev_ok, ev_all_day, ev_declined, ev_cancelled, ev_free, ev_invalid]
        )

        self.assertEqual(kept, [ev_ok])
        self.assertEqual(
            rejected,
            {
                "終日予定": 1,
                "辞退済み": 1,
                "キャンセル済み": 1,
                "空き時間扱い": 1,
                "区間が不正": 1,
            },
        )

    def test_skip_flags_false_keep_each_condition(self) -> None:
        filt = CalendarFilter(
            skip_all_day=False,
            skip_declined=False,
            skip_cancelled=False,
            skip_free=False,
        )
        ev_all_day = self._base_event(is_all_day=True)
        ev_declined = self._base_event(response_status="declined")
        ev_cancelled = self._base_event(is_cancelled=True)
        ev_free = self._base_event(busy_status="free")

        for ev in (ev_all_day, ev_declined, ev_cancelled, ev_free):
            self.assertIsNone(filt.reject_reason(ev))

        # 区間不正だけは skip_* に依らず常に除外される
        ev_invalid = self._base_event(start=_dt(10), end=_dt(9))
        self.assertEqual(filt.reject_reason(ev_invalid), "区間が不正")


# ---------------------------------------------------------------------------
# OutlookComProvider 用の偽 COM オブジェクト
# 実際の Outlook・win32com は一切使わない。
# ---------------------------------------------------------------------------


class _FakeItems:
    """calendar.Items の偽物。

    Sort() / IncludeRecurrences 代入の呼び出し順と、Restrict() に渡された
    条件式を記録する。restrict_fail_times 回だけ Restrict を失敗させられる。
    """

    def __init__(self, items, *, restrict_fail_times: int = 0, restrict_result=None) -> None:
        self._items = items
        self.call_log: list[tuple[str, object]] = []
        self.restrict_calls: list[str] = []
        self._restrict_fail_times = restrict_fail_times
        self._restrict_result = restrict_result
        self._include_recurrences = None

    def Sort(self, key):
        self.call_log.append(("Sort", key))

    @property
    def IncludeRecurrences(self):
        return self._include_recurrences

    @IncludeRecurrences.setter
    def IncludeRecurrences(self, value):
        self.call_log.append(("IncludeRecurrences", value))
        self._include_recurrences = value

    def Restrict(self, condition):
        self.restrict_calls.append(condition)
        if len(self.restrict_calls) <= self._restrict_fail_times:
            raise RuntimeError(f"fake Restrict failure: {condition}")
        return self._restrict_result if self._restrict_result is not None else self

    def __iter__(self):
        return iter(self._items)


def _make_dispatch(items):
    """OutlookComProvider(dispatch=...) に渡す偽の dispatch 関数を作る。

    dispatch(prog_id) -> outlook.GetNamespace("MAPI") -> .GetDefaultFolder(9) -> .Items
    という呼び出し連鎖を、実際の COM を一切使わずに再現する。
    """
    folder = SimpleNamespace(Items=items)
    namespace = SimpleNamespace(GetDefaultFolder=lambda folder_id: folder)
    app = SimpleNamespace(GetNamespace=lambda name: namespace)
    return lambda prog_id: app


def _item(start, end, **kwargs):
    """AppointmentItem 相当の偽オブジェクト。

    未指定の属性は SimpleNamespace の性質上 AttributeError になり、
    実際の COM オブジェクトで属性が欠けている場合の振る舞いに近い。
    """
    return SimpleNamespace(Start=start, End=end, **kwargs)


class OutlookComProviderTest(unittest.TestCase):
    def test_check_without_pywin32_returns_unavailable_without_exception(self) -> None:
        provider = OutlookComProvider()
        with mock.patch.dict(sys.modules, {"win32com": None, "win32com.client": None}):
            status = provider.check()  # 例外にならないこと
        self.assertFalse(status.available)
        self.assertIn("pywin32", status.message)
        # 日本語メッセージであること（全角文字を含む）
        self.assertTrue(any(ord(ch) > 0x3000 for ch in status.message))

    def test_fetch_returns_events_sorted_by_start(self) -> None:
        items = _FakeItems(
            [
                _item(_dt(14), _dt(15), Subject="B"),
                _item(_dt(9), _dt(10), Subject="A"),
            ]
        )
        provider = OutlookComProvider(dispatch=_make_dispatch(items))

        events = provider.fetch(_dt(0), _dt(23, 59))

        self.assertEqual([e.subject for e in events], ["A", "B"])

    def test_sort_called_before_include_recurrences(self) -> None:
        items = _FakeItems([])
        provider = OutlookComProvider(dispatch=_make_dispatch(items))

        provider.fetch(_dt(0), _dt(23, 59))

        names = [name for name, _ in items.call_log]
        self.assertIn("Sort", names)
        self.assertIn("IncludeRecurrences", names)
        self.assertLess(names.index("Sort"), names.index("IncludeRecurrences"))

    def test_restrict_retries_with_alternate_format_on_first_failure(self) -> None:
        items = _FakeItems([_item(_dt(9), _dt(10), Subject="ok")], restrict_fail_times=1)
        provider = OutlookComProvider(dispatch=_make_dispatch(items))

        events = provider.fetch(_dt(0), _dt(23, 59))

        self.assertEqual(len(items.restrict_calls), 2)
        self.assertNotEqual(items.restrict_calls[0], items.restrict_calls[1])
        self.assertEqual([e.subject for e in events], ["ok"])

    def test_restrict_double_failure_falls_back_to_full_scan(self) -> None:
        in_range = _item(_dt(9), _dt(10), Subject="in")
        out_of_range = _item(_dt(9, day=24), _dt(10, day=24), Subject="out")
        items = _FakeItems([in_range, out_of_range], restrict_fail_times=2)
        provider = OutlookComProvider(dispatch=_make_dispatch(items))

        events = provider.fetch(_dt(0), _dt(23, 59))

        self.assertEqual(len(items.restrict_calls), 2)  # 2書式とも試した
        self.assertEqual([e.subject for e in events], ["in"])

    def test_missing_attributes_use_defaults(self) -> None:
        item = _item(_dt(9), _dt(10))  # Subject 等の属性を一切持たない

        event = to_event(item)

        self.assertIsNotNone(event)
        self.assertEqual(event.subject, "")
        self.assertEqual(event.location, "")
        self.assertEqual(event.organizer, "")
        self.assertFalse(event.is_all_day)
        self.assertFalse(event.is_cancelled)
        self.assertFalse(event.is_recurring)
        self.assertEqual(event.busy_status, "unknown")
        self.assertEqual(event.response_status, "unknown")
        self.assertEqual(event.uid, "")
        self.assertEqual(event.categories, [])

    def test_meeting_status_response_status_busy_status_conversion(self) -> None:
        item = _item(_dt(9), _dt(10), MeetingStatus=5, ResponseStatus=4, BusyStatus=0)

        event = to_event(item)

        self.assertTrue(event.is_cancelled)
        self.assertEqual(event.response_status, "declined")
        self.assertEqual(event.busy_status, "free")

    def test_format_restrict_datetime_returns_expected_string(self) -> None:
        dt = _dt(13, 5)
        self.assertEqual(format_restrict_datetime(dt, "%Y/%m/%d %H:%M"), "2026/09/23 13:05")
        self.assertEqual(format_restrict_datetime(dt), "09/23/2026 01:05 PM")


# ---------------------------------------------------------------------------
# calendar_sync: fetch_window
# ---------------------------------------------------------------------------


class FetchWindowTest(unittest.TestCase):
    def test_default_window_is_previous_day_to_seven_days_later(self) -> None:
        config = AppConfig(data={})
        base = date(2026, 9, 23)

        start, end = fetch_window(config, base=base)

        expected_start, _ = day_range(date(2026, 9, 22))
        expected_end, _ = day_range(date(2026, 9, 30))
        self.assertEqual(start, expected_start)
        self.assertEqual(end, expected_end)

    def test_config_values_used_when_arguments_omitted(self) -> None:
        config = AppConfig(data={"calendar": {"fetch_days": 3, "fetch_days_back": 2}})
        base = date(2026, 9, 23)

        start, end = fetch_window(config, base=base)

        expected_start, _ = day_range(date(2026, 9, 21))
        expected_end, _ = day_range(date(2026, 9, 26))
        self.assertEqual(start, expected_start)
        self.assertEqual(end, expected_end)

    def test_days_and_days_back_arguments_override_config(self) -> None:
        config = AppConfig(data={"calendar": {"fetch_days": 3, "fetch_days_back": 2}})
        base = date(2026, 9, 23)

        start, end = fetch_window(config, days=1, days_back=0, base=base)

        expected_start, _ = day_range(date(2026, 9, 23))
        expected_end, _ = day_range(date(2026, 9, 24))
        self.assertEqual(start, expected_start)
        self.assertEqual(end, expected_end)


# ---------------------------------------------------------------------------
# calendar_sync: events_to_activities
# ---------------------------------------------------------------------------


class EventsToActivitiesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AppConfig(data={})

    def test_basic_fields_are_planned_calendar_meeting(self) -> None:
        event = CalendarEvent(start=_dt(9), end=_dt(10), subject="Meeting", location="Room")

        activities = events_to_activities([event], self.config)

        self.assertEqual(len(activities), 1)
        activity = activities[0]
        self.assertEqual(activity.layer, ActivityLayer.PLANNED)
        self.assertEqual(activity.source, Source.CALENDAR)
        self.assertEqual(activity.activity_type, ActivityType.MEETING)
        self.assertEqual(activity.summary, "Meeting")
        self.assertEqual(activity.detail["location"], "Room")

    def test_mask_patterns_applied_to_subject_and_location(self) -> None:
        # config.toml の [[privacy.mask_patterns]] と同内容（メールアドレス・URL）
        config = AppConfig(
            data={
                "privacy": {
                    "mask_patterns": [
                        {"pattern": r"[\w.+-]+@[\w-]+\.[\w.]+", "replacement": "<mail>"},
                        {"pattern": r"https?://[^\s]+", "replacement": "<url>"},
                    ]
                }
            }
        )
        event = CalendarEvent(
            start=_dt(9),
            end=_dt(10),
            subject="Sync with foo@example.com",
            location="https://example.com/room",
        )

        activities = events_to_activities([event], config)

        self.assertEqual(activities[0].summary, "Sync with <mail>")
        self.assertEqual(activities[0].detail["location"], "<url>")

    def test_mask_subject_false_disables_masking(self) -> None:
        config = AppConfig(
            data={
                "calendar": {"mask_subject": False},
                "privacy": {
                    "mask_patterns": [
                        {"pattern": r"[\w.+-]+@[\w-]+\.[\w.]+", "replacement": "<mail>"}
                    ]
                },
            }
        )
        event = CalendarEvent(start=_dt(9), end=_dt(10), subject="foo@example.com")

        activities = events_to_activities([event], config)

        self.assertEqual(activities[0].summary, "foo@example.com")

    def test_event_spanning_midnight_is_split_by_day(self) -> None:
        # ローカル日境界（day_range）そのものを使い、実行環境の tz に依らず検証する
        _, day1_end = day_range(date(2026, 9, 23))
        event = CalendarEvent(start=day1_end - timedelta(hours=1), end=day1_end + timedelta(hours=1))

        activities = events_to_activities([event], self.config)

        self.assertEqual(len(activities), 2)
        self.assertEqual(activities[0].start_at.date(), date(2026, 9, 23))
        self.assertEqual(activities[0].end_at, day1_end)
        self.assertEqual(activities[1].start_at, day1_end)
        self.assertEqual(activities[1].start_at.date(), date(2026, 9, 24))

    def test_without_labels_all_events_stay_meeting(self) -> None:
        # labels 未指定（第3引数省略）は従来どおり全件 meeting のまま
        event = CalendarEvent(start=_dt(9), end=_dt(10), subject="Meeting", uid="uid-a")

        activities = events_to_activities([event], self.config)

        self.assertEqual(activities[0].activity_type, ActivityType.MEETING)

    def test_labels_override_activity_type_only_for_matching_uid(self) -> None:
        labeled = CalendarEvent(start=_dt(9), end=_dt(10), subject="A", uid="uid-a")
        unlabeled = CalendarEvent(start=_dt(11), end=_dt(12), subject="B", uid="uid-b")

        activities = events_to_activities(
            [labeled, unlabeled], self.config, {"uid-a": ActivityType.CODING}
        )

        by_summary = {a.summary: a.activity_type for a in activities}
        self.assertEqual(by_summary["A"], ActivityType.CODING)
        self.assertEqual(by_summary["B"], ActivityType.MEETING)


# ---------------------------------------------------------------------------
# calendar_sync: create_provider
# ---------------------------------------------------------------------------


class CreateProviderTest(unittest.TestCase):
    def test_ics_name_returns_ics_provider(self) -> None:
        config = AppConfig(data={})

        provider = create_provider(config, "ics")

        self.assertIsInstance(provider, IcsFileProvider)

    def test_unknown_name_raises_value_error(self) -> None:
        config = AppConfig(data={})

        with self.assertRaises(ValueError):
            create_provider(config, "no_such_provider")

    def test_outlook_without_pywin32_falls_back_to_ics_with_warning(self) -> None:
        config = AppConfig(data={})

        with mock.patch.dict(sys.modules, {"win32com": None, "win32com.client": None}):
            with mock.patch("sys.stderr", new_callable=io.StringIO) as fake_stderr:
                provider = create_provider(config, "outlook")

        self.assertIsInstance(provider, IcsFileProvider)
        self.assertIn("警告", fake_stderr.getvalue())


# ---------------------------------------------------------------------------
# calendar_sync: sync_calendar
# ---------------------------------------------------------------------------


class _FakeProvider(CalendarProvider):
    """CalendarProvider の偽実装。固定の events を [start, end) で絞って返す。"""

    name = "fake"

    def __init__(self, events: list[CalendarEvent]) -> None:
        self._events = events

    def check(self) -> ProviderStatus:
        return ProviderStatus(True, "fake provider")

    def fetch(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        return [e for e in self._events if e.start < end and e.end >= start]


class SyncCalendarTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "test.db"
        self.db = Database(db_path)
        self.db.initialize()
        # fetch_days=2, fetch_days_back=1, base=9/23 → 対象は 9/22, 9/23, 9/24 の3日
        self.config = AppConfig(data={"calendar": {"fetch_days": 2, "fetch_days_back": 1}})
        self.base = date(2026, 9, 23)

    def tearDown(self) -> None:
        self.db.close()
        self._tmpdir.cleanup()

    def _wide_range(self) -> tuple[datetime, datetime]:
        return _dt(0, day=1), _dt(0, day=30)

    def test_sync_saves_and_is_idempotent(self) -> None:
        events = [
            CalendarEvent(start=_dt(9, day=22), end=_dt(10, day=22), subject="d-1"),
            CalendarEvent(start=_dt(9, day=23), end=_dt(10, day=23), subject="d0"),
            CalendarEvent(start=_dt(9, day=24), end=_dt(10, day=24), subject="d1"),
        ]
        provider = _FakeProvider(events)

        result1 = sync_calendar(self.db, self.config, provider=provider, base=self.base)
        self.assertEqual(len(result1.saved), 3)

        result2 = sync_calendar(self.db, self.config, provider=provider, base=self.base)
        self.assertEqual(len(result2.saved), 3)

        repo = ActivityRepository(self.db)
        saved_all = repo.list_between(*self._wide_range())
        self.assertEqual(len(saved_all), 3)  # 2回実行しても重複しない

    def test_zero_events_day_clears_old_planned_but_out_of_window_day_stays(self) -> None:
        repo = ActivityRepository(self.db)
        # 取得範囲外（9/21）に古い PLANNED を仕込んでおく
        out_of_window = Activity(
            start_at=_dt(9, day=21),
            end_at=_dt(10, day=21),
            activity_type=ActivityType.MEETING,
            layer=ActivityLayer.PLANNED,
            source=Source.CALENDAR,
            summary="old-out-of-window",
        )
        repo.add(out_of_window)

        events = [
            CalendarEvent(start=_dt(9, day=22), end=_dt(10, day=22), subject="d-1"),
            CalendarEvent(start=_dt(9, day=23), end=_dt(10, day=23), subject="d0"),
            CalendarEvent(start=_dt(9, day=24), end=_dt(10, day=24), subject="d1"),
        ]
        sync_calendar(self.db, self.config, provider=_FakeProvider(events), base=self.base)

        # 9/23（d0）の予定が0件になったケースを再取得
        events_without_d0 = [e for e in events if e.subject != "d0"]
        sync_calendar(
            self.db, self.config, provider=_FakeProvider(events_without_d0), base=self.base
        )

        remaining = repo.list_between(*self._wide_range())
        summaries = {a.summary for a in remaining}
        self.assertNotIn("d0", summaries)  # 0件になった日の古い PLANNED は消える
        self.assertIn("d-1", summaries)
        self.assertIn("d1", summaries)
        self.assertIn("old-out-of-window", summaries)  # 取得範囲外の日は残る


# ---------------------------------------------------------------------------
# calendar_sync: calendar_labels が再取得をまたいで保たれること
# ---------------------------------------------------------------------------


class CalendarLabelSurvivesResyncTest(unittest.TestCase):
    """uid を持つ .ics を IcsFileProvider 経由で取り込み、種別ラベルが
    calendar sync の再実行後も保たれることを確認する（既存の .ics 系テストの
    作り方に合わせ、実ファイルを一時ディレクトリへ書いて検証する）。
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmpdir.name)
        self.db = Database(tmp_path / "test.db")
        self.db.initialize()
        self.calendar_dir = tmp_path / "calendar"
        self.calendar_dir.mkdir()
        self.config = AppConfig(
            data={
                "calendar": {"fetch_days": 2, "fetch_days_back": 1},
                "paths": {"calendar_dir": str(self.calendar_dir)},
            }
        )
        self.base = date(2026, 9, 23)

        ics_text = (
            "BEGIN:VCALENDAR\n"
            "BEGIN:VEVENT\n"
            "UID:evt-123@example.com\n"
            "SUMMARY:Daily Standup\n"
            "DTSTART:20260923T090000Z\n"
            "DTEND:20260923T093000Z\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        (self.calendar_dir / "cal.ics").write_text(ics_text, encoding="utf-8")

    def tearDown(self) -> None:
        self.db.close()
        self._tmpdir.cleanup()

    def test_label_kept_after_second_sync(self) -> None:
        provider = IcsFileProvider(self.config)
        repo = ActivityRepository(self.db)

        sync_calendar(self.db, self.config, provider=provider, base=self.base)
        first = repo.list_between(_dt(0, day=1), _dt(0, day=30))
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].activity_type, ActivityType.MEETING)
        self.assertEqual(first[0].detail["uid"], "evt-123@example.com")  # uid が拾えている

        CalendarLabelRepository(self.db).set("evt-123@example.com", ActivityType.CODING)

        sync_calendar(self.db, self.config, provider=provider, base=self.base)
        second = repo.list_between(_dt(0, day=1), _dt(0, day=30))
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0].activity_type, ActivityType.CODING)  # 再取得しても保たれる


if __name__ == "__main__":
    unittest.main()
