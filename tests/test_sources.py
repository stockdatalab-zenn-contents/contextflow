"""sources 層（manual.py / calendar_ics.py / github_context.py）のテスト。

DB・ファイルは `tempfile.TemporaryDirectory` を使い、プロジェクト内は汚さない。
GitHubIssues のテストではネットワークアクセスを一切行わない
（urlopen を差し替え、呼ばれたら失敗させることで保証する）。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.conftest_path import add_source_path

add_source_path()

from contextflow.contracts.models import (  # noqa: E402
    ActivityLayer,
    ActivityType,
    Source,
    TaskStatus,
)
from contextflow.sources.calendar_ics import parse_ics  # noqa: E402
from contextflow.sources.github_context import ContextRepo, GitHubIssues  # noqa: E402
from contextflow.sources.manual import ManualInput  # noqa: E402
from contextflow.storage.db import Database  # noqa: E402
from contextflow.storage.repositories import ActivityRepository  # noqa: E402
from contextflow.timeutil import day_range  # noqa: E402

# 固定オフセットの JST。zoneinfo は Windows 環境で tzdata が無いと落ちるため使わない。
JST = timezone(timedelta(hours=9))


def _dt(hour: int, minute: int = 0, day: int = 23) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=JST)


# ---------------------------------------------------------------------------
# ManualInput
# ---------------------------------------------------------------------------


class ManualInputTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "test.db"
        self.db = Database(db_path)
        self.db.initialize()
        self.manual = ManualInput(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._tmpdir.cleanup()

    def test_start_running_stop_roundtrip(self) -> None:
        self.assertIsNone(self.manual.running())

        self.manual.start(ActivityType.CODING, project="p1", at=_dt(9, 0))
        running = self.manual.running()
        self.assertIsNotNone(running)
        self.assertEqual(running["activity_type"], ActivityType.CODING)
        self.assertEqual(running["project"], "p1")
        self.assertEqual(running["started_at"], _dt(9, 0))

        activities = self.manual.stop(at=_dt(10, 0), summary="done")
        # 日をまたがない通常ケースでは、今までどおり1件だけ保存される
        self.assertEqual(len(activities), 1)
        activity = activities[0]
        self.assertEqual(activity.layer, ActivityLayer.REPORTED)
        self.assertEqual(activity.source, Source.MANUAL)
        self.assertEqual(activity.start_at, _dt(9, 0))
        self.assertEqual(activity.end_at, _dt(10, 0))
        self.assertEqual(activity.summary, "done")
        # stop 後は running 状態が消えている
        self.assertIsNone(self.manual.running())

    def test_double_start_closes_previous_work(self) -> None:
        self.manual.start(ActivityType.CODING, project="p1", at=_dt(9, 0))
        self.manual.start(ActivityType.MEETING, project="p2", at=_dt(9, 30))

        running = self.manual.running()
        self.assertEqual(running["activity_type"], ActivityType.MEETING)
        self.assertEqual(running["project"], "p2")
        self.assertEqual(running["started_at"], _dt(9, 30))

        # 前の作業（CODING）は自動的に stop されて保存されている
        activities = ActivityRepository(self.db).list_between(
            _dt(0, 0), _dt(23, 59), layer=ActivityLayer.REPORTED
        )
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0].activity_type, ActivityType.CODING)
        self.assertEqual(activities[0].start_at, _dt(9, 0))
        self.assertEqual(activities[0].end_at, _dt(9, 30))

    def test_add_past_time_range(self) -> None:
        activities = self.manual.add(
            _dt(8, 0), _dt(8, 30), ActivityType.ADMIN, project="p1", summary="past work"
        )
        # 日をまたがない通常ケースでは、今までどおり1件だけ保存される
        self.assertEqual(len(activities), 1)
        activity = activities[0]
        self.assertEqual(activity.layer, ActivityLayer.REPORTED)
        self.assertEqual(activity.source, Source.MANUAL)
        self.assertEqual(activity.summary, "past work")

        got = ActivityRepository(self.db).list_between(_dt(0, 0), _dt(23, 59))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].start_at, _dt(8, 0))
        self.assertEqual(got[0].end_at, _dt(8, 30))

    def test_stop_when_not_running_returns_empty_list(self) -> None:
        self.assertEqual(self.manual.stop(), [])


# ---------------------------------------------------------------------------
# ManualInput: 日またぎ分割（stop / add）
# ---------------------------------------------------------------------------


class ManualInputDaySplitTest(unittest.TestCase):
    """9/22 23:50 開始 → 9/23 00:10 終了のような日またぎの手入力が、
    日境界（ローカル日付の 00:00）で複数 Activity に分割されることを確認する。

    ActivityRepository.list_between は start_at だけで絞り込むため、分割しないと
    終了日側の一覧・時間集計から丸ごと抜け落ちてしまう不具合の再発防止。
    日付は timeutil.day_range（実行環境のローカル tz）から組み立て、
    固定文字列では組み立てない。
    """

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "test.db"
        self.db = Database(db_path)
        self.db.initialize()
        self.manual = ManualInput(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._tmpdir.cleanup()

    def test_stop_splits_at_day_boundary(self) -> None:
        _, day23_start = day_range(date(2026, 9, 22))
        started = day23_start - timedelta(minutes=10)  # 9/22 23:50
        ended = day23_start + timedelta(minutes=10)  # 9/23 00:10

        self.manual.start(ActivityType.CODING, project="p1", at=started)
        activities = self.manual.stop(at=ended)

        self.assertEqual(len(activities), 2)
        first, second = activities
        self.assertEqual(first.start_at, started)
        self.assertEqual(first.end_at, day23_start)
        self.assertEqual(first.duration_min, 10)
        self.assertEqual(second.start_at, day23_start)
        self.assertEqual(second.end_at, ended)
        self.assertEqual(second.duration_min, 10)
        for activity in activities:
            self.assertEqual(activity.activity_type, ActivityType.CODING)
            self.assertEqual(activity.project, "p1")
            self.assertEqual(activity.layer, ActivityLayer.REPORTED)
            self.assertEqual(activity.source, Source.MANUAL)
            self.assertEqual(activity.confidence, 1.0)

    def test_stop_split_activities_appear_in_each_day_list_between(self) -> None:
        # 分割しないと 9/23 側が0件になってしまう不具合の再発防止
        _, day23_start = day_range(date(2026, 9, 22))
        _, day24_start = day_range(date(2026, 9, 23))
        started = day23_start - timedelta(minutes=10)
        ended = day23_start + timedelta(minutes=10)

        self.manual.start(ActivityType.CODING, at=started)
        self.manual.stop(at=ended)

        repo = ActivityRepository(self.db)
        day22_activities = repo.list_between(day23_start - timedelta(days=1), day23_start)
        day23_activities = repo.list_between(day23_start, day24_start)
        self.assertEqual(len(day22_activities), 1)
        self.assertEqual(len(day23_activities), 1)

    def test_stop_splits_across_multiple_days(self) -> None:
        # 2日以上またぐ場合は3件に分割され、中日は丸1日になる
        _, day23_start = day_range(date(2026, 9, 22))
        _, day24_start = day_range(date(2026, 9, 23))
        started = day23_start - timedelta(minutes=10)  # 9/22 23:50
        ended = day24_start + timedelta(minutes=10)  # 9/24 00:10

        self.manual.start(ActivityType.CODING, at=started)
        activities = self.manual.stop(at=ended)

        self.assertEqual(len(activities), 3)
        first, middle, last = activities
        self.assertEqual(first.start_at, started)
        self.assertEqual(first.end_at, day23_start)
        self.assertEqual(middle.start_at, day23_start)
        self.assertEqual(middle.end_at, day24_start)
        self.assertEqual(middle.duration_min, 24 * 60)
        self.assertEqual(last.start_at, day24_start)
        self.assertEqual(last.end_at, ended)

    def test_stop_below_min_duration_returns_empty_list(self) -> None:
        # MIN_DURATION_SEC 未満（ここでは0秒）で止めた場合は空リストで、
        # Activity は1件も増えない（従来の取り消し挙動）
        started = _dt(9, 0)
        self.manual.start(ActivityType.CODING, at=started)
        activities = self.manual.stop(at=started)

        self.assertEqual(activities, [])
        self.assertIsNone(self.manual.running())
        self.assertEqual(
            ActivityRepository(self.db).list_between(_dt(0, 0), _dt(23, 59)), []
        )

    def test_add_splits_at_day_boundary(self) -> None:
        _, day23_start = day_range(date(2026, 9, 22))
        started = day23_start - timedelta(minutes=10)
        ended = day23_start + timedelta(minutes=10)

        activities = self.manual.add(started, ended, ActivityType.MEETING, project="p1")

        self.assertEqual(len(activities), 2)
        self.assertEqual(activities[0].start_at, started)
        self.assertEqual(activities[0].end_at, day23_start)
        self.assertEqual(activities[1].start_at, day23_start)
        self.assertEqual(activities[1].end_at, ended)
        for activity in activities:
            self.assertEqual(activity.activity_type, ActivityType.MEETING)
            self.assertEqual(activity.project, "p1")


# ---------------------------------------------------------------------------
# parse_ics
# ---------------------------------------------------------------------------


class ParseIcsTest(unittest.TestCase):
    def test_tzid_event(self) -> None:
        text = (
            "BEGIN:VCALENDAR\n"
            "BEGIN:VEVENT\n"
            "SUMMARY:Meeting\n"
            "DTSTART;TZID=Asia/Tokyo:20260923T130000\n"
            "DTEND;TZID=Asia/Tokyo:20260923T140000\n"
            "END:VEVENT\n"
            "END:VCALENDAR\n"
        )
        events = parse_ics(text)
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["summary"], "Meeting")
        self.assertEqual(ev["start"].hour, 13)
        self.assertEqual(ev["end"].hour, 14)
        self.assertIsNotNone(ev["start"].tzinfo)

    def test_utc_z_is_converted_to_local(self) -> None:
        text = (
            "BEGIN:VEVENT\n"
            "SUMMARY:UTC Meeting\n"
            "DTSTART:20260923T040000Z\n"
            "DTEND:20260923T050000Z\n"
            "END:VEVENT\n"
        )
        events = parse_ics(text)
        self.assertEqual(len(events), 1)
        ev = events[0]
        # UTC 04:00 と同一時刻であること（tzinfo 自体はローカルに変換される）
        self.assertEqual(ev["start"], datetime(2026, 9, 23, 4, 0, tzinfo=timezone.utc))
        self.assertEqual(ev["end"], datetime(2026, 9, 23, 5, 0, tzinfo=timezone.utc))

    def test_all_day_event(self) -> None:
        text = (
            "BEGIN:VEVENT\n"
            "SUMMARY:Holiday\n"
            "DTSTART;VALUE=DATE:20260925\n"
            "END:VEVENT\n"
        )
        events = parse_ics(text)
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["start"].date(), date(2026, 9, 25))
        # DTEND が無い終日イベントは既定で+1日
        self.assertEqual(ev["end"] - ev["start"], timedelta(days=1))

    def test_line_continuation(self) -> None:
        text = (
            "BEGIN:VEVENT\n"
            "SUMMARY:Long Title Cont\n"
            " inued\n"
            "DTSTART:20260923T090000Z\n"
            "END:VEVENT\n"
        )
        events = parse_ics(text)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["summary"], "Long Title Continued")

    def test_escaped_characters(self) -> None:
        text = (
            "BEGIN:VEVENT\n"
            "SUMMARY:Foo\\, Bar\\nBaz\n"
            "DTSTART:20260923T090000Z\n"
            "END:VEVENT\n"
        )
        events = parse_ics(text)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["summary"], "Foo, Bar\nBaz")

    def test_no_vevent_returns_empty(self) -> None:
        self.assertEqual(parse_ics("BEGIN:VCALENDAR\nEND:VCALENDAR\n"), [])

    def test_uid_is_captured(self) -> None:
        text = (
            "BEGIN:VEVENT\n"
            "UID:evt-123@example.com\n"
            "SUMMARY:With UID\n"
            "DTSTART:20260923T090000Z\n"
            "END:VEVENT\n"
        )
        events = parse_ics(text)
        self.assertEqual(events[0]["uid"], "evt-123@example.com")

    def test_missing_uid_defaults_to_empty_string(self) -> None:
        text = "BEGIN:VEVENT\nSUMMARY:No UID\nDTSTART:20260923T090000Z\nEND:VEVENT\n"
        events = parse_ics(text)
        self.assertEqual(events[0]["uid"], "")


# ---------------------------------------------------------------------------
# ContextRepo
# ---------------------------------------------------------------------------


class ContextRepoTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.repo = ContextRepo(Path(self._tmpdir.name) / "context_repo")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_ensure_layout_is_idempotent(self) -> None:
        self.repo.ensure_layout()
        constraints_path = self.repo.root / "context" / "constraints.md"
        # 雛形が既にある状態を作り、内容を書き換えておく
        constraints_path.write_text("# Constraints\n\n- 独自の制約\n", encoding="utf-8")

        self.repo.ensure_layout()  # 2回目実行しても壊れない・上書きしない

        self.assertIn("独自の制約", constraints_path.read_text(encoding="utf-8"))
        self.assertTrue((self.repo.root / "projects").is_dir())
        self.assertTrue((self.repo.root / "context" / "decisions").is_dir())
        self.assertTrue((self.repo.root / "context" / "changes").is_dir())
        self.assertTrue((self.repo.root / "activity" / "daily").is_dir())
        self.assertTrue((self.repo.root / "activity" / "weekly").is_dir())
        self.assertTrue((self.repo.root / "agent" / "schemas").is_dir())

    def test_append_month_does_not_duplicate_same_line(self) -> None:
        target = date(2026, 9, 23)
        self.repo.append_month("changes", target, "- 何かを変更した")
        self.repo.append_month("changes", target, "- 何かを変更した")  # 同じ行は重複させない

        path = self.repo.root / "context" / "changes" / "2026-09.md"
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count("- 何かを変更した"), 1)
        self.assertEqual(text.count("## 2026-09-23"), 1)

        self.repo.append_month("changes", target, "- 別の変更")
        text = path.read_text(encoding="utf-8")
        self.assertIn("- 別の変更", text)
        self.assertEqual(text.count("## 2026-09-23"), 1)  # 見出しは増えない

    def test_append_month_invalid_kind_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.repo.append_month("invalid", date(2026, 9, 23), "- x")

    def test_read_tasks_parses_checkbox_variants(self) -> None:
        project_dir = self.repo.root / "projects" / "demo"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "tasks.md").write_text(
            "- [ ] 未完了タスク\n"
            "- [x] 完了タスク\n"
            "- [ ] !ブロック中タスク\n"
            "- [ ] Issue付きタスク (#12)\n",
            encoding="utf-8",
        )

        tasks = self.repo.read_tasks()
        self.assertEqual(len(tasks), 4)
        by_title = {t.title: t for t in tasks}

        self.assertEqual(by_title["未完了タスク"].status, TaskStatus.OPEN)
        self.assertFalse(by_title["未完了タスク"].blocked)

        self.assertEqual(by_title["完了タスク"].status, TaskStatus.DONE)

        self.assertEqual(by_title["ブロック中タスク"].status, TaskStatus.BLOCKED)
        self.assertTrue(by_title["ブロック中タスク"].blocked)

        self.assertEqual(by_title["Issue付きタスク"].github_issue, 12)

        for task in tasks:
            self.assertEqual(task.project, "demo")

    def test_read_constraints_and_project_state_when_absent(self) -> None:
        # ensure_layout していない状態でも例外にならず空を返す
        self.assertEqual(self.repo.read_constraints(), [])
        self.assertEqual(self.repo.read_project_state("no-such-project"), "")


# ---------------------------------------------------------------------------
# GitHubIssues
# ---------------------------------------------------------------------------


class GitHubIssuesTest(unittest.TestCase):
    def test_disabled_without_token_returns_empty_without_network(self) -> None:
        client = GitHubIssues(repo="owner/repo", token=None)
        self.assertFalse(client.enabled)

        # urlopen が呼ばれたら即失敗させ、通信していないことを保証する
        with mock.patch(
            "urllib.request.urlopen", side_effect=AssertionError("network access attempted")
        ):
            tasks = client.list_open()
        self.assertEqual(tasks, [])

    def test_disabled_without_repo(self) -> None:
        client = GitHubIssues(repo="", token="dummy-token")
        self.assertFalse(client.enabled)
        with mock.patch(
            "urllib.request.urlopen", side_effect=AssertionError("network access attempted")
        ):
            self.assertEqual(client.list_open(), [])

    def test_sync_to_db_when_disabled_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = Database(Path(tmpdir) / "test.db")
            db.initialize()
            try:
                client = GitHubIssues(repo="", token=None)
                with mock.patch(
                    "urllib.request.urlopen",
                    side_effect=AssertionError("network access attempted"),
                ):
                    count = client.sync_to_db(db)
                self.assertEqual(count, 0)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
