"""pipeline/p30_activity_builder.py

Session（PCログの観測区間）を Activity へ変換し、
予定（planned）・観測（observed）・自己申告（reported）の3層をマージして
確定（confirmed）の活動列を作る。

層を分ける理由は参照資料のとおり。
「カレンダーの予定 ＝ 実際にやったこと」ではないため、
予定はあくまで候補として扱い、実績（手入力・PCログ）が優先される。

優先度: reported(手入力) > observed(PCログ) > planned(カレンダー)
上位層が占有した時間帯を下位層から差し引き、残った断片だけを採用する。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Optional, Sequence

from contextflow.config import AppConfig
from contextflow.contracts.models import (
    Activity,
    ActivityLayer,
    ActivityType,
    Session,
    Source,
)
from contextflow.pipeline.p20_classifier import Classifier, load_classifier
from contextflow.storage.db import Database
from contextflow.storage.repositories import ActivityRepository, SessionRepository
from contextflow.timeutil import day_range, ensure_aware

__all__ = [
    "sessions_to_activities",
    "merge_layers",
    "build_day",
    "find_gaps",
]


# --- 既定値（config に同名キーがあれば上書きできる。無くても動く） ---

# マージ後にこの秒数未満になった断片は捨てる
DEFAULT_MIN_FRAGMENT_SEC = 60

# 予定しか無い時間帯に与える confidence の上限
DEFAULT_PLANNED_CONFIDENCE = 0.5

# 層ごとの優先度（先に書いたものが強い）
_LAYER_PRIORITY = (ActivityLayer.REPORTED, ActivityLayer.OBSERVED, ActivityLayer.PLANNED)


# ---------------------------------------------------------------------------
# 区間演算（素直に書く。区間は [start, end) の半開区間として扱う）
# ---------------------------------------------------------------------------

_Interval = tuple[datetime, datetime]


def _merge_intervals(intervals: Iterable[_Interval]) -> list[_Interval]:
    """重なり・隣接する区間を1本にまとめる。"""
    ordered = sorted((s, e) for s, e in intervals if e > s)
    merged: list[_Interval] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            # 直前の区間と重なる（または接する）ので伸ばす
            last_start, last_end = merged[-1]
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _subtract(base: _Interval, blocks: Sequence[_Interval]) -> list[_Interval]:
    """base から blocks（正規化済み）を差し引き、残った断片を返す。"""
    start, end = base
    remains: list[_Interval] = []
    cursor = start
    for block_start, block_end in blocks:
        if block_end <= cursor:
            continue          # まだ cursor より手前
        if block_start >= end:
            break             # base の外へ出た
        if block_start > cursor:
            remains.append((cursor, block_start))
        cursor = max(cursor, block_end)
        if cursor >= end:
            break
    if cursor < end:
        remains.append((cursor, end))
    return remains


def _duration_sec(interval: _Interval) -> int:
    return max(0, int((interval[1] - interval[0]).total_seconds()))


# ---------------------------------------------------------------------------
# session -> activity（観測層）
# ---------------------------------------------------------------------------


def _observed_confidence(activity_type: ActivityType, project: Optional[str]) -> float:
    """ルール一致の強さを 0.6〜0.9 で表す。

    - どのルールにも当たらず既定値へ落ちた   -> 0.6
    - activity_type がルールで決まった       -> 0.8
    - さらに project まで決まった            -> 0.9
    """
    if activity_type in (ActivityType.OTHER, ActivityType.UNKNOWN):
        return 0.6
    return 0.9 if project else 0.8


def _build_summary(session: Session) -> str:
    """プロセス名だけの簡潔な1行。

    ウィンドウタイトルには顧客名・メール件名・ファイル名など生ログが
    含まれ得る。summary は report/daily_markdown.py 経由で GitHub 向け
    Markdown へ出力されるため、ここには含めない
    （参照資料「2. GitHubには生ログを入れない」）。
    タイトルは detail["window_title"] にだけ残し、ローカル SQLite 止まりにする。
    """
    process = (session.process or "").strip()
    return process or "unknown"


def sessions_to_activities(
    sessions: Sequence[Session],
    classifier: Classifier,
    config: AppConfig,
) -> list[Activity]:
    """Session を観測層の Activity へ変換する（layer=OBSERVED / source=WINDOWS）。

    config は現状ここでは使わないが、コントラクトどおり引数として受け取る。
    """
    activities: list[Activity] = []
    for session in sessions:
        start = ensure_aware(session.start_at)
        end = ensure_aware(session.end_at)
        if end <= start:
            continue  # 壊れた区間は落とす（型崩れ対策）

        activity_type, project = classifier.classify(session.process, session.window_title)
        activities.append(
            Activity(
                start_at=start,
                end_at=end,
                activity_type=activity_type,
                layer=ActivityLayer.OBSERVED,
                source=Source.WINDOWS,
                project=project,
                task=None,
                confidence=_observed_confidence(activity_type, project),
                summary=_build_summary(session),
                detail={
                    "process": session.process,
                    "window_title": session.window_title,
                    "sample_count": session.sample_count,
                },
            )
        )

    activities.sort(key=lambda a: a.start_at)
    return activities


# ---------------------------------------------------------------------------
# 3層マージ（このモジュールの中心）
# ---------------------------------------------------------------------------


def _confirmed_from(
    origin: Activity,
    fragment: _Interval,
    *,
    confidence: float,
) -> Activity:
    """採用元レコードの内容を引き継いだ確定 Activity を作る。"""
    start, end = fragment
    detail: dict[str, Any] = dict(origin.detail or {})
    # どの層から採用したかを必ず残す（後から差分を見るため）
    detail["from_layer"] = origin.layer.value
    if start != ensure_aware(origin.start_at) or end != ensure_aware(origin.end_at):
        # 上位層に食われて切り取られた断片であることを記録する
        detail["clipped"] = True
    return Activity(
        start_at=start,
        end_at=end,
        activity_type=origin.activity_type,
        layer=ActivityLayer.CONFIRMED,
        source=origin.source,
        project=origin.project,
        task=origin.task,
        confidence=confidence,
        summary=origin.summary,
        detail=detail,
    )


def merge_layers(
    planned: Sequence[Activity],
    observed: Sequence[Activity],
    reported: Sequence[Activity],
    config: AppConfig,
) -> list[Activity]:
    """3層をマージして確定 Activity（layer=CONFIRMED）を作る。

    手順:
      1. reported -> observed -> planned の順に見る
      2. すでに確定済みの時間帯を差し引き、残った断片だけ採用する
      3. 断片が min_fragment_sec 未満なら捨てる

    例: 予定 13:00-14:00 会議 / 観測 13:40-14:00 PowerPoint / 手入力 13:00-13:40 会議
        -> 13:00-13:40 meeting(manual) と 13:40-14:00 document(windows)
    """
    min_fragment_sec = int(config.get("activity.min_fragment_sec", DEFAULT_MIN_FRAGMENT_SEC))
    planned_confidence = float(
        config.get("activity.planned_confidence", DEFAULT_PLANNED_CONFIDENCE)
    )

    by_layer = {
        ActivityLayer.REPORTED: list(reported or []),
        ActivityLayer.OBSERVED: list(observed or []),
        ActivityLayer.PLANNED: list(planned or []),
    }

    confirmed: list[Activity] = []
    occupied: list[_Interval] = []   # すでに確定した時間帯（正規化済み）

    for layer in _LAYER_PRIORITY:
        # 同じ層の中は開始時刻順。先に来たレコードが勝つ（層内の重なりもここで解消する）
        records = sorted(by_layer[layer], key=lambda a: ensure_aware(a.start_at))
        for origin in records:
            start = ensure_aware(origin.start_at)
            end = ensure_aware(origin.end_at)
            if end <= start:
                continue  # 壊れた区間は無視

            for fragment in _subtract((start, end), occupied):
                if _duration_sec(fragment) < min_fragment_sec:
                    # 上位層に削られて短くなりすぎた断片は雑音になるので捨てる
                    continue

                if layer is ActivityLayer.PLANNED:
                    # カレンダー＝実績ではない。予定しか無い時間帯は
                    # 「その予定どおり動いた可能性が高い」程度の扱いにして
                    # confidence を下げ、後段（gap_fill 質問）で確認できるようにする。
                    confidence = min(origin.confidence, planned_confidence)
                else:
                    confidence = origin.confidence

                confirmed.append(_confirmed_from(origin, fragment, confidence=confidence))
                occupied = _merge_intervals(occupied + [fragment])

    confirmed.sort(key=lambda a: a.start_at)
    return confirmed


# ---------------------------------------------------------------------------
# 1日ぶんの組み立て
# ---------------------------------------------------------------------------


def _load_day_classifier(config: AppConfig) -> Classifier:
    """config の置き場所にある categories.toml を優先して Classifier を作る。"""
    candidate = config.sibling("categories.toml")
    return load_classifier(candidate if candidate.exists() else None)


def build_day(db: Database, target: date, config: AppConfig) -> list[Activity]:
    """対象日の observed / confirmed を作り直して保存し、確定リストを返す。"""
    start, end = day_range(target)

    # 1. PCログ（session）から観測層を作る
    sessions = SessionRepository(db).list_between(start, end)
    classifier = _load_day_classifier(config)
    observed = sessions_to_activities(sessions, classifier, config)

    # 2. 予定・自己申告は DB に入っているものを読む（作るのは別モジュールの役目）
    activity_repo = ActivityRepository(db)
    planned = activity_repo.list_between(start, end, layer=ActivityLayer.PLANNED)
    reported = activity_repo.list_between(start, end, layer=ActivityLayer.REPORTED)

    # 3. マージして確定を作る
    confirmed = merge_layers(planned, observed, reported, config)

    # 4. 保存（その日のぶんは毎回作り直す）
    activity_repo.replace_between(start, end, observed, layer=ActivityLayer.OBSERVED)
    activity_repo.replace_between(start, end, confirmed, layer=ActivityLayer.CONFIRMED)

    return confirmed


# ---------------------------------------------------------------------------
# 空白時間の抽出
# ---------------------------------------------------------------------------


def find_gaps(
    activities: Sequence[Activity],
    start: datetime,
    end: datetime,
    min_gap_sec: int = 600,
) -> list[_Interval]:
    """確定 Activity の隙間のうち min_gap_sec 以上のものを返す。

    PC 操作が無い時間を「休憩」と断定しないための入力。
    Decision Engine の gap_fill 質問セットへ渡して、
    会議だったのか・考えていたのか・離席だったのかを確認する。
    """
    window_start = ensure_aware(start)
    window_end = ensure_aware(end)
    if window_end <= window_start:
        return []

    # 対象範囲でクリップしてから重なりをまとめる
    covered: list[_Interval] = []
    for activity in activities:
        a_start = max(window_start, ensure_aware(activity.start_at))
        a_end = min(window_end, ensure_aware(activity.end_at))
        if a_end > a_start:
            covered.append((a_start, a_end))
    covered = _merge_intervals(covered)

    gaps = [
        gap for gap in _subtract((window_start, window_end), covered)
        if _duration_sec(gap) >= min_gap_sec
    ]
    return gaps
