"""planner/planner.py

Decision Engine が下した判断結果（noul/choice/score）を、人間向けの
説明・計画（日本語 Markdown）へ変換する。判断そのものは行わない
（続ける／どれ／何点、を決めるのは decision/ 側の役割）。

- make_plan          : LLM（運用方針の planner が指す提供元）で計画文を生成。使えなければオフライン文面。
- parse_activity_text : 自然文の作業報告を Activity（layer=REPORTED, source=MANUAL）へ構造化。
- render_offline_plan : LLM 無しで state と判断結果から Markdown を組み立てる。

LLM を1回呼び出す実処理（提供元ごとの分岐）は planner/llm_client.py に持たせる。
標準ライブラリのみを使用し、`anthropic` は llm_client.py 側で関数内遅延 import する。
互いの内部実装には依存せず、contracts/ config.py timeutil.py llm_client.py のみを import する。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from contextflow.config import AppConfig
from contextflow.contracts.decision import DecisionResponse
from contextflow.contracts.models import (
    Activity,
    ActivityLayer,
    ActivityType,
    CurrentState,
    Source,
)
from contextflow.planner import llm_client
from contextflow.timeutil import fmt_minutes, parse_hhmm, today

# ---------------------------------------------------------------------------
# system プロンプト・JSON Schema（モジュール定数）
# ---------------------------------------------------------------------------

# make_plan 用: 判断結果を覆さず、理由と手順を説明するだけの役割に限定する
_PLAN_SYSTEM_PROMPT = (
    "Planner役。Decision Engineが出した判断結果を、人間向けの説明・計画へ変換する。\n"
    "判断そのものは行わない。与えられた判断結果（value/confidence）を覆さず、\n"
    "その理由と次にやる手順の説明に徹する。\n"
    "\n"
    "出力ルール:\n"
    "- 敬語・丁寧語は使わない。体言止め・用言止めで簡潔に書く。\n"
    "- 箇条書き中心のMarkdownで出力する。\n"
    "- 見出しは短く（今日の状況／判断結果／次の一手 など）。\n"
    "- 入力のstateとdecisionsに無い情報は書かない。新しい判断を作らない。"
)

# parse_activity_text 用: 構造化のみ行う。判断や評価はしない
_PARSE_SYSTEM_PROMPT = (
    "自然文の作業報告を構造化データへ変換する役。\n"
    "時刻はHH:MM形式。開始時刻と所要時間・終了時刻の記述から、開始・終了の両方を計算して埋める。\n"
    "activity_typeは与えられた選択肢から最も近いものを1つ選ぶ。\n"
    "project・taskは文中から読み取れなければnullにする。summaryは短い日本語で要約する。"
)

# parse_activity_text の構造化出力 Schema（additionalProperties/required 必須）
_ACTIVITY_TEXT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "start": {"type": "string", "description": "開始時刻 HH:MM"},
        "end": {"type": "string", "description": "終了時刻 HH:MM"},
        "activity_type": {
            "type": "string",
            "enum": [item.value for item in ActivityType],
        },
        "project": {"type": ["string", "null"]},
        "task": {"type": ["string", "null"]},
        "summary": {"type": "string"},
    },
    "required": ["start", "end", "activity_type", "project", "task", "summary"],
    "additionalProperties": False,
}


def _minutes_ago(moment: datetime, reference: datetime) -> int:
    """reference から見て何分前か。未来の時刻なら負になる。"""
    return int(round((reference - moment).total_seconds() / 60))


def _safe_state_payload(state: CurrentState) -> dict[str, Any]:
    """LLMへ渡す state を、安全な項目だけの許可リストで組み立てる。

    to_jsonable(state) をそのまま渡すと current_activity.summary/detail
    （プロセス名・ウィンドウタイトルなどの生ログ）まで漏れてしまうため、
    渡してよい項目をここで明示的に列挙する（除外リストではなく許可リスト方式）。
    """
    activity = state.current_activity
    return {
        "date": state.target_date.isoformat(),
        "now_time": state.now_time,
        "today": {
            "total_min": state.today.total_min,
            "by_type": dict(state.today.by_type),
            "by_project": dict(state.today.by_project),
        },
        "features": {
            "deep_work_min": state.features.deep_work_min,
            "context_switches": state.features.context_switches,
            "longest_focus_min": state.features.longest_focus_min,
            "active_min": state.features.active_min,
            "idle_min": state.features.idle_min,
            "last_break_min_ago": state.features.last_break_min_ago,
        },
        "current_activity_type": activity.activity_type.value if activity else None,
        "current_project": activity.project if activity else None,
        "current_task": state.current_task.title if state.current_task else None,
        "task_elapsed_min": state.task_elapsed_min,
        "open_tasks": state.open_tasks,
        "blocked_tasks": state.blocked_tasks,
        "candidate_tasks": [
            {
                "title": task.title,
                "project": task.project,
                "priority": task.priority,
                "deadline": task.deadline.isoformat() if task.deadline else None,
                "blocked": bool(task.blocked),
            }
            for task in state.candidate_tasks
        ],
        # 案件名と日時も渡す。どの案件でいつ起きた変化・判断かが分からないと、
        # 案件をまたぐ状況や、古い話か直近の話かを説明できないため
        # （state_to_flat_dict と同じ形に揃える）
        "recent_changes": [
            {
                "description": change.description,
                "project": change.project,
                "ts": change.ts.isoformat(),
                "minutes_ago": _minutes_ago(change.ts, state.generated_at),
            }
            for change in state.recent_changes
        ],
        "recent_decisions": [
            {
                "decision": decision.decision,
                "reason": decision.reason,
                "project": decision.project,
                "ts": decision.ts.isoformat(),
                "minutes_ago": _minutes_ago(decision.ts, state.generated_at),
            }
            for decision in state.recent_decisions
        ],
        "constraints": list(state.constraints),
        # 過去の傾向。対象日（today/features）だけでは未来の判断材料が無いため渡す
        "past": {
            "days": state.past.days,
            "total_min": state.past.total_min,
            "by_type": dict(state.past.by_type),
            "by_project": dict(state.past.by_project),
            "deep_work_min": state.past.deep_work_min,
            "context_switches": state.past.context_switches,
            "active_days": state.past.active_days,
            "change_count": state.past.change_count,
            "decision_count": state.past.decision_count,
        },
        # 未来の先読み（予定・締切）。件名(summary)は利用者の明示の指示で渡す。
        # ただし [calendar] mask_subject のマスクは取り込み時に既に適用済み
        # （生のウィンドウタイトル・生ログではない）
        "upcoming": {
            "days": state.upcoming.days,
            "planned_min": state.upcoming.planned_min,
            "by_type": dict(state.upcoming.by_type),
            "items": [
                {
                    "start": item.start_at.isoformat(),
                    "end": item.end_at.isoformat(),
                    "activity_type": item.activity_type.value,
                    "summary": item.summary,
                    "project": item.project,
                    "duration_min": item.duration_min,
                }
                for item in state.upcoming.items
            ],
            "deadlines": [
                {
                    "title": task.title,
                    "project": task.project,
                    "deadline_days": (
                        (task.deadline - state.target_date).days if task.deadline else None
                    ),
                    "priority": task.priority,
                    "blocked": bool(task.blocked),
                }
                for task in state.upcoming.deadlines
            ],
        },
    }


class Planner:
    """判断結果を人間向けに翻訳する役。判断自体は decision/ 側に任せる。"""

    def __init__(self, config: AppConfig, mode: str | None = None) -> None:
        self._config = config
        self._mode_name = mode

    def _planner_provider(self) -> str | None:
        """運用方針（decision.mode）から Planner の提供元（llm_client.call_llm の provider）を取り出す。

        運用方針が解決できない、または planner="offline" のときは None
        （呼び出し側は None を「LLMを使わない」の合図として扱う）。
        """
        from contextflow.config import resolve_mode

        try:
            mode = resolve_mode(self._config, self._mode_name)
        except ValueError:
            return None
        return mode.planner if mode.uses_llm_planner else None

    def _uses_llm(self) -> bool:
        """運用方針（decision.mode）が Planner に LLM を使う設定かどうか。"""
        return self._planner_provider() is not None

    def make_plan(self, state: CurrentState, decisions: DecisionResponse) -> str:
        """判断結果を日本語の短い計画文（Markdown）にする。

        与える入力は _safe_state_payload(state)（安全な項目だけの許可リスト）と
        判断結果 {key: {value, confidence}} のみ（生ログは渡さない）。
        運用方針が planner="offline" のとき、および LLM が使えないときは
        render_offline_plan の結果を返す。
        """
        provider = self._planner_provider()
        if provider is None:
            return render_offline_plan(state, decisions)
        payload = {
            "state": _safe_state_payload(state),
            "decisions": {
                key: {"value": answer.value, "confidence": answer.confidence}
                for key, answer in decisions.answers.items()
            },
        }
        text = llm_client.call_llm(
            self._config,
            provider,
            system_prompt=_PLAN_SYSTEM_PROMPT,
            user_content=json.dumps(payload, ensure_ascii=False),
            model=llm_client.resolve_model(self._config, provider),
            max_tokens=int(self._config.get("llm.planner.max_tokens", 4000)),
            effort=str(self._config.get("llm.planner.effort", "high")),
        )
        return text if text else render_offline_plan(state, decisions)

    def parse_activity_text(self, text: str, base_date: date | None = None) -> Activity | None:
        """自然文の作業報告を Activity（layer=REPORTED, source=MANUAL）へ構造化する。

        例:「13時から45分、AI活用案件についてAさんと相談した」。
        時刻は base_date（既定は今日）を基準に解釈する。
        運用方針が planner="offline" のとき、および LLM が使えない／出力が不正な場合は
        None を返す（例外にしない）。
        """
        provider = self._planner_provider()
        if provider is None:
            return None
        raw = llm_client.call_llm(
            self._config,
            provider,
            system_prompt=_PARSE_SYSTEM_PROMPT,
            user_content=text,
            model=llm_client.resolve_model(self._config, provider),
            max_tokens=int(self._config.get("llm.planner.max_tokens", 4000)),
            effort=str(self._config.get("llm.planner.effort", "high")),
            json_schema=_ACTIVITY_TEXT_SCHEMA,
        )
        if not raw:
            return None
        try:
            data = json.loads(raw)
            base = base_date or today()
            start_at = parse_hhmm(str(data["start"]), base)
            end_at = parse_hhmm(str(data["end"]), base)
            activity_type = ActivityType(data["activity_type"])
            return Activity(
                start_at=start_at,
                end_at=end_at,
                activity_type=activity_type,
                layer=ActivityLayer.REPORTED,
                source=Source.MANUAL,
                project=data.get("project") or None,
                task=data.get("task") or None,
                summary=str(data.get("summary") or ""),
            )
        except Exception:
            # JSON不正・キー欠落・型不一致などは None（例外にしない）
            return None


def render_offline_plan(state: CurrentState, decisions: DecisionResponse) -> str:
    """LLM 無しで state と判断結果から日本語の要約 Markdown を組み立てる。

    今日の時間配分・現在タスク・判断結果・候補タスク上位3件・直近の変化と判断を並べる。
    """
    lines: list[str] = [f"# {state.target_date.isoformat()} の計画（オフライン生成）", ""]

    # 今日の時間配分
    lines.append("## 今日の時間配分")
    lines.append(f"- 合計: {fmt_minutes(state.today.total_min)}")
    if state.today.by_type:
        for activity_type, minutes in sorted(
            state.today.by_type.items(), key=lambda item: -item[1]
        ):
            lines.append(f"- {activity_type}: {fmt_minutes(minutes)}")
    else:
        lines.append("- 記録なし")
    lines.append("")

    # 現在タスク
    lines.append("## 現在タスク")
    if state.current_activity is not None:
        summary = state.current_activity.summary or "概要なし"
        lines.append(f"- 活動: {state.current_activity.activity_type.value}（{summary}）")
    else:
        lines.append("- 活動: 記録なし")
    if state.current_task is not None:
        elapsed = fmt_minutes(state.task_elapsed_min)
        lines.append(f"- タスク: {state.current_task.title}（経過 {elapsed}）")
    else:
        lines.append("- タスク: 未設定")
    lines.append("")

    # 判断結果
    lines.append("## 判断結果")
    if decisions.answers:
        for key, answer in decisions.answers.items():
            lines.append(f"- {key}: {answer.value}（確信度 {answer.confidence:.2f}）")
    else:
        lines.append("- 判断結果なし")
    lines.append("")

    # 候補タスク上位3件
    lines.append("## 候補タスク（上位3件）")
    top_tasks = state.candidate_tasks[:3]
    if top_tasks:
        for task in top_tasks:
            deadline = task.deadline.isoformat() if task.deadline else "期限なし"
            lines.append(f"- {task.title}（優先度 {task.priority} / {deadline}）")
    else:
        lines.append("- 候補なし")
    lines.append("")

    # 直近の変化・判断
    lines.append("## 直近の変化・判断")
    has_recent = False
    for change in state.recent_changes[:3]:
        lines.append(f"- 変化: {change.description}")
        has_recent = True
    for decision in state.recent_decisions[:3]:
        lines.append(f"- 判断: {decision.decision}")
        has_recent = True
    if not has_recent:
        lines.append("- 直近の記録なし")

    return "\n".join(lines)
