"""各テーブルへの読み書きをまとめるリポジトリ層。

- datetime は `timeutil.ensure_aware` で tz を保証し、DB には ISO8601 文字列で保存する。
- Enum カラム（activity_type / layer / source / status）は `.value` で保存し、
  読み出し時に Enum へ戻す。
- UNIQUE 制約がある表は `INSERT ... ON CONFLICT ... DO UPDATE`（upsert）で扱う。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from typing import Any, Optional, Sequence

from contextflow.contracts.decision import DecisionRequest, DecisionResponse
from contextflow.contracts.models import (
    Activity,
    ActivityLayer,
    ActivityType,
    Change,
    CurrentState,
    Decision,
    Project,
    RawEvent,
    Session,
    Source,
    Task,
    TaskStatus,
)
from contextflow.contracts.serde import parse_date, parse_datetime, to_jsonable
from contextflow.storage.db import Database
from contextflow.timeutil import ensure_aware, now


# ---------------------------------------------------------------------------
# row -> dataclass 変換ヘルパ
# ---------------------------------------------------------------------------


def _row_to_raw_event(row: sqlite3.Row) -> RawEvent:
    return RawEvent(
        id=row["id"],
        ts=parse_datetime(row["ts"]),
        process=row["process"],
        window_title=row["window_title"],
        idle_sec=row["idle_sec"],
        host=row["host"],
    )


def _row_to_session(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"],
        start_at=parse_datetime(row["start_at"]),
        end_at=parse_datetime(row["end_at"]),
        process=row["process"],
        window_title=row["window_title"],
        duration_sec=row["duration_sec"],
        idle_sec=row["idle_sec"],
        sample_count=row["sample_count"],
    )


def _activity_params(a: Activity) -> tuple:
    """activities テーブルへの INSERT/UPSERT 用パラメータタプル。"""
    return (
        ensure_aware(a.start_at).isoformat(),
        ensure_aware(a.end_at).isoformat(),
        a.activity_type.value,
        a.layer.value,
        a.source.value,
        a.project,
        a.task,
        a.confidence,
        a.summary,
        json.dumps(a.detail, ensure_ascii=False),
    )


def _row_to_activity(row: sqlite3.Row) -> Activity:
    detail = json.loads(row["detail"]) if row["detail"] else {}
    return Activity(
        id=row["id"],
        start_at=parse_datetime(row["start_at"]),
        end_at=parse_datetime(row["end_at"]),
        activity_type=ActivityType(row["activity_type"]),
        layer=ActivityLayer(row["layer"]),
        source=Source(row["source"]),
        project=row["project"],
        task=row["task"],
        confidence=row["confidence"],
        summary=row["summary"],
        detail=detail,
    )


def _row_to_change(row: sqlite3.Row) -> Change:
    return Change(
        id=row["id"],
        ts=parse_datetime(row["ts"]),
        description=row["description"],
        project=row["project"],
        source=Source(row["source"]),
    )


def _row_to_decision(row: sqlite3.Row) -> Decision:
    return Decision(
        id=row["id"],
        ts=parse_datetime(row["ts"]),
        decision=row["decision"],
        reason=row["reason"],
        project=row["project"],
        source=Source(row["source"]),
        change_id=row["change_id"],
    )


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        title=row["title"],
        project=row["project"],
        status=TaskStatus(row["status"]),
        priority=row["priority"],
        deadline=parse_date(row["deadline"]) if row["deadline"] else None,
        github_issue=row["github_issue"],
        blocked=bool(row["blocked"]),
        remaining_steps=row["remaining_steps"],
        updated_at=parse_datetime(row["updated_at"]) if row["updated_at"] else None,
    )


def _row_to_project(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"],
        name=row["name"],
        title=row["title"],
        status=row["status"],
        note=row["note"],
    )


def _row_to_calibration(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["params"] = json.loads(d["params"]) if d["params"] else {}
    return d


def _value_to_text(value: Any) -> Optional[str]:
    """Answer.value（bool/str/int/float/None）を decision_logs.value の TEXT へ。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# ---------------------------------------------------------------------------
# raw_events
# ---------------------------------------------------------------------------


class RawEventRepository:
    """前面ウィンドウの生ログ。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def add(self, event: RawEvent) -> int:
        conn = self._db.connect()
        cur = conn.execute(
            "INSERT INTO raw_events (ts, process, window_title, idle_sec, host) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                ensure_aware(event.ts).isoformat(),
                event.process,
                event.window_title,
                event.idle_sec,
                event.host,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)

    def add_many(self, events: Sequence[RawEvent]) -> int:
        conn = self._db.connect()
        rows = [
            (
                ensure_aware(e.ts).isoformat(),
                e.process,
                e.window_title,
                e.idle_sec,
                e.host,
            )
            for e in events
        ]
        if rows:
            conn.executemany(
                "INSERT INTO raw_events (ts, process, window_title, idle_sec, host) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        return len(rows)

    def list_between(self, start: datetime, end: datetime) -> list[RawEvent]:
        conn = self._db.connect()
        cur = conn.execute(
            "SELECT * FROM raw_events WHERE ts >= ? AND ts < ? ORDER BY ts",
            (ensure_aware(start).isoformat(), ensure_aware(end).isoformat()),
        )
        return [_row_to_raw_event(row) for row in cur.fetchall()]

    def last(self) -> Optional[RawEvent]:
        conn = self._db.connect()
        cur = conn.execute("SELECT * FROM raw_events ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        return _row_to_raw_event(row) if row else None


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


class SessionRepository:
    """raw_events を圧縮した session。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def replace_between(
        self, start: datetime, end: datetime, sessions: Sequence[Session]
    ) -> int:
        """[start, end) の既存 session を削除してから挿入し直す。"""
        conn = self._db.connect()
        conn.execute(
            "DELETE FROM sessions WHERE start_at >= ? AND start_at < ?",
            (ensure_aware(start).isoformat(), ensure_aware(end).isoformat()),
        )
        count = 0
        for s in sessions:
            conn.execute(
                "INSERT INTO sessions (start_at, end_at, process, window_title, "
                "duration_sec, idle_sec, sample_count) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (start_at, process, window_title) DO UPDATE SET "
                "end_at=excluded.end_at, duration_sec=excluded.duration_sec, "
                "idle_sec=excluded.idle_sec, sample_count=excluded.sample_count",
                (
                    ensure_aware(s.start_at).isoformat(),
                    ensure_aware(s.end_at).isoformat(),
                    s.process,
                    s.window_title,
                    s.duration_sec,
                    s.idle_sec,
                    s.sample_count,
                ),
            )
            count += 1
        conn.commit()
        return count

    def list_between(self, start: datetime, end: datetime) -> list[Session]:
        conn = self._db.connect()
        cur = conn.execute(
            "SELECT * FROM sessions WHERE start_at >= ? AND start_at < ? ORDER BY start_at",
            (ensure_aware(start).isoformat(), ensure_aware(end).isoformat()),
        )
        return [_row_to_session(row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# activities
# ---------------------------------------------------------------------------


class ActivityRepository:
    """PC作業・非PC作業を同じ形で持つ活動実績。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def _upsert(self, conn: sqlite3.Connection, activity: Activity) -> int:
        cur = conn.execute(
            "INSERT INTO activities (start_at, end_at, activity_type, layer, source, "
            "project, task, confidence, summary, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (start_at, end_at, layer, source, activity_type) DO UPDATE SET "
            "project=excluded.project, task=excluded.task, "
            "confidence=excluded.confidence, summary=excluded.summary, "
            "detail=excluded.detail "
            "RETURNING id",
            _activity_params(activity),
        )
        row = cur.fetchone()
        return int(row["id"])

    def add(self, activity: Activity) -> int:
        conn = self._db.connect()
        new_id = self._upsert(conn, activity)
        conn.commit()
        return new_id

    def replace_between(
        self,
        start: datetime,
        end: datetime,
        activities: Sequence[Activity],
        *,
        layer: Optional[ActivityLayer] = None,
        source: Optional[Source] = None,
    ) -> int:
        """[start, end) の既存 activity を削除してから挿入し直す。

        layer / source を指定した場合はその条件も削除の絞り込みに加える。
        """
        conn = self._db.connect()
        query = "DELETE FROM activities WHERE start_at >= ? AND start_at < ?"
        params: list[Any] = [ensure_aware(start).isoformat(), ensure_aware(end).isoformat()]
        if layer is not None:
            query += " AND layer = ?"
            params.append(layer.value)
        if source is not None:
            query += " AND source = ?"
            params.append(source.value)
        conn.execute(query, params)

        count = 0
        for a in activities:
            self._upsert(conn, a)
            count += 1
        conn.commit()
        return count

    def list_between(
        self, start: datetime, end: datetime, *, layer: Optional[ActivityLayer] = None
    ) -> list[Activity]:
        conn = self._db.connect()
        query = "SELECT * FROM activities WHERE start_at >= ? AND start_at < ?"
        params: list[Any] = [ensure_aware(start).isoformat(), ensure_aware(end).isoformat()]
        if layer is not None:
            query += " AND layer = ?"
            params.append(layer.value)
        query += " ORDER BY start_at"
        cur = conn.execute(query, params)
        return [_row_to_activity(row) for row in cur.fetchall()]

    def current(
        self, at: datetime, *, layer: Optional[ActivityLayer] = None
    ) -> Optional[Activity]:
        """`at` を含む区間の activity を1件返す。"""
        conn = self._db.connect()
        at_iso = ensure_aware(at).isoformat()
        query = "SELECT * FROM activities WHERE start_at <= ? AND end_at > ?"
        params: list[Any] = [at_iso, at_iso]
        if layer is not None:
            query += " AND layer = ?"
            params.append(layer.value)
        query += " ORDER BY start_at DESC LIMIT 1"
        cur = conn.execute(query, params)
        row = cur.fetchone()
        return _row_to_activity(row) if row else None

    def get(self, activity_id: int) -> Optional[Activity]:
        conn = self._db.connect()
        cur = conn.execute("SELECT * FROM activities WHERE id = ?", (activity_id,))
        row = cur.fetchone()
        return _row_to_activity(row) if row else None

    def update(self, activity: Activity) -> bool:
        """`activity.id` を条件に全カラムを UPDATE する。

        UNIQUE(start_at, end_at, layer, source, activity_type) と衝突した場合は
        呼び出し側が 409 を返せるよう、日本語メッセージの ValueError に変換する。
        """
        if activity.id is None:
            raise ValueError("id が指定されていない")
        conn = self._db.connect()
        params = (*_activity_params(activity), activity.id)
        try:
            cur = conn.execute(
                "UPDATE activities SET start_at=?, end_at=?, activity_type=?, layer=?, "
                "source=?, project=?, task=?, confidence=?, summary=?, detail=? "
                "WHERE id=?",
                params,
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("同じ時間帯・種別の活動が既にある") from exc
        conn.commit()
        return cur.rowcount > 0

    def delete(self, activity_id: int) -> bool:
        conn = self._db.connect()
        cur = conn.execute("DELETE FROM activities WHERE id = ?", (activity_id,))
        conn.commit()
        return cur.rowcount > 0

    def update_type_by_uid(
        self,
        uid: str,
        activity_type: ActivityType,
        *,
        layer: ActivityLayer = ActivityLayer.PLANNED,
    ) -> int:
        """`detail.uid` が一致する行の activity_type をまとめて更新する。

        カレンダーの予定へ種別ラベルを付けたとき、次の calendar sync を待たずに
        既存の planned 行へ反映するために使う（画面・build への即時反映のため）。
        detail は JSON文字列で持っており SQL 側では絞り込めないため、対象層を
        いったん読み出し、Python 側で detail.uid を照合してから1件ずつ UPDATE する。
        """
        conn = self._db.connect()
        cur = conn.execute("SELECT * FROM activities WHERE layer = ?", (layer.value,))
        rows = cur.fetchall()
        count = 0
        for row in rows:
            detail = json.loads(row["detail"]) if row["detail"] else {}
            if str(detail.get("uid") or "") != uid:
                continue
            conn.execute(
                "UPDATE activities SET activity_type = ? WHERE id = ?",
                (activity_type.value, row["id"]),
            )
            count += 1
        conn.commit()
        return count


# ---------------------------------------------------------------------------
# changes / decisions
# ---------------------------------------------------------------------------


class ChangeRepository:
    """状況の変化。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def add(self, change: Change) -> int:
        conn = self._db.connect()
        cur = conn.execute(
            "INSERT INTO changes (ts, project, description, source) VALUES (?, ?, ?, ?)",
            (
                ensure_aware(change.ts).isoformat(),
                change.project,
                change.description,
                change.source.value,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)

    def recent(self, limit: int = 10) -> list[Change]:
        conn = self._db.connect()
        cur = conn.execute("SELECT * FROM changes ORDER BY ts DESC LIMIT ?", (limit,))
        return [_row_to_change(row) for row in cur.fetchall()]

    def list_between(self, start: datetime, end: datetime) -> list[Change]:
        conn = self._db.connect()
        cur = conn.execute(
            "SELECT * FROM changes WHERE ts >= ? AND ts < ? ORDER BY ts",
            (ensure_aware(start).isoformat(), ensure_aware(end).isoformat()),
        )
        return [_row_to_change(row) for row in cur.fetchall()]

    def get(self, change_id: int) -> Optional[Change]:
        conn = self._db.connect()
        cur = conn.execute("SELECT * FROM changes WHERE id = ?", (change_id,))
        row = cur.fetchone()
        return _row_to_change(row) if row else None

    def update(self, change: Change) -> bool:
        if change.id is None:
            raise ValueError("id が指定されていない")
        conn = self._db.connect()
        cur = conn.execute(
            "UPDATE changes SET ts=?, project=?, description=?, source=? WHERE id=?",
            (
                ensure_aware(change.ts).isoformat(),
                change.project,
                change.description,
                change.source.value,
                change.id,
            ),
        )
        conn.commit()
        return cur.rowcount > 0

    def delete(self, change_id: int) -> bool:
        conn = self._db.connect()
        cur = conn.execute("DELETE FROM changes WHERE id = ?", (change_id,))
        conn.commit()
        return cur.rowcount > 0


class DecisionRepository:
    """人の判断（decisions テーブル）。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def add(self, decision: Decision) -> int:
        conn = self._db.connect()
        cur = conn.execute(
            "INSERT INTO decisions (ts, project, decision, reason, source, change_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                ensure_aware(decision.ts).isoformat(),
                decision.project,
                decision.decision,
                decision.reason,
                decision.source.value,
                decision.change_id,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)

    def recent(self, limit: int = 10) -> list[Decision]:
        conn = self._db.connect()
        cur = conn.execute("SELECT * FROM decisions ORDER BY ts DESC LIMIT ?", (limit,))
        return [_row_to_decision(row) for row in cur.fetchall()]

    def list_between(self, start: datetime, end: datetime) -> list[Decision]:
        conn = self._db.connect()
        cur = conn.execute(
            "SELECT * FROM decisions WHERE ts >= ? AND ts < ? ORDER BY ts",
            (ensure_aware(start).isoformat(), ensure_aware(end).isoformat()),
        )
        return [_row_to_decision(row) for row in cur.fetchall()]

    def get(self, decision_id: int) -> Optional[Decision]:
        conn = self._db.connect()
        cur = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,))
        row = cur.fetchone()
        return _row_to_decision(row) if row else None

    def update(self, decision: Decision) -> bool:
        if decision.id is None:
            raise ValueError("id が指定されていない")
        conn = self._db.connect()
        cur = conn.execute(
            "UPDATE decisions SET ts=?, project=?, decision=?, reason=?, source=?, "
            "change_id=? WHERE id=?",
            (
                ensure_aware(decision.ts).isoformat(),
                decision.project,
                decision.decision,
                decision.reason,
                decision.source.value,
                decision.change_id,
                decision.id,
            ),
        )
        conn.commit()
        return cur.rowcount > 0

    def delete(self, decision_id: int) -> bool:
        conn = self._db.connect()
        cur = conn.execute("DELETE FROM decisions WHERE id = ?", (decision_id,))
        conn.commit()
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# tasks / projects
# ---------------------------------------------------------------------------


class TaskRepository:
    """次にやること。(title, project) で一意。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def upsert(self, task: Task) -> int:
        """(title, project) で一意に upsert する。

        SQLite の UNIQUE は NULL を毎回別の値として扱うため、
        `ON CONFLICT (title, project)` では project=None の行が重複する。
        既存行を明示的に引いてから UPDATE / INSERT する。
        """
        conn = self._db.connect()
        deadline = task.deadline.isoformat() if task.deadline else None
        updated_at = ensure_aware(task.updated_at).isoformat() if task.updated_at else None
        values = (
            task.status.value,
            task.priority,
            deadline,
            task.github_issue,
            int(task.blocked),
            task.remaining_steps,
            updated_at,
        )
        existing = conn.execute(
            "SELECT id FROM tasks WHERE title = ? AND project IS ?", (task.title, task.project)
        ).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE tasks SET status=?, priority=?, deadline=?, github_issue=?, "
                "blocked=?, remaining_steps=?, updated_at=? WHERE id=?",
                (*values, existing["id"]),
            )
            conn.commit()
            return int(existing["id"])
        cur = conn.execute(
            "INSERT INTO tasks (title, project, status, priority, deadline, "
            "github_issue, blocked, remaining_steps, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (task.title, task.project, *values),
        )
        row = cur.fetchone()
        conn.commit()
        return int(row["id"])

    def list(self, *, status: Optional[TaskStatus] = None) -> list[Task]:
        conn = self._db.connect()
        if status is None:
            cur = conn.execute("SELECT * FROM tasks ORDER BY priority, id")
        else:
            cur = conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY priority, id",
                (status.value,),
            )
        return [_row_to_task(row) for row in cur.fetchall()]

    def get(self, task_id: int) -> Optional[Task]:
        conn = self._db.connect()
        cur = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = cur.fetchone()
        return _row_to_task(row) if row else None

    def find_by_title(self, title: str, project: Optional[str] = None) -> Optional[Task]:
        conn = self._db.connect()
        cur = conn.execute(
            "SELECT * FROM tasks WHERE title = ? AND project IS ?", (title, project)
        )
        row = cur.fetchone()
        return _row_to_task(row) if row else None

    def counts(self) -> dict[str, int]:
        conn = self._db.connect()
        cur = conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
        return {row["status"]: row["n"] for row in cur.fetchall()}


class ProjectRepository:
    """プロジェクト（案件）。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def upsert(self, project: Project) -> int:
        conn = self._db.connect()
        cur = conn.execute(
            "INSERT INTO projects (name, title, status, note) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (name) DO UPDATE SET "
            "title=excluded.title, status=excluded.status, note=excluded.note "
            "RETURNING id",
            (project.name, project.title, project.status, project.note),
        )
        row = cur.fetchone()
        conn.commit()
        return int(row["id"])

    def list(self) -> list[Project]:
        conn = self._db.connect()
        cur = conn.execute("SELECT * FROM projects ORDER BY name")
        return [_row_to_project(row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# decision engine 関連
# ---------------------------------------------------------------------------


class DecisionLogRepository:
    """Decision Engine への問い合わせ履歴（校正の材料）。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def add_response(
        self, request: DecisionRequest, response: DecisionResponse
    ) -> list[int]:
        conn = self._db.connect()
        ts = ensure_aware(now()).isoformat()
        state_json = json.dumps(request.state, ensure_ascii=False, default=str)
        ids: list[int] = []
        for question in request.questions:
            answer = response.answers.get(question.key)
            if answer is None:
                continue
            cur = conn.execute(
                "INSERT INTO decision_logs (ts, engine, question_key, question_type, "
                "value, raw_confidence, confidence, latency_ms, state_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    response.engine or answer.engine,
                    question.key,
                    question.type.value,
                    _value_to_text(answer.value),
                    answer.raw_confidence,
                    answer.confidence,
                    response.latency_ms,
                    state_json,
                ),
            )
            ids.append(int(cur.lastrowid))
        conn.commit()
        return ids

    def recent(
        self,
        *,
        engine: Optional[str] = None,
        question_key: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict]:
        conn = self._db.connect()
        query = "SELECT * FROM decision_logs WHERE 1=1"
        params: list[Any] = []
        if engine is not None:
            query += " AND engine = ?"
            params.append(engine)
        if question_key is not None:
            query += " AND question_key = ?"
            params.append(question_key)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        cur = conn.execute(query, params)
        return [dict(row) for row in cur.fetchall()]


class FeedbackRepository:
    """実際どうだったか（AI判断の答え合わせ）。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def add(
        self,
        *,
        engine: str,
        question_key: str,
        predicted: str,
        actual: str,
        correct: bool,
        raw_confidence: float,
        decision_log_id: Optional[int] = None,
        note: str = "",
    ) -> int:
        conn = self._db.connect()
        ts = ensure_aware(now()).isoformat()
        cur = conn.execute(
            "INSERT INTO decision_feedback (ts, decision_log_id, engine, question_key, "
            "predicted, actual, correct, raw_confidence, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts,
                decision_log_id,
                engine,
                question_key,
                predicted,
                actual,
                int(correct),
                raw_confidence,
                note,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)

    def list(
        self, *, engine: Optional[str] = None, question_key: Optional[str] = None
    ) -> list[dict]:
        conn = self._db.connect()
        query = "SELECT * FROM decision_feedback WHERE 1=1"
        params: list[Any] = []
        if engine is not None:
            query += " AND engine = ?"
            params.append(engine)
        if question_key is not None:
            query += " AND question_key = ?"
            params.append(question_key)
        query += " ORDER BY id DESC"
        cur = conn.execute(query, params)
        return [dict(row) for row in cur.fetchall()]


class CalibrationRepository:
    """校正モデル（isotonic / platt のパラメータを JSON で保持）。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def save(
        self,
        *,
        engine: str,
        question_key: str,
        method: str,
        params: dict,
        sample_count: int,
    ) -> int:
        conn = self._db.connect()
        fitted_at = ensure_aware(now()).isoformat()
        cur = conn.execute(
            "INSERT INTO calibration_models (engine, question_key, method, params, "
            "sample_count, fitted_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (engine, question_key) DO UPDATE SET "
            "method=excluded.method, params=excluded.params, "
            "sample_count=excluded.sample_count, fitted_at=excluded.fitted_at "
            "RETURNING id",
            (
                engine,
                question_key,
                method,
                json.dumps(params, ensure_ascii=False),
                sample_count,
                fitted_at,
            ),
        )
        row = cur.fetchone()
        conn.commit()
        return int(row["id"])

    def get(self, engine: str, question_key: str) -> Optional[dict]:
        conn = self._db.connect()
        cur = conn.execute(
            "SELECT * FROM calibration_models WHERE engine = ? AND question_key = ?",
            (engine, question_key),
        )
        row = cur.fetchone()
        return _row_to_calibration(row) if row else None

    def list(self) -> list[dict]:
        conn = self._db.connect()
        cur = conn.execute("SELECT * FROM calibration_models ORDER BY engine, question_key")
        return [_row_to_calibration(row) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# state_snapshots / meta
# ---------------------------------------------------------------------------


class StateSnapshotRepository:
    """Context Builder が生成した state のスナップショット。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def add(self, state: CurrentState) -> int:
        conn = self._db.connect()
        state_json = json.dumps(to_jsonable(state), ensure_ascii=False)
        cur = conn.execute(
            "INSERT INTO state_snapshots (generated_at, target_date, state_json) "
            "VALUES (?, ?, ?)",
            (
                ensure_aware(state.generated_at).isoformat(),
                state.target_date.isoformat(),
                state_json,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)

    def latest(self) -> Optional[dict]:
        conn = self._db.connect()
        cur = conn.execute("SELECT * FROM state_snapshots ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        if row is None:
            return None
        result = dict(row)
        result["state_json"] = json.loads(result["state_json"])
        return result

    def latest_for(self, target: date) -> Optional[dict]:
        """対象日の最新スナップショット。無ければ None。"""
        conn = self._db.connect()
        cur = conn.execute(
            "SELECT * FROM state_snapshots WHERE target_date = ? "
            "ORDER BY id DESC LIMIT 1",
            (target.isoformat(),),
        )
        row = cur.fetchone()
        if row is None:
            return None
        result = dict(row)
        result["state_json"] = json.loads(result["state_json"])
        return result


class MetaRepository:
    """スキーマ版などの内部メモ（key-value）。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self, key: str) -> Optional[str]:
        conn = self._db.connect()
        cur = conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None

    def set(self, key: str, value: str) -> None:
        conn = self._db.connect()
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()

    def delete(self, key: str) -> None:
        conn = self._db.connect()
        conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        conn.commit()


# ---------------------------------------------------------------------------
# calendar_labels
# ---------------------------------------------------------------------------


class CalendarLabelRepository:
    """予定（カレンダー）に人が付けた種別。uid をキーに持ち、再取得で消えない。

    calendar sync は日ごとに planned 行をまるごと削除→再挿入するため、
    予定の行自体に種別を持たせると再取得のたびに消えてしまう。そのため
    種別だけをこの別テーブルへ uid キーで保持し、events_to_activities で
    Activity へ変換するタイミングで当てはめる。
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def all(self) -> dict[str, ActivityType]:
        conn = self._db.connect()
        cur = conn.execute("SELECT uid, activity_type FROM calendar_labels")
        return {row["uid"]: ActivityType(row["activity_type"]) for row in cur.fetchall()}

    def get(self, uid: str) -> Optional[ActivityType]:
        conn = self._db.connect()
        cur = conn.execute(
            "SELECT activity_type FROM calendar_labels WHERE uid = ?", (uid,)
        )
        row = cur.fetchone()
        return ActivityType(row["activity_type"]) if row else None

    def set(self, uid: str, activity_type: ActivityType) -> None:
        conn = self._db.connect()
        conn.execute(
            "INSERT INTO calendar_labels (uid, activity_type, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (uid) DO UPDATE SET "
            "activity_type=excluded.activity_type, updated_at=excluded.updated_at",
            (uid, activity_type.value, ensure_aware(now()).isoformat()),
        )
        conn.commit()

    def delete(self, uid: str) -> None:
        conn = self._db.connect()
        conn.execute("DELETE FROM calendar_labels WHERE uid = ?", (uid,))
        conn.commit()
