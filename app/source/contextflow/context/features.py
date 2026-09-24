"""特徴量の算出。

観測（activity）を、そのまま判断系へ渡さずに「特徴量」へ落とす層。
    観測 → 特徴量 → 状態 → 判断
の2段目にあたる。ここでは生ログ・ウィンドウタイトルを一切扱わない。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from contextflow.config import AppConfig
from contextflow.contracts.models import (
    Activity,
    ActivityType,
    Session,
    TimeSummary,
    WorkFeatures,
)
from contextflow.timeutil import ensure_aware, now as now_fn

# project が未設定の activity をまとめる先
UNASSIGNED = "unassigned"

# 「集中作業」とみなす活動種別（today_focus_min の対象）
FOCUS_TYPES: tuple[ActivityType, ...] = (
    ActivityType.CODING,
    ActivityType.RESEARCH,
    ActivityType.DOCUMENT,
    ActivityType.THINKING,
    ActivityType.REVIEW,
    ActivityType.PLANNING,
)


# ---------------------------------------------------------------------------
# 内部ヘルパ
# ---------------------------------------------------------------------------


def _sec_to_min(total_sec: float) -> int:
    """秒を分へ（四捨五入・負にしない）。"""
    return max(0, int(round(total_sec / 60)))


def _project_key(activity: Activity) -> str:
    """project が None の場合は 'unassigned' に寄せる。"""
    return activity.project or UNASSIGNED


def _sorted_activities(activities: Sequence[Activity]) -> list[Activity]:
    """開始時刻順に並べ、長さ0以下のものは捨てる。"""
    valid = [a for a in activities if a.duration_sec > 0]
    return sorted(valid, key=lambda a: (ensure_aware(a.start_at), ensure_aware(a.end_at)))


# ---------------------------------------------------------------------------
# 時間集計
# ---------------------------------------------------------------------------


def summarize_time(activities: Sequence[Activity]) -> TimeSummary:
    """activity_type 別・project 別の合計分と総合計を返す。

    端数の丸めは「秒で合算してから分へ変換」する。
    分に直してから足すより誤差が小さい。
    """
    by_type_sec: dict[str, int] = {}
    by_project_sec: dict[str, int] = {}
    total_sec = 0

    for activity in activities:
        duration = activity.duration_sec
        if duration <= 0:
            continue
        type_key = activity.activity_type.value
        by_type_sec[type_key] = by_type_sec.get(type_key, 0) + duration
        project_key = _project_key(activity)
        by_project_sec[project_key] = by_project_sec.get(project_key, 0) + duration
        total_sec += duration

    return TimeSummary(
        total_min=_sec_to_min(total_sec),
        by_type={k: _sec_to_min(v) for k, v in by_type_sec.items()},
        by_project={k: _sec_to_min(v) for k, v in by_project_sec.items()},
    )


def focus_minutes(summary: TimeSummary) -> int:
    """TimeSummary から集中作業の合計分を取り出す。"""
    return sum(summary.by_type.get(t.value, 0) for t in FOCUS_TYPES)


# ---------------------------------------------------------------------------
# 特徴量
# ---------------------------------------------------------------------------


def compute_features(
    activities: Sequence[Activity],
    sessions: Sequence[Session],
    config: AppConfig,
    *,
    now: Optional[datetime] = None,
) -> WorkFeatures:
    """判断に使う特徴量をまとめて算出する。

    sessions は現状ここでは使わないが、将来 idle の精緻化に使うため
    コントラクトどおり引数として受け取る。
    """
    at = ensure_aware(now) if now is not None else now_fn()
    deep_work_sec_threshold = int(config.get("features.deep_work_min_sec", 1500))
    switch_sec_threshold = int(config.get("features.context_switch_min_sec", 60))

    ordered = _sorted_activities(activities)
    if not ordered:
        # データが1件も無い日でも例外を出さず、空の特徴量を返す
        return WorkFeatures()

    return WorkFeatures(
        deep_work_min=_deep_work_min(ordered, deep_work_sec_threshold),
        context_switches=_context_switches(ordered, switch_sec_threshold),
        longest_focus_min=_longest_focus_min(ordered),
        active_min=_active_min(ordered),
        idle_min=_idle_min(ordered, at),
        last_break_min_ago=_last_break_min_ago(ordered, at),
    )


def _deep_work_min(ordered: Sequence[Activity], threshold_sec: int) -> int:
    """閾値以上続いた単一 activity（集中作業種別=FOCUS_TYPES のみ）の合計分。

    「break 以外すべて」を対象にすると、会議（meeting）だけの日でも
    deep work が積み上がってしまう。deep work は coding / research /
    document / thinking / review / planning のみを対象にする。
    """
    total_sec = sum(
        a.duration_sec
        for a in ordered
        if a.activity_type in FOCUS_TYPES and a.duration_sec >= threshold_sec
    )
    return _sec_to_min(total_sec)


def _context_switches(ordered: Sequence[Activity], min_sec: int) -> int:
    """project または activity_type が変わった回数。

    min_sec 未満の短い滞在は「切り替え」と数えないため、先に除外する。
    """
    kept = [a for a in ordered if a.duration_sec >= min_sec]
    switches = 0
    for prev, curr in zip(kept, kept[1:]):
        if (
            _project_key(prev) != _project_key(curr)
            or prev.activity_type is not curr.activity_type
        ):
            switches += 1
    return switches


def _longest_focus_min(ordered: Sequence[Activity]) -> int:
    """最長の連続作業（break を挟まない同一 project の連結）の分。"""
    longest_sec = 0
    run_sec = 0
    run_project: Optional[str] = None

    for activity in ordered:
        if activity.activity_type is ActivityType.BREAK:
            # break は連続を切る
            run_sec = 0
            run_project = None
            continue
        project = _project_key(activity)
        if project == run_project:
            run_sec += activity.duration_sec
        else:
            run_sec = activity.duration_sec
            run_project = project
        longest_sec = max(longest_sec, run_sec)

    return _sec_to_min(longest_sec)


def _active_min(ordered: Sequence[Activity]) -> int:
    """activity の合計分。"""
    return _sec_to_min(sum(a.duration_sec for a in ordered))


def _idle_min(ordered: Sequence[Activity], at: datetime) -> int:
    """その日の経過時間と active との差。

    経過時間は「最初の activity の開始 〜 now」で測る。
    0:00 起点にすると就寝時間まで idle に入ってしまい、判断材料にならないため。
    now が最後の activity より前でも負にしないよう、終端は両者の大きい方を使う。
    """
    first_start = ensure_aware(ordered[0].start_at)
    last_end = max(ensure_aware(a.end_at) for a in ordered)
    end_ref = max(at, last_end)
    elapsed_min = _sec_to_min((end_ref - first_start).total_seconds())
    return max(0, elapsed_min - _active_min(ordered))


def _last_break_min_ago(ordered: Sequence[Activity], at: datetime) -> Optional[int]:
    """直近の break 活動の終了から now までの分。break が無ければ None。"""
    breaks = [a for a in ordered if a.activity_type is ActivityType.BREAK]
    if not breaks:
        return None
    last_end = max(ensure_aware(a.end_at) for a in breaks)
    return _sec_to_min(max(0.0, (at - last_end).total_seconds()))
