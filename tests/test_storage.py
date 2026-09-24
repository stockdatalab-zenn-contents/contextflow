"""storage 層（db.py / repositories.py）のテスト。

DB は毎回 `tempfile.TemporaryDirectory` の中に作る。
プロジェクト内（特に app/data）は一切汚さない。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.conftest_path import add_source_path

add_source_path()

from contextflow.contracts.decision import (  # noqa: E402
    Answer,
    DecisionRequest,
    DecisionResponse,
    Question,
    QuestionType,
)
from contextflow.contracts.models import (  # noqa: E402
    Activity,
    ActivityLayer,
    ActivityType,
    Change,
    Decision,
    RawEvent,
    Session,
    Source,
    Task,
    TaskStatus,
)
from contextflow.storage.db import Database  # noqa: E402
from contextflow.storage.repositories import (  # noqa: E402
    ActivityRepository,
    ChangeRepository,
    DecisionLogRepository,
    DecisionRepository,
    MetaRepository,
    RawEventRepository,
    SessionRepository,
    TaskRepository,
)

# 固定オフセットの JST。zoneinfo は Windows 環境で tzdata が無いと落ちるため使わない。
JST = timezone(timedelta(hours=9))


def _dt(hour: int, minute: int = 0, day: int = 23) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=JST)


class _TempDbTestCase(unittest.TestCase):
    """一時ディレクトリに DB を作り、テスト後に破棄する共通基底。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "test.db"
        self.db = Database(db_path)
        self.db.initialize()

    def tearDown(self) -> None:
        self.db.close()
        self._tmpdir.cleanup()


class DatabaseTest(_TempDbTestCase):
    """Database.initialize の冪等性。"""

    def test_initialize_is_idempotent(self) -> None:
        # 2回実行しても例外にならず、テーブルは残ったまま
        self.db.initialize()
        conn = self.db.connect()
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='activities'"
        )
        self.assertIsNotNone(cur.fetchone())


class RawEventRepositoryTest(_TempDbTestCase):
    def test_add_and_list_between_preserve_tz(self) -> None:
        repo = RawEventRepository(self.db)
        event = RawEvent(
            ts=_dt(10, 0), process="chrome.exe", window_title="foo", idle_sec=5, host="pc1"
        )
        new_id = repo.add(event)
        self.assertIsInstance(new_id, int)

        events = repo.list_between(_dt(0, 0), _dt(23, 59))
        self.assertEqual(len(events), 1)
        got = events[0]
        self.assertEqual(got.process, "chrome.exe")
        self.assertIsNotNone(got.ts.tzinfo)
        self.assertEqual(got.ts, _dt(10, 0))

    def test_add_many_and_last(self) -> None:
        repo = RawEventRepository(self.db)
        events = [
            RawEvent(ts=_dt(9, 0), process="a.exe", window_title="A"),
            RawEvent(ts=_dt(9, 1), process="b.exe", window_title="B"),
        ]
        count = repo.add_many(events)
        self.assertEqual(count, 2)
        last = repo.last()
        self.assertIsNotNone(last)
        self.assertEqual(last.process, "b.exe")


class SessionRepositoryTest(_TempDbTestCase):
    def test_replace_between(self) -> None:
        repo = SessionRepository(self.db)
        s1 = Session(
            start_at=_dt(9, 0), end_at=_dt(9, 10), process="a.exe",
            window_title="A", duration_sec=600,
        )
        repo.replace_between(_dt(0, 0), _dt(23, 59), [s1])
        sessions = repo.list_between(_dt(0, 0), _dt(23, 59))
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].process, "a.exe")
        self.assertIsNotNone(sessions[0].start_at.tzinfo)

        # 同じ範囲を置き換えると古い session は消え、新しい方だけが残る
        s2 = Session(
            start_at=_dt(10, 0), end_at=_dt(10, 5), process="b.exe",
            window_title="B", duration_sec=300,
        )
        repo.replace_between(_dt(0, 0), _dt(23, 59), [s2])
        sessions = repo.list_between(_dt(0, 0), _dt(23, 59))
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].process, "b.exe")


class ActivityRepositoryTest(_TempDbTestCase):
    def test_add_and_roundtrip_enum_and_tz(self) -> None:
        repo = ActivityRepository(self.db)
        activity = Activity(
            start_at=_dt(9, 0),
            end_at=_dt(10, 0),
            activity_type=ActivityType.CODING,
            layer=ActivityLayer.OBSERVED,
            source=Source.WINDOWS,
            project="proj-a",
            summary="test",
            detail={"foo": "bar"},
        )
        new_id = repo.add(activity)

        got = repo.current(_dt(9, 30))
        self.assertIsNotNone(got)
        self.assertEqual(got.id, new_id)
        self.assertEqual(got.activity_type, ActivityType.CODING)
        self.assertEqual(got.layer, ActivityLayer.OBSERVED)
        self.assertEqual(got.source, Source.WINDOWS)
        self.assertIsNotNone(got.start_at.tzinfo)
        self.assertIsNotNone(got.end_at.tzinfo)
        self.assertEqual(got.detail, {"foo": "bar"})

    def test_replace_between_respects_layer(self) -> None:
        """layer を指定した replace_between は他層の Activity を消さないこと。"""
        repo = ActivityRepository(self.db)
        observed = Activity(
            start_at=_dt(9, 0), end_at=_dt(10, 0),
            activity_type=ActivityType.CODING,
            layer=ActivityLayer.OBSERVED, source=Source.WINDOWS,
        )
        planned = Activity(
            start_at=_dt(9, 0), end_at=_dt(10, 0),
            activity_type=ActivityType.MEETING,
            layer=ActivityLayer.PLANNED, source=Source.CALENDAR,
        )
        repo.add(observed)
        repo.add(planned)

        new_observed = Activity(
            start_at=_dt(11, 0), end_at=_dt(12, 0),
            activity_type=ActivityType.RESEARCH,
            layer=ActivityLayer.OBSERVED, source=Source.WINDOWS,
        )
        repo.replace_between(
            _dt(0, 0), _dt(23, 59), [new_observed], layer=ActivityLayer.OBSERVED
        )

        remaining_observed = repo.list_between(
            _dt(0, 0), _dt(23, 59), layer=ActivityLayer.OBSERVED
        )
        remaining_planned = repo.list_between(
            _dt(0, 0), _dt(23, 59), layer=ActivityLayer.PLANNED
        )
        self.assertEqual(len(remaining_observed), 1)
        self.assertEqual(remaining_observed[0].activity_type, ActivityType.RESEARCH)
        # PLANNED 層は replace_between(layer=OBSERVED) の影響を受けず残っている
        self.assertEqual(len(remaining_planned), 1)
        self.assertEqual(remaining_planned[0].activity_type, ActivityType.MEETING)

    def test_list_between_filters_by_layer(self) -> None:
        repo = ActivityRepository(self.db)
        repo.add(Activity(start_at=_dt(9, 0), end_at=_dt(10, 0), layer=ActivityLayer.OBSERVED))
        repo.add(Activity(start_at=_dt(10, 0), end_at=_dt(11, 0), layer=ActivityLayer.REPORTED))

        only_reported = repo.list_between(_dt(0, 0), _dt(23, 59), layer=ActivityLayer.REPORTED)
        self.assertEqual(len(only_reported), 1)
        self.assertEqual(only_reported[0].layer, ActivityLayer.REPORTED)

        all_activities = repo.list_between(_dt(0, 0), _dt(23, 59))
        self.assertEqual(len(all_activities), 2)


class ChangeRepositoryTest(_TempDbTestCase):
    def test_add_and_read(self) -> None:
        repo = ChangeRepository(self.db)
        repo.add(Change(ts=_dt(9, 0), description="c1", project="p", source=Source.MANUAL))
        repo.add(Change(ts=_dt(10, 0), description="c2", project="p", source=Source.GITHUB))

        recent = repo.recent(limit=10)
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[0].description, "c2")  # 新しい順
        self.assertEqual(recent[0].source, Source.GITHUB)
        self.assertIsNotNone(recent[0].ts.tzinfo)

        between = repo.list_between(_dt(0, 0), _dt(23, 59))
        self.assertEqual(len(between), 2)
        self.assertEqual(between[0].description, "c1")  # 昇順


class DecisionRepositoryTest(_TempDbTestCase):
    def test_add_and_read(self) -> None:
        repo = DecisionRepository(self.db)
        repo.add(Decision(ts=_dt(9, 0), decision="d1", reason="r1", source=Source.MANUAL))
        repo.add(Decision(ts=_dt(10, 0), decision="d2", reason="r2", source=Source.MANUAL))

        recent = repo.recent(limit=10)
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[0].decision, "d2")
        self.assertIsNotNone(recent[0].ts.tzinfo)

        between = repo.list_between(_dt(0, 0), _dt(23, 59))
        self.assertEqual(len(between), 2)


class TaskRepositoryTest(_TempDbTestCase):
    def test_upsert_updates_by_title_and_project(self) -> None:
        repo = TaskRepository(self.db)
        first_id = repo.upsert(
            Task(title="t1", project="p1", status=TaskStatus.OPEN, priority=3)
        )
        second_id = repo.upsert(
            Task(title="t1", project="p1", status=TaskStatus.IN_PROGRESS, priority=1)
        )
        # (title, project) が同じなら新規行にならず上書き更新される
        self.assertEqual(first_id, second_id)

        got = repo.get(first_id)
        self.assertEqual(got.status, TaskStatus.IN_PROGRESS)
        self.assertEqual(got.priority, 1)

        all_tasks = repo.list()
        self.assertEqual(len(all_tasks), 1)

    def test_upsert_different_project_creates_new_row(self) -> None:
        repo = TaskRepository(self.db)
        id_a = repo.upsert(Task(title="t1", project="p1"))
        id_b = repo.upsert(Task(title="t1", project="p2"))
        self.assertNotEqual(id_a, id_b)
        self.assertEqual(len(repo.list()), 2)

    def test_upsert_with_none_project_does_not_duplicate(self) -> None:
        # SQLite の UNIQUE は NULL を別値として扱うため、project=None で行が増えないことを確認
        repo = TaskRepository(self.db)
        id_a = repo.upsert(Task(title="t1", project=None))
        id_b = repo.upsert(Task(title="t1", project=None, status=TaskStatus.DONE))
        self.assertEqual(id_a, id_b)
        self.assertEqual(len(repo.list()), 1)
        self.assertEqual(repo.list()[0].status, TaskStatus.DONE)

    def test_counts(self) -> None:
        repo = TaskRepository(self.db)
        repo.upsert(Task(title="a", project="p1", status=TaskStatus.OPEN))
        repo.upsert(Task(title="b", project="p1", status=TaskStatus.OPEN))
        repo.upsert(Task(title="c", project="p1", status=TaskStatus.BLOCKED))

        counts = repo.counts()
        self.assertEqual(counts.get("open"), 2)
        self.assertEqual(counts.get("blocked"), 1)

    def test_find_by_title_with_none_project(self) -> None:
        repo = TaskRepository(self.db)
        repo.upsert(Task(title="x", project=None))
        found = repo.find_by_title("x", None)
        self.assertIsNotNone(found)
        self.assertEqual(found.title, "x")
        self.assertIsNone(repo.find_by_title("not-exist"))


class MetaRepositoryTest(_TempDbTestCase):
    def test_get_set_delete(self) -> None:
        repo = MetaRepository(self.db)
        self.assertIsNone(repo.get("k1"))

        repo.set("k1", "v1")
        self.assertEqual(repo.get("k1"), "v1")

        repo.set("k1", "v2")  # 上書き
        self.assertEqual(repo.get("k1"), "v2")

        repo.delete("k1")
        self.assertIsNone(repo.get("k1"))

        # 無いキーを delete してもエラーにならない
        repo.delete("not-exist")


class DecisionLogRepositoryTest(_TempDbTestCase):
    def test_add_response_and_recent_roundtrip(self) -> None:
        repo = DecisionLogRepository(self.db)
        question = Question(key="q1", type=QuestionType.NOUL, instruction="test")
        request = DecisionRequest(state={"foo": "bar"}, questions=[question])
        answer = Answer(
            key="q1", value=True, raw_confidence=0.8, confidence=0.75, engine="rule_based"
        )
        response = DecisionResponse(answers={"q1": answer}, engine="rule_based", latency_ms=10)

        ids = repo.add_response(request, response)
        self.assertEqual(len(ids), 1)

        recent = repo.recent(engine="rule_based", question_key="q1")
        self.assertEqual(len(recent), 1)
        row = recent[0]
        self.assertEqual(row["value"], "true")
        self.assertAlmostEqual(row["raw_confidence"], 0.8)
        self.assertAlmostEqual(row["confidence"], 0.75)
        self.assertEqual(row["question_key"], "q1")

        # 存在しない question_key では取れない
        self.assertEqual(repo.recent(question_key="not-exist"), [])


if __name__ == "__main__":
    unittest.main()
