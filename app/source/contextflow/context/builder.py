"""Context Builder。

Windowsログ・カレンダー・GitHub などを Decision Engine が毎回読むのではなく、
ここで1つの `CurrentState` に整理し、その状態だけを判断系へ渡す。
システム全体の中心となるデータ構造を作る層。
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional

from contextflow import timeutil
from contextflow.config import AppConfig
from contextflow.context.features import compute_features, focus_minutes, summarize_time
from contextflow.contracts.models import (
    Activity,
    ActivityLayer,
    Change,
    CurrentState,
    Decision,
    PlannedItem,
    PastSummary,
    Task,
    TaskStatus,
    UpcomingSummary,
)
from contextflow.contracts.serde import to_jsonable
from contextflow.storage.db import Database
from contextflow.storage.repositories import (
    ActivityRepository,
    ChangeRepository,
    DecisionRepository,
    SessionRepository,
    StateSnapshotRepository,
    TaskRepository,
)

# 未完了とみなすタスク状態
_UNFINISHED = (TaskStatus.OPEN, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED)

# candidate_tasks の最大件数 / recent_* の件数
_MAX_CANDIDATES = 5
_MAX_RECENT = 5

# 締切が無いタスクを並べ替えるときの大きい既定値
_NO_DEADLINE = 10 ** 6


class ContextBuilder:
    """DB の内容から `CurrentState` を組み立てる。"""

    def __init__(self, db: Database, config: AppConfig) -> None:
        self._db = db
        self._config = config
        self._activities = ActivityRepository(db)
        self._sessions = SessionRepository(db)
        self._tasks = TaskRepository(db)
        self._changes = ChangeRepository(db)
        self._decisions = DecisionRepository(db)
        self._snapshots = StateSnapshotRepository(db)

    # ------------------------------------------------------------------
    # 組み立て
    # ------------------------------------------------------------------

    def build(
        self, target: Optional[date] = None, now: Optional[datetime] = None
    ) -> CurrentState:
        """対象日の状態を作る。データが1件も無くても例外は出さない。"""
        target_date = target or timeutil.today()
        start, end = timeutil.day_range(target_date)
        at = self._clamp_now(now, start, end)

        activities = self._load_activities(start, end)
        sessions = self._sessions.list_between(start, end)

        current_activity = self._find_current(activities, at)
        current_task = self._find_current_task(current_activity)
        counts = self._tasks.counts()

        return CurrentState(
            generated_at=timeutil.now(),
            target_date=target_date,
            now_time=timeutil.hhmm(at),
            today=summarize_time(activities),
            features=compute_features(activities, sessions, self._config, now=at),
            current_activity=current_activity,
            current_task=current_task,
            task_elapsed_min=self._task_elapsed_min(activities, current_task),
            open_tasks=sum(counts.get(s.value, 0) for s in _UNFINISHED),
            blocked_tasks=counts.get(TaskStatus.BLOCKED.value, 0),
            candidate_tasks=self._candidate_tasks(target_date),
            recent_changes=self._changes.recent(_MAX_RECENT),
            recent_decisions=self._decisions.recent(_MAX_RECENT),
            constraints=self._read_constraints(),
            past=self._build_past(target_date, start),
            upcoming=self._build_upcoming(at),
        )

    def _clamp_now(
        self, now: Optional[datetime], start: datetime, end: datetime
    ) -> datetime:
        """now を対象日の範囲内へ収める。

        過去日を指定したときに「翌日の現在時刻」で計算しないための保険。
        """
        at = timeutil.ensure_aware(now) if now is not None else timeutil.now()
        if at < start:
            return start
        last_minute = end - timedelta(minutes=1)
        return at if at <= last_minute else last_minute

    def _load_activities(self, start: datetime, end: datetime) -> list[Activity]:
        """確定層を読む。無ければ観測層へフォールバック。"""
        confirmed = self._activities.list_between(
            start, end, layer=ActivityLayer.CONFIRMED
        )
        if confirmed:
            return confirmed
        return self._activities.list_between(start, end, layer=ActivityLayer.OBSERVED)

    @staticmethod
    def _find_current(activities: list[Activity], at: datetime) -> Optional[Activity]:
        """now を含む activity。重なる場合は最後に始まったものを採る。"""
        found: Optional[Activity] = None
        for activity in activities:
            start_at = timeutil.ensure_aware(activity.start_at)
            end_at = timeutil.ensure_aware(activity.end_at)
            if start_at <= at < end_at:
                found = activity
        return found

    def _find_current_task(self, current_activity: Optional[Activity]) -> Optional[Task]:
        """current_activity.task に一致する Task、無ければ IN_PROGRESS の先頭。"""
        if current_activity is not None and current_activity.task:
            title = current_activity.task
            task = self._tasks.find_by_title(title, current_activity.project)
            if task is None:
                task = self._tasks.find_by_title(title, None)
            if task is None:
                # project 違いでも title が一致すれば採用する
                task = next((t for t in self._tasks.list() if t.title == title), None)
            if task is not None:
                return task
        in_progress = self._tasks.list(status=TaskStatus.IN_PROGRESS)
        return in_progress[0] if in_progress else None

    @staticmethod
    def _task_elapsed_min(activities: list[Activity], task: Optional[Task]) -> int:
        """同じ task に紐づく当日分 activity の合計分。"""
        if task is None:
            return 0
        total_sec = sum(a.duration_sec for a in activities if a.task == task.title)
        return max(0, int(round(total_sec / 60)))

    def _candidate_tasks(self, target_date: date) -> list[Task]:
        """未完了タスクを「期限が近い・優先度が高い・blocked でない」順で最大5件。"""
        unfinished = [t for t in self._tasks.list() if t.status in _UNFINISHED]
        unfinished.sort(
            key=lambda t: (
                _deadline_days(t, target_date, default=_NO_DEADLINE),
                t.priority,
                1 if t.blocked else 0,
                t.id or 0,
            )
        )
        return unfinished[:_MAX_CANDIDATES]

    def _read_constraints(self) -> list[str]:
        """長期コンテキストから制約を読む。読めなければ空リスト。"""
        try:
            from contextflow.sources.github_context import ContextRepo

            repo = ContextRepo(self._config.path("context_repo"), self._config)
            return list(repo.read_constraints())
        except Exception:
            # 制約が読めないだけで state 生成は止めない
            return []

    # ------------------------------------------------------------------
    # 過去の傾向・未来の先読み
    # ------------------------------------------------------------------

    def _build_past(self, target_date: date, target_start: datetime) -> PastSummary:
        """過去 past_days 日の傾向をまとめる。対象日は含まない。

        [target_date - past_days 日の 00:00, target_date の 00:00) を対象に、
        日ごとに確定→観測のフォールバックを適用してから合算する。
        日をまたいで一括で読むと、ある日は確定・別の日は観測という混在を
        正しく扱えないため。
        """
        past_days = int(self._config.get("context.past_days", 7))
        if past_days <= 0:
            return PastSummary(days=0)

        range_start = target_start - timedelta(days=past_days)
        range_end = target_start

        by_type: dict[str, int] = {}
        by_project: dict[str, int] = {}
        total_min = 0
        active_days = 0
        deep_work_min = 0
        context_switches = 0

        day_start = range_start
        while day_start < range_end:
            day_end = day_start + timedelta(days=1)
            activities = self._load_activities(day_start, day_end)
            if activities:
                active_days += 1

            day_summary = summarize_time(activities)
            total_min += day_summary.total_min
            for key, minutes in day_summary.by_type.items():
                by_type[key] = by_type.get(key, 0) + minutes
            for key, minutes in day_summary.by_project.items():
                by_project[key] = by_project.get(key, 0) + minutes

            sessions = self._sessions.list_between(day_start, day_end)
            features = compute_features(activities, sessions, self._config, now=day_end)
            deep_work_min += features.deep_work_min
            context_switches += features.context_switches

            day_start = day_end

        return PastSummary(
            days=past_days,
            start_date=range_start.date(),
            end_date=(range_end - timedelta(days=1)).date(),
            total_min=total_min,
            by_type=by_type,
            by_project=by_project,
            deep_work_min=deep_work_min,
            context_switches=context_switches,
            active_days=active_days,
            change_count=len(self._changes.list_between(range_start, range_end)),
            decision_count=len(self._decisions.list_between(range_start, range_end)),
        )

    def _build_upcoming(self, at: datetime) -> UpcomingSummary:
        """今後 upcoming_days 日の予定・締切をまとめる。

        [at, at + upcoming_days 日) が対象。at は _clamp_now が返す基準時刻で、
        過去日のスナップショットでもその時点から見た先になるようにする。
        予定は planned 層を直接読む（確定層のフォールバック規則に未来を
        混ぜると当日の集計が壊れるため）。
        """
        upcoming_days = int(self._config.get("context.upcoming_days", 7))
        if upcoming_days <= 0:
            return UpcomingSummary(days=0)

        range_start = at
        range_end = at + timedelta(days=upcoming_days)

        planned = self._activities.list_between(
            range_start, range_end, layer=ActivityLayer.PLANNED
        )
        items = [
            PlannedItem(
                start_at=a.start_at,
                end_at=a.end_at,
                activity_type=a.activity_type,
                summary=a.summary,
                project=a.project,
            )
            for a in planned
        ]

        by_type: dict[str, int] = {}
        planned_min = 0
        for item in items:
            planned_min += item.duration_min
            by_type[item.activity_type.value] = (
                by_type.get(item.activity_type.value, 0) + item.duration_min
            )

        deadlines = [
            t
            for t in self._tasks.list()
            if t.status in _UNFINISHED
            and t.deadline is not None
            and range_start <= timeutil.ensure_aware(datetime.combine(t.deadline, time.min))
            < range_end
        ]
        deadlines.sort(
            key=lambda t: (
                _deadline_days(t, at.date(), default=_NO_DEADLINE),
                t.priority,
                t.id or 0,
            )
        )

        return UpcomingSummary(
            days=upcoming_days,
            planned_min=planned_min,
            by_type=by_type,
            items=items,
            deadlines=deadlines,
        )

    # ------------------------------------------------------------------
    # 保存
    # ------------------------------------------------------------------

    def save_json(self, state: CurrentState, path: Optional[Path] = None) -> Path:
        """state を JSON で保存し、スナップショットも DB へ残す。"""
        target = Path(path) if path is not None else self._config.path("state_json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(to_jsonable(state), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._snapshots.add(state)
        return target


# ---------------------------------------------------------------------------
# Decision Engine へ渡す平坦 dict
# ---------------------------------------------------------------------------


def _deadline_days(task: Task, target_date: date, *, default: Any = None) -> Any:
    """締切までの日数。締切が無ければ default。"""
    if task.deadline is None:
        return default
    return (task.deadline - target_date).days


def _task_to_dict(task: Task, target_date: date) -> dict[str, Any]:
    """candidate_tasks 1件ぶんの平坦表現。"""
    return {
        "title": task.title,
        "project": task.project,
        "priority": task.priority,
        "deadline_days": _deadline_days(task, target_date),
        "blocked": bool(task.blocked),
        "remaining_steps": task.remaining_steps,
    }


def _minutes_ago(moment: datetime, reference: datetime) -> int:
    """reference から見て何分前かを返す。

    未来の時刻（手入力で後の時刻を指定した場合など）は負になる。
    0 へ丸めると「今」と区別できなくなるため、そのまま返す。
    """
    return int(round((reference - timeutil.ensure_aware(moment)).total_seconds() / 60))


def _change_to_dict(change: Change, reference: datetime) -> dict[str, Any]:
    """recent_changes 1件ぶんの平坦表現。

    案件名も渡す。他の項目（by_project / current_project / candidate_tasks）と同様で、
    これが無いと「その変化が今の案件のものか」を判断できないため。
    いつの変化かも渡す。`recent()` は日をまたいで直近N件を返すため、
    時刻が無いと古い変化をいつまでも「直近の変化」として扱ってしまう。
    """
    return {
        "description": change.description,
        "project": change.project,
        "ts": to_jsonable(change.ts),
        "minutes_ago": _minutes_ago(change.ts, reference),
    }


def _decision_to_dict(decision: Decision, reference: datetime) -> dict[str, Any]:
    """recent_decisions 1件ぶんの平坦表現。理由も判断材料になるので渡す。"""
    return {
        "decision": decision.decision,
        "reason": decision.reason,
        "project": decision.project,
        "ts": to_jsonable(decision.ts),
        "minutes_ago": _minutes_ago(decision.ts, reference),
    }


def _planned_item_to_dict(item: PlannedItem) -> dict[str, Any]:
    """upcoming_items 1件ぶんの平坦表現。件名も渡す（先読みの判断材料にするため）。"""
    return {
        "start": to_jsonable(item.start_at),
        "end": to_jsonable(item.end_at),
        "activity_type": item.activity_type.value,
        "summary": item.summary,
        "project": item.project,
        "duration_min": item.duration_min,
    }


def _deadline_to_dict(task: Task, target_date: date) -> dict[str, Any]:
    """upcoming_deadlines 1件ぶんの平坦表現。"""
    return {
        "title": task.title,
        "project": task.project,
        "deadline_days": _deadline_days(task, target_date),
        "priority": task.priority,
        "blocked": bool(task.blocked),
    }


def state_to_flat_dict(state: CurrentState) -> dict[str, Any]:
    """Decision Engine へ渡す唯一の入力。

    生ログ・ウィンドウタイトルは含めない。キー名は固定で、
    質問セット（next_action / task_triage）がこの名前を直接参照する。
    """
    activity = state.current_activity
    features = state.features
    candidates = state.candidate_tasks[:_MAX_CANDIDATES]
    # 経過時間の基準は state を作った時刻。now() を使うと同じ state でも値が動くため
    reference = timeutil.ensure_aware(state.generated_at)

    # task_* は current_task 優先、無ければ candidate_tasks の先頭から埋める
    focus_task = state.current_task or (candidates[0] if candidates else None)

    return {
        "time": state.now_time,
        "date": state.target_date.isoformat(),
        "today_total_min": state.today.total_min,
        "today_focus_min": focus_minutes(state.today),
        "by_type": dict(state.today.by_type),
        "by_project": dict(state.today.by_project),
        "current_activity_type": activity.activity_type.value if activity else None,
        "current_project": activity.project if activity else None,
        "current_task": state.current_task.title if state.current_task else None,
        "task_elapsed_min": state.task_elapsed_min,
        "open_tasks": state.open_tasks,
        "blocked_tasks": state.blocked_tasks,
        "recent_context_switches": features.context_switches,
        "deep_work_min": features.deep_work_min,
        "longest_focus_min": features.longest_focus_min,
        "active_min": features.active_min,
        "idle_min": features.idle_min,
        "last_break_min_ago": features.last_break_min_ago,
        "candidate_tasks": [_task_to_dict(t, state.target_date) for t in candidates],
        "recent_changes": [_change_to_dict(c, reference) for c in state.recent_changes],
        "recent_decisions": [_decision_to_dict(d, reference) for d in state.recent_decisions],
        "constraints": list(state.constraints),
        "task_blocked": bool(focus_task.blocked) if focus_task else False,
        "task_deadline_days": (
            _deadline_days(focus_task, state.target_date) if focus_task else None
        ),
        "task_priority": focus_task.priority if focus_task else 3,
        "task_remaining_steps": focus_task.remaining_steps if focus_task else 0,
        # --- 過去の傾向（recent_*）・未来の先読み（upcoming_*） ---------------
        # 既存の "recent_context_switches"（当日の切り替え回数）とは別物のため、
        # 同名衝突を避けて past_context_switches という名前にしている。
        "past_days": state.past.days,
        "past_total_min": state.past.total_min,
        "past_by_type": dict(state.past.by_type),
        "past_by_project": dict(state.past.by_project),
        "past_deep_work_min": state.past.deep_work_min,
        "past_context_switches": state.past.context_switches,
        "past_active_days": state.past.active_days,
        "past_change_count": state.past.change_count,
        "past_decision_count": state.past.decision_count,
        "upcoming_days": state.upcoming.days,
        "upcoming_planned_min": state.upcoming.planned_min,
        "upcoming_by_type": dict(state.upcoming.by_type),
        "upcoming_items": [_planned_item_to_dict(i) for i in state.upcoming.items],
        "upcoming_deadlines": [
            _deadline_to_dict(t, state.target_date) for t in state.upcoming.deadlines
        ],
    }
