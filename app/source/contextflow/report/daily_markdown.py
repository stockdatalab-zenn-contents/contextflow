"""report/daily_markdown.py

CurrentState と当日の Activity / Change / Decision から、
日次サマリの Markdown（activity/daily/YYYY-MM-DD.md 形式）を組み立てる。
出力形式は検討資料の Markdown 例（Time / Main activities /
Context switches / Changes / Decisions）に合わせ、
情報源が分かるよう Sources セクションを追加する。
標準ライブラリのみ使用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from contextflow.config import AppConfig
from contextflow.contracts.models import Activity, Change, CurrentState, Decision, Source
from contextflow.timeutil import fmt_minutes

# Main activities に載せる上位件数
_TOP_ACTIVITY_COUNT = 5


def render_daily(
    state: CurrentState,
    activities: Sequence[Activity],
    changes: Sequence[Change],
    decisions: Sequence[Decision],
) -> str:
    """CurrentState と当日実績から日次サマリ Markdown 本文を組み立てる。"""
    lines: list[str] = [f"# {state.target_date.isoformat()}", ""]

    lines.append("## Time")
    lines.extend(_render_time_section(state))
    lines.append("")

    lines.append("## Main activities")
    lines.extend(_render_main_activities(activities))
    lines.append("")

    lines.append("## Context switches")
    lines.append(str(state.features.context_switches))
    lines.append("")

    lines.append("## Changes")
    lines.extend(_render_changes(changes))
    lines.append("")

    lines.append("## Decisions")
    lines.extend(_render_decisions(decisions))
    lines.append("")

    # PC作業を過大評価しないよう、どこから得た情報かを残す
    lines.append("## Sources")
    lines.extend(_render_sources(activities))

    return "\n".join(lines) + "\n"


def write_daily(
    config: AppConfig,
    state: CurrentState,
    activities: Sequence[Activity],
    changes: Sequence[Change],
    decisions: Sequence[Decision],
) -> Path:
    """render_daily の結果を export_dir/activity/daily/YYYY-MM-DD.md へ書き出す。"""
    text = render_daily(state, activities, changes, decisions)
    target_dir = config.path("export_dir") / "activity" / "daily"
    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = target_dir / f"{state.target_date.isoformat()}.md"
    # UTF-8 / 改行 LF で書き出す
    with open(file_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return file_path


# ---------------------------------------------------------------------------
# 内部ヘルパ
# ---------------------------------------------------------------------------


def _render_time_section(state: CurrentState) -> list[str]:
    # activity_type ごとの合計時間（分）を長い順に並べる
    by_type = state.today.by_type
    if not by_type:
        return ["- なし"]
    ordered = sorted(by_type.items(), key=lambda item: item[1], reverse=True)
    return [f"- {_label(activity_type)}: {fmt_minutes(minutes)}" for activity_type, minutes in ordered]


def _render_main_activities(activities: Sequence[Activity]) -> list[str]:
    # 継続時間が長い順に上位 _TOP_ACTIVITY_COUNT 件を表示
    ordered = sorted(activities, key=lambda activity: activity.duration_min, reverse=True)
    top = ordered[:_TOP_ACTIVITY_COUNT]
    if not top:
        return ["- なし"]
    return [f"- {_activity_line(activity)}" for activity in top]


def _activity_line(activity: Activity) -> str:
    # activity_type / project / task に加え、本人が入力した内容と分かる
    # source（manual / calendar）のときだけ summary を出す。
    # windows 由来の summary にはプロセス名・ウィンドウタイトルが混じり得るため、
    # app/cf.py github export で長期コンテキストへ流出しないよう出力に使わない。
    # detail（raw_events 相当）は情報が濃く、source を問わず絶対に出力しない。
    parts = [_label(activity.activity_type.value)]
    if activity.project:
        parts.append(activity.project)
    if activity.task:
        parts.append(activity.task)
    if activity.summary and activity.source in (Source.MANUAL, Source.CALENDAR):
        parts.append(activity.summary)
    return " / ".join(parts)


def _render_changes(changes: Sequence[Change]) -> list[str]:
    if not changes:
        return ["- なし"]
    return [f"- {change.description}" for change in changes]


def _render_decisions(decisions: Sequence[Decision]) -> list[str]:
    if not decisions:
        return ["- なし"]
    lines: list[str] = []
    for decision in decisions:
        if decision.reason:
            lines.append(f"- {decision.decision}（{decision.reason}）")
        else:
            lines.append(f"- {decision.decision}")
    return lines


def _render_sources(activities: Sequence[Activity]) -> list[str]:
    # source（windows / manual / calendar 等）ごとの合計時間。
    # activity ごとに分へ丸めてから合計すると Time セクション（秒で合算）と
    # 数分ずれるため、ここも秒で合算してから最後に分へ丸める。
    totals_sec: dict[str, int] = {}
    for activity in activities:
        key = activity.source.value
        totals_sec[key] = totals_sec.get(key, 0) + activity.duration_sec
    if not totals_sec:
        return ["- なし"]
    ordered = sorted(totals_sec.items(), key=lambda item: item[1], reverse=True)
    return [f"- {_label(source)}: {fmt_minutes(int(round(seconds / 60)))}" for source, seconds in ordered]


def _label(value: str) -> str:
    # 'coding' -> 'Coding' のように先頭だけ大文字化する
    return value[:1].upper() + value[1:] if value else value
