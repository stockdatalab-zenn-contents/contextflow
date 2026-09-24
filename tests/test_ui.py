"""tests/test_ui.py

ローカル GUI（`app/source/contextflow/ui/`）のテスト。
`docs/20260923_ui_design.md` §4（API契約）・§5（安全側の設計）を正として検証する。

標準ライブラリの unittest のみを使用する（pytest は使わない）。
DB は毎回 `tempfile.TemporaryDirectory` の中に作る。`AppConfig` は
`load_config()` で実ファイル（config.toml）を読んだ上で `data["paths"]` だけを
一時ディレクトリへ差し替える。実設定ファイル・app/data は絶対に触らない。

外部ネットワークへは一切アクセスしない（127.0.0.1 のみ）。
"""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from tests.conftest_path import add_source_path

add_source_path()

from contextflow import timeutil  # noqa: E402
from contextflow.collector.collector import Collector, HEARTBEAT_KEY  # noqa: E402
from contextflow.sources.manual import MIN_DURATION_SEC  # noqa: E402
from contextflow.config import AppConfig, load_config  # noqa: E402
from contextflow.contracts.models import (  # noqa: E402
    Activity,
    ActivityLayer,
    ActivityType,
    RawEvent,
    Source,
)
from contextflow.contracts.serde import to_jsonable  # noqa: E402
from contextflow.storage.db import open_database  # noqa: E402
from contextflow.storage.repositories import (  # noqa: E402
    ActivityRepository,
    CalendarLabelRepository,
    MetaRepository,
    RawEventRepository,
)
from contextflow.ui import api  # noqa: E402
from contextflow.ui.server import create_server  # noqa: E402

# server を使うテストで共有する固定ポート（他のテストと衝突しにくい値を選ぶ）
_SERVER_PORT = 0  # 0 = OS に空きポートを割り当てさせる（固定ポートの衝突を避ける）
_bound_port = 0   # setUpClass で実際に割り当てられた番号を入れる


# ---------------------------------------------------------------------------
# 共通ヘルパ
# ---------------------------------------------------------------------------


def _temp_config(tmp_dir: Path) -> AppConfig:
    """実 config.toml を読み込んだ AppConfig の paths だけ一時ディレクトリへ差し替える。

    `config.data` を丸ごと入れ替えるわけではなく paths キーだけ差し替えるため、
    categories.toml など他の設定は実ファイルのものがそのまま使われる（読み取りのみ）。
    """
    config = load_config()
    config.data = dict(config.data)
    config.data["paths"] = {
        "database": str(tmp_dir / "contextflow.db"),
        "state_json": str(tmp_dir / "state.json"),
        "export_dir": str(tmp_dir / "export"),
        "context_repo": str(tmp_dir / "context_repo"),
        "calendar_dir": str(tmp_dir / "calendar"),
    }
    return config


def _seed_activity(config: AppConfig, activity: Activity) -> int:
    """API を経由せず activities テーブルへ直接1件書き込む（layer=observed 等を作るため）。"""
    db = open_database(config)
    try:
        return ActivityRepository(db).add(activity)
    finally:
        db.close()


def _set_heartbeat(config: AppConfig, value: str) -> None:
    """meta の `collector_heartbeat` を直接書き換える。"""
    db = open_database(config)
    try:
        MetaRepository(db).set(HEARTBEAT_KEY, value)
    finally:
        db.close()


def _add_raw_event(config: AppConfig, ts, process: str = "Code.exe") -> None:
    """API を経由せず raw_events テーブルへ直接1件書き込む。"""
    db = open_database(config)
    try:
        RawEventRepository(db).add(
            RawEvent(ts=ts, process=process, window_title="t", idle_sec=0)
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# api.handle（サーバを立てずに直接呼ぶ）
# ---------------------------------------------------------------------------


class ApiTestCase(unittest.TestCase):
    """`api.handle` を直接呼ぶテストの共通基盤。テストごとに新しい一時 DB を使う。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.config = _temp_config(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def call(self, method: str, path: str, query: dict | None = None, body: dict | None = None):
        return api.handle(method, path, query or {}, body or {}, self.config)


class DayEndpointTests(ApiTestCase):
    """GET /api/day の応答形状と日付指定。"""

    def test_default_today_has_all_contract_keys(self) -> None:
        status, payload = self.call("GET", "/api/day")
        self.assertEqual(status, 200)
        for key in (
            "date", "running", "sessions", "activities", "gaps",
            "changes", "decisions", "activity_types",
        ):
            self.assertIn(key, payload)
        self.assertEqual(payload["date"], timeutil.today().isoformat())
        self.assertIsNone(payload["running"])
        self.assertIn("coding", payload["activity_types"])

    def test_past_date_query_returns_that_day_data(self) -> None:
        past = (timeutil.today() - timedelta(days=3)).isoformat()
        status, added = self.call(
            "POST", "/api/work/add",
            body={
                "date": past, "start": "09:00", "end": "10:00",
                "activity_type": "coding", "project": "p1", "summary": "s1",
            },
        )
        self.assertEqual(status, 200)
        activity_id = added["activity"]["id"]

        status, payload = self.call("GET", "/api/day", {"date": past})
        self.assertEqual(status, 200)
        self.assertEqual(payload["date"], past)
        ids = [a["id"] for a in payload["activities"]]
        self.assertIn(activity_id, ids)


class WorkFlowTests(ApiTestCase):
    """work/start -> day.running -> work/stop の一連の流れ。"""

    def test_start_fills_running_then_stop_creates_activity(self) -> None:
        status, resp = self.call(
            "POST", "/api/work/start",
            body={"activity_type": "meeting", "project": "sample_project", "task": "要件整理"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(resp["ok"])

        status, day = self.call("GET", "/api/day")
        self.assertEqual(status, 200)
        self.assertIsNotNone(day["running"])
        self.assertEqual(day["running"]["activity_type"], "meeting")
        self.assertEqual(day["running"]["project"], "sample_project")

        # MIN_DURATION_SEC 未満で止めると押し間違い扱いになるため、少しだけ待つ
        time.sleep(MIN_DURATION_SEC + 0.1)
        status, stopped = self.call("POST", "/api/work/stop", body={"summary": "完了"})
        self.assertEqual(status, 200)
        self.assertIsNotNone(stopped["activity"])
        self.assertEqual(stopped["activity"]["layer"], "reported")
        self.assertTrue(stopped["activity"]["editable"])

        status, day2 = self.call("GET", "/api/day")
        self.assertIsNone(day2["running"])
        ids = [a["id"] for a in day2["activities"]]
        self.assertIn(stopped["activity"]["id"], ids)

    def test_immediate_stop_cancels_without_creating_activity(self) -> None:
        """開始してすぐ止めた場合は押し間違いとみなし、長さ0分の活動を残さない。"""
        status, _ = self.call(
            "POST", "/api/work/start", body={"activity_type": "meeting"}
        )
        self.assertEqual(status, 200)

        status, stopped = self.call("POST", "/api/work/stop")
        self.assertEqual(status, 200)
        self.assertIsNone(stopped["activity"])
        self.assertIn("取り消", stopped["message"])

        status, day = self.call("GET", "/api/day")
        self.assertIsNone(day["running"])
        self.assertEqual(day["activities"], [])

    def test_stop_without_running_returns_null_activity(self) -> None:
        status, resp = self.call("POST", "/api/work/stop")
        self.assertEqual(status, 200)
        self.assertIsNone(resp["activity"])


class ActivityCrudTests(ApiTestCase):
    """work/add（過去日）-> PUT -> DELETE の往復。"""

    def test_add_put_delete_roundtrip(self) -> None:
        past = (timeutil.today() - timedelta(days=2)).isoformat()
        status, added = self.call(
            "POST", "/api/work/add",
            body={
                "date": past, "start": "09:00", "end": "10:00",
                "activity_type": "coding", "project": "p1", "task": "t1", "summary": "s1",
            },
        )
        self.assertEqual(status, 200)
        activity_id = added["activity"]["id"]
        self.assertTrue(added["activity"]["editable"])

        status, updated = self.call(
            "PUT", f"/api/activity/{activity_id}",
            body={
                "start": "09:30", "end": "10:30", "activity_type": "meeting",
                "project": "p2", "task": "t2", "summary": "s2",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["activity"]["activity_type"], "meeting")
        self.assertEqual(updated["activity"]["project"], "p2")

        status, deleted = self.call("DELETE", f"/api/activity/{activity_id}")
        self.assertEqual(status, 200)
        self.assertEqual(deleted["id"], activity_id)

        status, day = self.call("GET", "/api/day", {"date": past})
        ids = [a["id"] for a in day["activities"]]
        self.assertNotIn(activity_id, ids)


class CalendarLabelTests(ApiTestCase):
    """GET /api/calendar/events（uid でのまとめ）と PUT /api/calendar/label（種別付与）。"""

    def _seed_planned(
        self,
        uid: str,
        *,
        day_offset: int = 0,
        hour: int = 9,
        activity_type: ActivityType = ActivityType.MEETING,
        summary: str = "予定",
    ) -> int:
        """calendar sync を経由せず、予定（layer=planned）を直接1件仕込む。"""
        base = timeutil.today() + timedelta(days=day_offset)
        activity = Activity(
            start_at=timeutil.parse_hhmm(f"{hour:02d}:00", base),
            end_at=timeutil.parse_hhmm(f"{hour:02d}:30", base),
            activity_type=activity_type,
            layer=ActivityLayer.PLANNED,
            source=Source.CALENDAR,
            summary=summary,
            detail={"uid": uid},
        )
        return _seed_activity(self.config, activity)

    def _set_label(self, uid: str, activity_type: ActivityType) -> None:
        db = open_database(self.config)
        try:
            CalendarLabelRepository(db).set(uid, activity_type)
        finally:
            db.close()

    def test_events_are_grouped_by_uid_with_count_and_labeled(self) -> None:
        self._seed_planned("uid-a", day_offset=0, hour=9)
        self._seed_planned("uid-a", day_offset=1, hour=9)  # 同じ uid が複数行（定期予定を想定）
        self._seed_planned("uid-b", day_offset=0, hour=11)
        self._seed_planned("", day_offset=0, hour=13)  # uid の無い予定
        self._set_label("uid-b", ActivityType.CODING)

        status, resp = self.call("GET", "/api/calendar/events", {"days": 7})

        self.assertEqual(status, 200)
        self.assertEqual(resp["no_uid"], 1)
        self.assertEqual(resp["unlabeled"], 1)
        by_uid = {e["uid"]: e for e in resp["events"]}
        self.assertEqual(by_uid["uid-a"]["count"], 2)
        self.assertFalse(by_uid["uid-a"]["labeled"])
        self.assertEqual(by_uid["uid-b"]["count"], 1)
        self.assertTrue(by_uid["uid-b"]["labeled"])
        # labeled=false が先に並ぶ
        self.assertEqual(resp["events"][0]["uid"], "uid-a")

    def test_put_label_updates_existing_planned_rows_and_get_reflects_it(self) -> None:
        self._seed_planned("uid-c", day_offset=0, hour=9)
        self._seed_planned("uid-c", day_offset=1, hour=9)

        status, resp = self.call(
            "PUT", "/api/calendar/label", body={"uid": "uid-c", "activity_type": "coding"}
        )

        self.assertEqual(status, 200)
        self.assertEqual(resp["ok"], True)
        self.assertEqual(resp["uid"], "uid-c")
        self.assertEqual(resp["activity_type"], "coding")
        self.assertEqual(resp["updated"], 2)  # 既存 planned 行数と一致

        status, events = self.call("GET", "/api/calendar/events", {"days": 7})
        by_uid = {e["uid"]: e for e in events["events"]}
        self.assertTrue(by_uid["uid-c"]["labeled"])
        self.assertEqual(by_uid["uid-c"]["activity_type"], "coding")

    def test_invalid_activity_type_is_400(self) -> None:
        status, resp = self.call(
            "PUT", "/api/calendar/label", body={"uid": "uid-x", "activity_type": "no_such_type"}
        )
        self.assertEqual(status, 400)
        self.assertIn("error", resp)

    def test_empty_uid_is_400(self) -> None:
        status, resp = self.call(
            "PUT", "/api/calendar/label", body={"uid": "", "activity_type": "coding"}
        )
        self.assertEqual(status, 400)
        self.assertIn("error", resp)

    def test_null_activity_type_clears_label_and_resets_planned_rows_to_meeting(self) -> None:
        self._seed_planned("uid-d", day_offset=0, hour=9, activity_type=ActivityType.CODING)
        self._set_label("uid-d", ActivityType.CODING)

        status, resp = self.call(
            "PUT", "/api/calendar/label", body={"uid": "uid-d", "activity_type": None}
        )

        self.assertEqual(status, 200)
        self.assertIsNone(resp["activity_type"])
        self.assertEqual(resp["updated"], 1)

        status, events = self.call("GET", "/api/calendar/events", {"days": 7})
        by_uid = {e["uid"]: e for e in events["events"]}
        self.assertFalse(by_uid["uid-d"]["labeled"])
        self.assertEqual(by_uid["uid-d"]["activity_type"], "meeting")


class ObservedActivityImmutableTests(ApiTestCase):
    """PCログ由来（layer=observed）の活動は PUT / DELETE で 409（日本語メッセージ）になること。"""

    def setUp(self) -> None:
        super().setUp()
        today = timeutil.today()
        activity = Activity(
            start_at=timeutil.parse_hhmm("09:00", today),
            end_at=timeutil.parse_hhmm("10:00", today),
            activity_type=ActivityType.CODING,
            layer=ActivityLayer.OBSERVED,
            source=Source.WINDOWS,
            summary="Code.exe",
        )
        self.activity_id = _seed_activity(self.config, activity)

    def test_put_observed_activity_is_409_in_japanese(self) -> None:
        status, resp = self.call(
            "PUT", f"/api/activity/{self.activity_id}",
            body={"start": "09:00", "end": "10:30", "activity_type": "coding"},
        )
        self.assertEqual(status, 409)
        self.assertIn("編集できない", resp["error"])

    def test_delete_observed_activity_is_409_in_japanese(self) -> None:
        status, resp = self.call("DELETE", f"/api/activity/{self.activity_id}")
        self.assertEqual(status, 409)
        self.assertIn("編集できない", resp["error"])


class EditableFlagTests(ApiTestCase):
    """`editable` は layer=reported のものだけ true になること。"""

    def test_reported_activity_is_editable(self) -> None:
        status, resp = self.call(
            "POST", "/api/work/add",
            body={"start": "09:00", "end": "10:00", "activity_type": "coding"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(resp["activity"]["editable"])

    def test_observed_activity_is_not_editable(self) -> None:
        today = timeutil.today()
        activity = Activity(
            start_at=timeutil.parse_hhmm("09:00", today),
            end_at=timeutil.parse_hhmm("10:00", today),
            activity_type=ActivityType.CODING,
            layer=ActivityLayer.OBSERVED,
            source=Source.WINDOWS,
        )
        _seed_activity(self.config, activity)
        status, day = self.call("GET", "/api/day")
        self.assertEqual(status, 200)
        self.assertEqual(len(day["activities"]), 1)
        self.assertFalse(day["activities"][0]["editable"])

    def test_planned_activity_is_not_editable(self) -> None:
        today = timeutil.today()
        activity = Activity(
            start_at=timeutil.parse_hhmm("09:00", today),
            end_at=timeutil.parse_hhmm("10:00", today),
            activity_type=ActivityType.MEETING,
            layer=ActivityLayer.PLANNED,
            source=Source.CALENDAR,
        )
        _seed_activity(self.config, activity)
        status, day = self.call("GET", "/api/day")
        self.assertEqual(status, 200)
        self.assertEqual(len(day["activities"]), 1)
        self.assertFalse(day["activities"][0]["editable"])


class ChangeCrudTests(ApiTestCase):
    """POST /api/change -> PUT -> DELETE の往復。"""

    def test_add_put_delete_roundtrip(self) -> None:
        status, added = self.call(
            "POST", "/api/change", body={"description": "先方のAPI利用が不可と判明", "project": "p1"}
        )
        self.assertEqual(status, 200)
        change_id = added["change"]["id"]

        status, updated = self.call(
            "PUT", f"/api/change/{change_id}", body={"description": "CSV連携へ変更", "project": "p2"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["change"]["description"], "CSV連携へ変更")
        self.assertEqual(updated["change"]["project"], "p2")

        status, deleted = self.call("DELETE", f"/api/change/{change_id}")
        self.assertEqual(status, 200)
        self.assertEqual(deleted["id"], change_id)

        status, day = self.call("GET", "/api/day")
        ids = [c["id"] for c in day["changes"]]
        self.assertNotIn(change_id, ids)


class DecisionCrudTests(ApiTestCase):
    """POST /api/decision -> PUT -> DELETE の往復。"""

    def test_add_put_delete_roundtrip(self) -> None:
        status, added = self.call(
            "POST", "/api/decision",
            body={"decision": "CSV連携方式に変更", "reason": "API利用不可のため", "project": "p1"},
        )
        self.assertEqual(status, 200)
        decision_id = added["decision"]["id"]

        status, updated = self.call(
            "PUT", f"/api/decision/{decision_id}",
            body={"decision": "手動連携に変更", "reason": "CSVも不可のため", "project": "p2"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["decision"]["decision"], "手動連携に変更")

        status, deleted = self.call("DELETE", f"/api/decision/{decision_id}")
        self.assertEqual(status, 200)
        self.assertEqual(deleted["id"], decision_id)

        status, day = self.call("GET", "/api/day")
        ids = [d["id"] for d in day["decisions"]]
        self.assertNotIn(decision_id, ids)


class ValidationTests(ApiTestCase):
    """入力検証: 不正な activity_type / 開始>=終了 / 必須項目の空。"""

    def test_invalid_activity_type_is_400(self) -> None:
        status, resp = self.call(
            "POST", "/api/work/start", body={"activity_type": "no_such_type"}
        )
        self.assertEqual(status, 400)
        self.assertIn("error", resp)

    def test_start_after_end_is_400(self) -> None:
        status, resp = self.call(
            "POST", "/api/work/add",
            body={"start": "10:00", "end": "09:00", "activity_type": "coding"},
        )
        self.assertEqual(status, 400)
        self.assertIn("error", resp)

    def test_empty_required_field_is_400(self) -> None:
        status, resp = self.call("POST", "/api/change", body={"description": ""})
        self.assertEqual(status, 400)
        self.assertIn("error", resp)

        status, resp = self.call("POST", "/api/decision", body={"decision": ""})
        self.assertEqual(status, 400)
        self.assertIn("error", resp)


class RawLogTests(ApiTestCase):
    """GET /api/raw: 範囲未指定・24時間超は400、正しい範囲は200。"""

    def test_missing_range_is_400(self) -> None:
        status, resp = self.call("GET", "/api/raw")
        self.assertEqual(status, 400)
        self.assertIn("error", resp)

    def test_range_over_24_hours_is_400(self) -> None:
        status, resp = self.call(
            "GET", "/api/raw",
            {"start": "2026-01-01T00:00:00+09:00", "end": "2026-01-02T00:00:01+09:00"},
        )
        self.assertEqual(status, 400)
        self.assertIn("error", resp)

    def test_valid_range_is_200(self) -> None:
        status, resp = self.call(
            "GET", "/api/raw",
            {"start": "2026-01-01T00:00:00+09:00", "end": "2026-01-01T01:00:00+09:00"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(resp, [])


class NotFoundTests(ApiTestCase):
    """存在しない id への PUT / DELETE、未知のパスは 404。"""

    def test_missing_activity_is_404(self) -> None:
        status, resp = self.call(
            "PUT", "/api/activity/999999",
            body={"start": "09:00", "end": "10:00", "activity_type": "coding"},
        )
        self.assertEqual(status, 404)
        status, resp = self.call("DELETE", "/api/activity/999999")
        self.assertEqual(status, 404)

    def test_missing_change_is_404(self) -> None:
        status, resp = self.call("PUT", "/api/change/999999", body={"description": "x"})
        self.assertEqual(status, 404)
        status, resp = self.call("DELETE", "/api/change/999999")
        self.assertEqual(status, 404)

    def test_missing_decision_is_404(self) -> None:
        status, resp = self.call("PUT", "/api/decision/999999", body={"decision": "x"})
        self.assertEqual(status, 404)
        status, resp = self.call("DELETE", "/api/decision/999999")
        self.assertEqual(status, 404)

    def test_unknown_path_is_404(self) -> None:
        status, resp = self.call("GET", "/api/no-such-endpoint")
        self.assertEqual(status, 404)


class BuildTests(ApiTestCase):
    """POST /api/build が 200 で応答すること。"""

    def test_build_returns_200(self) -> None:
        status, resp = self.call("POST", "/api/build", body={"date": timeutil.today().isoformat()})
        self.assertEqual(status, 200)
        self.assertTrue(resp["ok"])
        self.assertIn("count", resp)


class StateEndpointTests(ApiTestCase):
    """GET / POST /api/state: Current State の参照と実行（`StateSnapshotRepository.latest_for`）。"""

    def test_get_without_data_returns_state_none(self) -> None:
        """データが無い日でも 200 で state は None。"""
        status, payload = self.call("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertIsNone(payload["state"])

    def test_post_builds_state_and_total_min_matches_seeded_activity(self) -> None:
        """POST は組み立てて保存し、today.total_min が投入した活動の合計と一致する。"""
        today = timeutil.today()
        activity = Activity(
            start_at=timeutil.parse_hhmm("09:00", today),
            end_at=timeutil.parse_hhmm("10:30", today),
            activity_type=ActivityType.CODING,
            layer=ActivityLayer.OBSERVED,
            source=Source.WINDOWS,
            summary="Code.exe",
        )
        _seed_activity(self.config, activity)

        status, resp = self.call(
            "POST", "/api/state", body={"date": today.isoformat()}
        )
        self.assertEqual(status, 200)
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["state"]["today"]["total_min"], 90)

    def test_get_after_post_returns_same_snapshot(self) -> None:
        """POST の後に GET すると、同じ generated_at のスナップショットが返る（＝保存されている）。"""
        today = timeutil.today()
        status, posted = self.call(
            "POST", "/api/state", body={"date": today.isoformat()}
        )
        self.assertEqual(status, 200)

        status, got = self.call("GET", "/api/state", {"date": today.isoformat()})
        self.assertEqual(status, 200)
        self.assertEqual(got["generated_at"], posted["generated_at"])
        self.assertEqual(got["state"], posted["state"])

    def test_post_writes_state_json_file(self) -> None:
        """POST で paths.state_json のファイルが実際に作られ、JSON として読める。"""
        today = timeutil.today()
        status, resp = self.call(
            "POST", "/api/state", body={"date": today.isoformat()}
        )
        self.assertEqual(status, 200)

        path = Path(resp["path"])
        self.assertTrue(path.exists())
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["target_date"], today.isoformat())

    def test_get_returns_snapshot_for_requested_date_not_overall_latest(self) -> None:
        """別の日を POST した後でも、元の日を GET すればその日のスナップショットが返る
        （`latest_for` が target_date で絞っている確認。`latest()` は全体の最新を返すため区別が付かない）。
        """
        today = timeutil.today()
        other_day = (today - timedelta(days=1)).isoformat()

        status, first = self.call(
            "POST", "/api/state", body={"date": today.isoformat()}
        )
        self.assertEqual(status, 200)

        status, _ = self.call("POST", "/api/state", body={"date": other_day})
        self.assertEqual(status, 200)

        status, got = self.call("GET", "/api/state", {"date": today.isoformat()})
        self.assertEqual(status, 200)
        self.assertEqual(got["generated_at"], first["generated_at"])
        self.assertEqual(got["date"], today.isoformat())

    def test_get_invalid_date_is_400(self) -> None:
        """不正な日付は 400。"""
        status, resp = self.call("GET", "/api/state", {"date": "not-a-date"})
        self.assertEqual(status, 400)
        self.assertIn("error", resp)


# ---------------------------------------------------------------------------
# 収集・記録の状態表示（GET /api/day の "collector"）
# docs/20260923_ui_design.md「収集・記録の状態表示」: 判断材料は心拍のみ。
# 生ログの最終時刻では判断しない（まとめ書きで古く見えるため）。
# ---------------------------------------------------------------------------


class CollectorStatusTests(ApiTestCase):
    """`_collector_status` が `/api/day` へ付与する `collector` の判断規則。"""

    def test_day_response_has_collector_keys(self) -> None:
        status, payload = self.call("GET", "/api/day")
        self.assertEqual(status, 200)
        self.assertIn("collector", payload)
        self.assertEqual(
            set(payload["collector"].keys()),
            {
                "running",
                "owned_by_ui",  # この UI が起動した収集かどうか（停止ボタンの出し分けに使う）
                "last_event_at",
                "events_in_range",
                "interval_sec",
                "flush_every",
            },
        )

    def test_no_heartbeat_is_not_running(self) -> None:
        status, payload = self.call("GET", "/api/day")
        self.assertEqual(status, 200)
        self.assertFalse(payload["collector"]["running"])

    def test_fresh_heartbeat_is_running(self) -> None:
        _set_heartbeat(self.config, timeutil.now().isoformat())
        status, payload = self.call("GET", "/api/day")
        self.assertEqual(status, 200)
        self.assertTrue(payload["collector"]["running"])

    def test_heartbeat_older_than_3x_interval_is_not_running(self) -> None:
        # 境界: 採取間隔の3倍より古い心拍は「停止中」とみなす
        interval = int(self.config.get("collector.interval_sec", 5) or 5)
        old = timeutil.now() - timedelta(seconds=interval * 3 + 1)
        _set_heartbeat(self.config, old.isoformat())
        status, payload = self.call("GET", "/api/day")
        self.assertEqual(status, 200)
        self.assertFalse(payload["collector"]["running"])

    def test_broken_heartbeat_text_does_not_raise_and_is_not_running(self) -> None:
        _set_heartbeat(self.config, "abc")
        status, payload = self.call("GET", "/api/day")
        self.assertEqual(status, 200)
        self.assertFalse(payload["collector"]["running"])

    def test_raw_log_without_heartbeat_is_not_running(self) -> None:
        # まとめ書きで最終ログが古く見えても、判断材料は心拍だけであることの確認
        _add_raw_event(self.config, timeutil.now())
        status, payload = self.call("GET", "/api/day")
        self.assertEqual(status, 200)
        self.assertFalse(payload["collector"]["running"])

    def test_events_in_range_counts_that_day_and_zero_for_other_day(self) -> None:
        today = timeutil.today()
        _add_raw_event(self.config, timeutil.parse_hhmm("09:00", today))
        _add_raw_event(self.config, timeutil.parse_hhmm("10:00", today))

        status, payload = self.call("GET", "/api/day")
        self.assertEqual(status, 200)
        self.assertEqual(payload["collector"]["events_in_range"], 2)

        other_day = (today - timedelta(days=1)).isoformat()
        status, other = self.call("GET", "/api/day", {"date": other_day})
        self.assertEqual(status, 200)
        self.assertEqual(other["collector"]["events_in_range"], 0)

    def test_last_event_at_is_none_without_raw_log_then_last_logged_time(self) -> None:
        status, payload = self.call("GET", "/api/day")
        self.assertEqual(status, 200)
        self.assertIsNone(payload["collector"]["last_event_at"])

        today = timeutil.today()
        _add_raw_event(self.config, timeutil.parse_hhmm("09:00", today))
        last = timeutil.parse_hhmm("09:30", today)
        _add_raw_event(self.config, last)

        status, payload = self.call("GET", "/api/day")
        self.assertEqual(status, 200)
        self.assertEqual(payload["collector"]["last_event_at"], to_jsonable(last))


class CollectorHeartbeatLifecycleTests(unittest.TestCase):
    """`Collector.run` がサンプルごとに心拍を書き、終了時（finally）に消すこと。

    Windows 以外でも落ちないよう、win32 の取得関数はモックして実行環境に依存させない。
    採取間隔を1秒にして実行時間を数秒以内に収める。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.config = _temp_config(Path(self._tmp.name))
        # 実行時間を短くするため、採取間隔だけこのテスト用に上書きする
        self.config.data["collector"] = dict(self.config.data.get("collector", {}))
        self.config.data["collector"]["interval_sec"] = 1
        self.config.data["collector"]["flush_every"] = 100

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_heartbeat_is_removed_after_run_finishes(self) -> None:
        from contextflow.collector.win32 import ForegroundInfo

        fake_info = ForegroundInfo(process="Code.exe", window_title="t", pid=1)
        db = open_database(self.config)
        try:
            with mock.patch(
                "contextflow.collector.collector.get_foreground_info",
                return_value=fake_info,
            ), mock.patch(
                "contextflow.collector.collector.get_idle_sec", return_value=0
            ):
                collector = Collector(self.config, db)
                collector.run(duration_sec=1)

            self.assertIsNone(MetaRepository(db).get(HEARTBEAT_KEY))
        finally:
            db.close()


# ---------------------------------------------------------------------------
# server（HTTP 経由。最小限）
# ---------------------------------------------------------------------------


def _raw_request(
    method: str,
    path: str,
    *,
    host: str = "127.0.0.1",
    headers: dict | None = None,
    body: dict | None = None,
) -> tuple[int, bytes]:
    """`http.client` で生の HTTP リクエストを送る。

    Host ヘッダを自由に偽装するため（DNS rebinding テスト用）urllib は使わず、
    `skip_host=True` で自動付与を止めて自前で Host を載せる。
    毎回新しい接続を張って使い捨てるため `Connection: close` を必ず付ける
    （トークン不一致などサーバがボディを読まずに早期応答するケースで、
    HTTP/1.1 の持続接続のまま次のリクエストを読もうとして未読ボディを
    誤って読み込む挙動を避けるため）。
    """
    conn = http.client.HTTPConnection("127.0.0.1", _bound_port, timeout=5)
    try:
        data = None
        send_headers = dict(headers or {})
        send_headers.setdefault("Connection", "close")
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            send_headers.setdefault("Content-Type", "application/json; charset=utf-8")
            send_headers["Content-Length"] = str(len(data))
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host)
        for key, value in send_headers.items():
            conn.putheader(key, value)
        conn.endheaders(data)
        resp = conn.getresponse()
        status = resp.status
        payload = resp.read()
        return status, payload
    finally:
        conn.close()


class ServerHttpTests(unittest.TestCase):
    """server.py を実際に HTTP 経由で叩く最小限のテスト。

    ポートは `_SERVER_PORT` の固定値1つだけを使い、サーバを使うテストはこのクラスへまとめる。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.config = _temp_config(Path(cls._tmp.name))
        cls.server, cls.token = create_server(cls.config, port=_SERVER_PORT)
        global _bound_port
        _bound_port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.server.shutdown()
        finally:
            cls.server.server_close()
        cls.thread.join(timeout=5)
        cls._tmp.cleanup()

    def test_index_html_replaces_token(self) -> None:
        status, body = _raw_request("GET", "/")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertNotIn("<!--CF_TOKEN-->", text)
        self.assertIn(self.token, text)

    def test_static_js_and_css_are_200(self) -> None:
        status, _ = _raw_request("GET", "/app.js")
        self.assertEqual(status, 200)
        status, _ = _raw_request("GET", "/app.css")
        self.assertEqual(status, 200)

    def test_post_without_token_is_403(self) -> None:
        status, body = _raw_request("POST", "/api/change", body={"description": "x"})
        self.assertEqual(status, 403)

    def test_post_with_correct_token_is_200(self) -> None:
        status, body = _raw_request(
            "POST", "/api/change",
            headers={"X-CF-Token": self.token},
            body={"description": "x"},
        )
        self.assertEqual(status, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertTrue(payload["ok"])

    def test_forged_host_header_is_403(self) -> None:
        status, body = _raw_request("GET", "/api/day", host="evil.example.com")
        self.assertEqual(status, 403)

    def test_path_traversal_is_403_or_404(self) -> None:
        status, _ = _raw_request("GET", "/../config.toml")
        self.assertIn(status, (403, 404))


class CreateServerHostValidationTests(unittest.TestCase):
    """`create_server` に `host="0.0.0.0"` を渡すと拒否されること（実際にはbindしない）。"""

    def test_bind_all_interfaces_is_rejected_in_japanese(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            config = _temp_config(Path(tmp.name))
            with self.assertRaises(ValueError) as ctx:
                create_server(config, host="0.0.0.0", port=_SERVER_PORT)
            self.assertIn("127.0.0.1", str(ctx.exception))
        finally:
            tmp.cleanup()


# ---------------------------------------------------------------------------
# 選択肢の設定（種別の日本語ラベル・案件・タスク候補）
# docs/20260923_ui_design.md「3.1 選択肢の設定」・app/source/contextflow/ui/options.py
# ---------------------------------------------------------------------------


def _seed_task(config: AppConfig, title: str, project: str | None = None) -> int:
    """API を経由せず tasks テーブルへ直接1件 upsert する（`_seed_activity` と同じ考え方）。"""
    from contextflow.contracts.models import Task
    from contextflow.storage.repositories import TaskRepository

    db = open_database(config)
    try:
        return TaskRepository(db).upsert(Task(title=title, project=project))
    finally:
        db.close()


class OptionsDayIntegrationTests(ApiTestCase):
    """GET /api/day の応答に含まれる `options`（既存の `activity_types` との共存）。"""

    def test_day_options_has_three_keys(self) -> None:
        status, payload = self.call("GET", "/api/day")
        self.assertEqual(status, 200)
        self.assertIn("options", payload)
        self.assertEqual(
            set(payload["options"].keys()),
            {"activity_types", "projects", "task_suggestions"},
        )

    def test_day_options_activity_types_are_visible_ones_with_japanese_label(self) -> None:
        """選択肢は ActivityType の全値から、設定で隠した分を除いたもの。"""
        status, payload = self.call("GET", "/api/day")
        self.assertEqual(status, 200)
        types = payload["options"]["activity_types"]
        values = [t["value"] for t in types]

        hidden = set(self.config.get("ui.hidden_activity_types") or [])
        expected = {item.value for item in ActivityType} - hidden
        self.assertEqual(set(values), expected)
        self.assertEqual(len(values), len(set(values)))

        coding = next(t for t in types if t["value"] == "coding")
        self.assertEqual(coding["label"], "コーディング")
        # 保存値（value）は英語のまま変わっていないことの確認
        self.assertEqual(coding["value"], "coding")

    def test_day_options_exclude_planning_by_default(self) -> None:
        """planning は thinking へ集約したため、既定では選択肢に出さない。

        ただし ActivityType からは消さない（過去データの値を読めなくなるため）。
        """
        status, payload = self.call("GET", "/api/day")
        self.assertEqual(status, 200)
        values = [t["value"] for t in payload["options"]["activity_types"]]
        self.assertNotIn("planning", values)
        self.assertIn("thinking", values)
        # 母集合（後方互換の文字列配列）には残っていること
        self.assertIn("planning", payload["activity_types"])

    def test_legacy_activity_types_array_still_present(self) -> None:
        """後方互換: 既存の `activity_types`（文字列配列）がそのまま残っていること。"""
        status, payload = self.call("GET", "/api/day")
        self.assertEqual(status, 200)
        self.assertIn("activity_types", payload)
        self.assertEqual(
            payload["activity_types"], [item.value for item in ActivityType]
        )


class OptionsEndpointTests(ApiTestCase):
    """GET /api/options 単体・PUT /api/options（検証・保存）の確認。"""

    def test_get_options_matches_day_options(self) -> None:
        status, day = self.call("GET", "/api/day")
        self.assertEqual(status, 200)
        status, opts = self.call("GET", "/api/options")
        self.assertEqual(status, 200)
        self.assertEqual(opts, day["options"])

    def test_put_label_is_reflected_in_get_options_and_day(self) -> None:
        status, resp = self.call(
            "PUT", "/api/options",
            body={"activity_types": {"labels": {"coding": "プログラミング"}}},
        )
        self.assertEqual(status, 200)
        coding = next(t for t in resp["activity_types"] if t["value"] == "coding")
        self.assertEqual(coding["label"], "プログラミング")

        status, opts = self.call("GET", "/api/options")
        self.assertEqual(status, 200)
        coding = next(t for t in opts["activity_types"] if t["value"] == "coding")
        self.assertEqual(coding["label"], "プログラミング")

        status, day = self.call("GET", "/api/day")
        self.assertEqual(status, 200)
        coding = next(
            t for t in day["options"]["activity_types"] if t["value"] == "coding"
        )
        self.assertEqual(coding["label"], "プログラミング")

    def test_put_is_persisted_to_ui_options_json(self) -> None:
        from contextflow.ui import options as ui_options

        status, _ = self.call(
            "PUT", "/api/options",
            body={"activity_types": {"labels": {"coding": "プログラミング"}}},
        )
        self.assertEqual(status, 200)

        path = ui_options.options_path(self.config)
        self.assertTrue(path.exists())
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["activity_types"]["labels"]["coding"], "プログラミング")

    def test_hidden_type_is_removed_from_choices(self) -> None:
        status, resp = self.call(
            "PUT", "/api/options",
            body={"activity_types": {"hidden": ["break"]}},
        )
        self.assertEqual(status, 200)
        values = [t["value"] for t in resp["activity_types"]]
        self.assertNotIn("break", values)
        self.assertEqual(len(values), len(list(ActivityType)) - 1)

    def test_hiding_all_types_is_400_in_japanese(self) -> None:
        all_values = [item.value for item in ActivityType]
        status, resp = self.call(
            "PUT", "/api/options",
            body={"activity_types": {"hidden": all_values}},
        )
        self.assertEqual(status, 400)
        self.assertIn("error", resp)
        self.assertRegex(resp["error"], r"[ぁ-んァ-ン一-龥]")

    def test_order_is_applied_and_unlisted_values_come_after(self) -> None:
        status, resp = self.call(
            "PUT", "/api/options",
            body={"activity_types": {"order": ["other", "coding"]}},
        )
        self.assertEqual(status, 200)
        values = [t["value"] for t in resp["activity_types"]]
        self.assertEqual(values[0], "other")
        self.assertEqual(values[1], "coding")
        # order に無い値は元の ActivityType の並びのまま後ろに残る
        expected_rest = [
            item.value for item in ActivityType if item.value not in ("other", "coding")
        ]
        self.assertEqual(values[2:], expected_rest)

    def test_projects_extra_adds_from_repo_false(self) -> None:
        status, resp = self.call(
            "PUT", "/api/options",
            body={"projects": {"extra": ["社内_雑務"]}},
        )
        self.assertEqual(status, 200)
        extra = next(p for p in resp["projects"] if p["key"] == "社内_雑務")
        self.assertFalse(extra["from_repo"])

    def test_duplicate_extra_project_merges_to_one(self) -> None:
        """リポジトリが空の環境では extra 重複時の1件へのまとめのみ確認する（タスク仕様の代替条件）。"""
        status, resp = self.call(
            "PUT", "/api/options",
            body={"projects": {"extra": ["社内_雑務", "社内_雑務"]}},
        )
        self.assertEqual(status, 200)
        keys = [p["key"] for p in resp["projects"] if p["key"] == "社内_雑務"]
        self.assertEqual(len(keys), 1)

    def test_task_added_in_db_appears_in_suggestions_saved_first(self) -> None:
        _seed_task(self.config, "DB発のタスク")
        status, resp = self.call(
            "PUT", "/api/options",
            body={"tasks": {"suggestions": ["保存済みタスク"]}},
        )
        self.assertEqual(status, 200)
        suggestions = resp["task_suggestions"]
        self.assertIn("DB発のタスク", suggestions)
        self.assertIn("保存済みタスク", suggestions)
        self.assertLess(
            suggestions.index("保存済みタスク"), suggestions.index("DB発のタスク")
        )

    def test_too_many_task_suggestions_is_400(self) -> None:
        status, resp = self.call(
            "PUT", "/api/options",
            body={"tasks": {"suggestions": [f"タスク{i}" for i in range(51)]}},
        )
        self.assertEqual(status, 400)
        self.assertIn("error", resp)

    def test_invalid_activity_type_key_in_labels_is_400(self) -> None:
        status, resp = self.call(
            "PUT", "/api/options",
            body={"activity_types": {"labels": {"no_such_type": "何か"}}},
        )
        self.assertEqual(status, 400)
        self.assertIn("error", resp)

    def test_unknown_top_level_key_does_not_raise(self) -> None:
        status, resp = self.call(
            "PUT", "/api/options",
            body={"unknown_section": {"anything": True}},
        )
        self.assertEqual(status, 200)
        self.assertIn("activity_types", resp)

    def test_missing_context_repo_does_not_raise_on_get_options(self) -> None:
        # _temp_config の context_repo は一時ディレクトリ配下の存在しないパス
        status, resp = self.call("GET", "/api/options")
        self.assertEqual(status, 200)
        self.assertIn("projects", resp)


# ---------------------------------------------------------------------------
# 判断・計画（GET /api/decide/info, POST /api/decide, POST /api/plan）
# ---------------------------------------------------------------------------


class DecideInfoTests(ApiTestCase):
    """GET /api/decide/info: 運用方針と質問セットの一覧。"""

    def test_info_has_expected_sets(self) -> None:
        status, resp = self.call("GET", "/api/decide/info")
        self.assertEqual(status, 200)
        names = {item["name"] for item in resp["sets"]}
        self.assertEqual(
            names,
            {"next_action", "gap_fill", "task_triage", "week_outlook", "deadline_risk"},
        )

    def test_info_reports_external_send(self) -> None:
        """外部へ問い合わせるかどうかを、運用方針から正しく伝えること。

        画面はこの値だけを見て確認ダイアログと警告を出す。
        「LLM かどうか」ではなく「外部送信が起きるか」で判定する
        （jev は LLM ではないが、Jev Decision API へ HTTP で送る）。
        """
        status, resp = self.call("GET", "/api/decide/info")
        self.assertEqual(status, 200)
        self.assertIsInstance(resp["sends_external"], bool)
        self.assertIsInstance(resp["external_engines"], list)
        self.assertIsInstance(resp["planner_external"], bool)
        # 既定は rule_first（ローカル完結）なので外部送信は起きない
        self.assertFalse(resp["sends_external"])
        self.assertEqual(resp["external_engines"], [])
        # ローカル完結とみなすのは rule_based だけ。
        # 新しいエンジンを足したとき、既定で「外部送信あり」に倒れるようにしておく
        self.assertEqual(set(api._LOCAL_ENGINES), {"rule_based"})


class DecideTests(ApiTestCase):
    """POST /api/decide: 型付き判断を実行して記録する。"""

    def test_decide_returns_answers_in_question_set_order(self) -> None:
        status, resp = self.call("POST", "/api/decide", body={"set": "next_action"})
        self.assertEqual(status, 200)
        keys = [answer["key"] for answer in resp["answers"]]
        self.assertEqual(keys, ["continue_current_task", "next_task_type", "urgency"])

    def test_decide_saves_state_before_deciding(self) -> None:
        """判断前に state が保存され、GET /api/state で同じスナップショットが取れる。"""
        today = timeutil.today()
        status, resp = self.call("POST", "/api/decide", body={"date": today.isoformat()})
        self.assertEqual(status, 200)

        status, state_resp = self.call("GET", "/api/state", {"date": today.isoformat()})
        self.assertEqual(status, 200)
        self.assertIsNotNone(state_resp["state"])
        self.assertEqual(state_resp["generated_at"], resp["generated_at"])

    def test_held_matches_confidence_below_threshold(self) -> None:
        status, resp = self.call("POST", "/api/decide")
        self.assertEqual(status, 200)
        self.assertTrue(resp["answers"])
        for answer in resp["answers"]:
            self.assertEqual(answer["held"], answer["confidence"] < resp["threshold"])

    def test_unknown_set_is_400(self) -> None:
        status, resp = self.call("POST", "/api/decide", body={"set": "no_such_set"})
        self.assertEqual(status, 400)
        self.assertIn("存在しない質問セット", resp["error"])

    def test_decision_is_recorded_in_decision_logs(self) -> None:
        from contextflow.storage.repositories import DecisionLogRepository

        status, resp = self.call("POST", "/api/decide", body={"set": "task_triage"})
        self.assertEqual(status, 200)

        db = open_database(self.config)
        try:
            rows = DecisionLogRepository(db).recent(limit=10)
        finally:
            db.close()
        self.assertGreaterEqual(len(rows), len(resp["answers"]))


class PlanTests(ApiTestCase):
    """POST /api/plan: decide と同じ処理に加え、説明・計画文を返す。"""

    def test_plan_returns_nonempty_text_and_answers(self) -> None:
        status, resp = self.call("POST", "/api/plan", body={"set": "next_action"})
        self.assertEqual(status, 200)
        self.assertIsInstance(resp["text"], str)
        self.assertTrue(resp["text"])
        self.assertEqual(len(resp["answers"]), 3)


if __name__ == "__main__":
    unittest.main()
