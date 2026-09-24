"""LLM もネットワークも使わないルールベースの既定 Decision Engine。

state の平坦 dict（Context Builder が作る特徴量）を素朴なルールで判定する。
未知の質問 key が来ても型に応じた既定値を必ず返す。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from contextflow.config import AppConfig
from contextflow.contracts.decision import (
    Answer,
    AnswerValue,
    DecisionEngine,
    DecisionRequest,
    DecisionResponse,
    Question,
    QuestionType,
)

# ルール1件の戻り値: (値, raw_confidence, rationale)
_RuleResult = tuple[AnswerValue, float, str]


def _num(value: Any, default: float = 0.0) -> float:
    """state の値を素直に float へ寄せる。変換できなければ default。"""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# これより古い変化は「直近の変化」として扱わない（24時間）。
# recent_changes は日をまたいで直近N件を返すため、新しさで絞らないと古い話が残り続ける
_RECENT_CHANGE_MIN = 24 * 60

# --- 未来向け（week_outlook / deadline_risk）の定数 ------------------------
# 可処分時間に対して予定がこの割合を超えたら「逼迫」とみなす（分かりやすさ優先で固定割合）
_CAPACITY_TIGHT_RATIO = 0.8
# 締切がこの日数以内なら「近い」とみなす
_DEADLINE_NEAR_DAYS = 3
# 過去N日の当該案件への投下時間がこれ未満なら「ほとんど割けていない」とみなす
_LOW_PROJECT_MIN = 30


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    """dict のリストとして安全に取り出す。形が違うときは空リスト。

    state は外部（Context Builder）から来るため、想定外の形でも落ちないようにする。
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _nearest_deadline(state: dict[str, Any]) -> Optional[dict[str, Any]]:
    """upcoming_deadlines の先頭（最も近い締切）。無ければ None。

    state_to_flat_dict 側で deadline_days 昇順に整列済みのため、先頭が最も近い。
    """
    deadlines = _as_dict_list(state.get("upcoming_deadlines"))
    return deadlines[0] if deadlines else None


class RuleBasedEngine(DecisionEngine):
    """既定の Decision Engine。ネットワークに一切依存しない。"""

    name = "rule_based"

    def __init__(self, calibrator=None, config: Optional[AppConfig] = None) -> None:
        super().__init__(calibrator=calibrator)
        self.config = config
        # 質問 key ごとの判定関数。1関数1問い。
        self._handlers: dict[str, Callable[[dict[str, Any], Question], _RuleResult]] = {
            "continue_current_task": self._continue_current_task,
            "next_task_type": self._next_task_type,
            "urgency": self._urgency,
            "is_blocked": self._is_blocked,
            "do_today": self._do_today,
            "priority": self._priority,
            "gap_activity_type": self._gap_activity_type,
            "gap_is_work": self._gap_is_work,
            "capacity_is_tight": self._capacity_is_tight,
            "focus_project": self._focus_project,
            "week_risk": self._week_risk,
            "deadline_at_risk": self._deadline_at_risk,
            "needs_reschedule": self._needs_reschedule,
            "deadline_pressure": self._deadline_pressure,
        }

    # ------------------------------------------------------------------
    # DecisionEngine 実装
    # ------------------------------------------------------------------

    def _ask(self, request: DecisionRequest) -> DecisionResponse:
        answers: dict[str, Answer] = {}
        for question in request.questions:
            handler = self._handlers.get(question.key)
            if handler is not None:
                value, raw_confidence, rationale = handler(request.state, question)
            else:
                value, raw_confidence, rationale = self._default_answer(question)
            answers[question.key] = Answer(
                key=question.key,
                value=value,
                raw_confidence=raw_confidence,
                engine=self.name,
                rationale=rationale,
            )
        return DecisionResponse(answers=answers, engine=self.name)

    def _default_answer(self, question: Question) -> _RuleResult:
        """未知の key 用の既定値。型に応じて素直な値を返す。"""
        if question.type == QuestionType.NOUL:
            return False, 0.3, "未知の質問のため既定値(False)を返す"
        if question.type == QuestionType.CHOICE:
            choice = question.choices[0] if question.choices else ""
            return choice, 0.3, "未知の質問のため先頭の選択肢を返す"
        median = int(round((question.min + question.max) / 2))
        return median, 0.3, "未知の質問のため中央値を返す"

    # ------------------------------------------------------------------
    # 個別ルール（next_action）
    # ------------------------------------------------------------------

    def _compute_continue(self, state: dict[str, Any]) -> _RuleResult:
        """continue_current_task の判定本体。next_task_type からも再利用する。"""
        elapsed = _num(state.get("task_elapsed_min"))
        last_break = state.get("last_break_min_ago")
        switches = _num(state.get("recent_context_switches"))

        if elapsed >= 90:
            return False, 0.8, "現在タスクの経過が90分以上のため中断を提案"
        if last_break is not None and _num(last_break) >= 120:
            return False, 0.7, "前回休憩から120分以上経過のため中断を提案"
        if switches <= 3:
            confidence = 0.85 if switches <= 1 else 0.65
            return True, confidence, "切り替えが少なく集中が続いているため継続と判断"
        return True, 0.5, "明確な中断要因が無いため継続を仮判定"

    def _continue_current_task(self, state: dict[str, Any], question: Question) -> _RuleResult:
        return self._compute_continue(state)

    def _next_task_type(self, state: dict[str, Any], question: Question) -> _RuleResult:
        continue_flag, _confidence, _rationale = self._compute_continue(state)
        blocked_tasks = _num(state.get("blocked_tasks"))
        current_task = state.get("current_task")
        last_break = state.get("last_break_min_ago")
        # 休憩が長時間無い（=最後の休憩から時間が経っている）かどうか
        break_overdue = last_break is None or _num(last_break) >= 60

        if not continue_flag and break_overdue:
            return "break", 0.7, "現在タスクを継続すべきでなく休憩も長時間無いため休憩を提案"
        if blocked_tasks > 0 and not current_task:
            return "quick_task", 0.6, "現在タスクが無くブロック中タスクがあるため軽作業を提案"
        return "continue", 0.55, "中断・軽作業の要因が無いため継続を提案"

    def _urgency(self, state: dict[str, Any], question: Question) -> _RuleResult:
        open_tasks = _num(state.get("open_tasks"))
        blocked_tasks = _num(state.get("blocked_tasks"))
        deadline_days = state.get("task_deadline_days")

        score = 2
        reasons: list[str] = []
        if deadline_days is not None:
            days = _num(deadline_days, default=999)
            if days <= 1:
                score = max(score, 5)
                reasons.append("締切が1日以内")
            elif days <= 3:
                score = max(score, 4)
                reasons.append("締切が3日以内")
        if blocked_tasks >= 3:
            score = max(score, 4)
            reasons.append("ブロック中タスクが3件以上")
        elif blocked_tasks >= 1:
            score = max(score, 3)
            reasons.append("ブロック中タスクあり")
        if open_tasks >= 10:
            score = max(score, 3)
            reasons.append("未完了タスクが10件以上")

        # 今の案件で「最近」起きた変化は緊急度を押し上げる。
        # recent_changes は案件名と minutes_ago を持つので、案件と新しさの両方で絞る。
        # 新しさを見ないと、何日も前の変化がいつまでも緊急度を上げ続けてしまう。
        current_project = state.get("current_project")
        if current_project:
            fresh = [
                change
                for change in _as_dict_list(state.get("recent_changes"))
                if change.get("project") == current_project
                and _num(change.get("minutes_ago"), default=_RECENT_CHANGE_MIN + 1)
                <= _RECENT_CHANGE_MIN
            ]
            if fresh:
                score = max(score, 4)
                reasons.append(f"現在の案件（{current_project}）で24時間以内に変化あり")

        score = max(question.min, min(question.max, score))
        rationale = "、".join(reasons) if reasons else "顕著な緊急要因が無いため低めのスコア"
        return score, 0.5, rationale

    # ------------------------------------------------------------------
    # 個別ルール（task_triage）
    # ------------------------------------------------------------------

    def _is_blocked(self, state: dict[str, Any], question: Question) -> _RuleResult:
        blocked = state.get("task_blocked")
        if blocked is None:
            return False, 0.3, "タスクのブロック情報が無いため既定でFalse"
        return bool(blocked), 0.8, "タスクのブロックフラグに従って判定"

    def _do_today(self, state: dict[str, Any], question: Question) -> _RuleResult:
        deadline_days = state.get("task_deadline_days")
        priority = state.get("task_priority")

        if deadline_days is not None and _num(deadline_days, default=999) <= 1:
            return True, 0.8, "締切が1日以内のため今日実施と判断"
        if priority is not None and _num(priority, default=5) <= 2:
            return True, 0.6, "優先度が高いため今日実施と判断"
        return False, 0.4, "締切・優先度とも緊急でないため今日実施は見送り"

    def _priority(self, state: dict[str, Any], question: Question) -> _RuleResult:
        priority = state.get("task_priority")
        deadline_days = state.get("task_deadline_days")

        if priority is not None:
            score = _num(priority, default=3)
            rationale = "タスクに設定された優先度をそのまま採用"
        else:
            score = 3
            rationale = "優先度情報が無いため中央値を採用"

        if deadline_days is not None and _num(deadline_days, default=999) <= 2:
            score = min(score, 2)
            rationale += "、締切が近いため繰り上げ"

        score = max(question.min, min(question.max, int(round(score))))
        return score, 0.5, rationale

    # ------------------------------------------------------------------
    # 個別ルール（gap_fill）
    # ------------------------------------------------------------------

    def _gap_activity_type(self, state: dict[str, Any], question: Question) -> _RuleResult:
        has_event = state.get("gap_has_calendar_event")
        if has_event:
            return "meeting", 0.75, "カレンダー予定があるため会議と推定"
        return "unknown", 0.3, "根拠が無くPC idleだけでは断定できないため未確定"

    def _gap_is_work(self, state: dict[str, Any], question: Question) -> _RuleResult:
        has_event = state.get("gap_has_calendar_event")
        if has_event:
            return True, 0.75, "カレンダー予定があるため仕事の空白時間と判断"
        return False, 0.3, "根拠が無くPC idleだけでは休憩とも断定しないためFalse"

    # ------------------------------------------------------------------
    # 個別ルール（week_outlook / deadline_risk）
    #
    # 未来の判断は答え合わせが遅れて校正が効きにくいため、
    # 材料（過去実績・締切情報）が無いときは断定せず、値をNoneや中央値にし、
    # confidenceを低く保つ方針にしている。
    # ------------------------------------------------------------------

    def _compute_capacity_is_tight(self, state: dict[str, Any]) -> _RuleResult:
        """capacity_is_tight の判定本体。needs_reschedule からも再利用する。"""
        upcoming_days = _num(state.get("upcoming_days"))
        upcoming_planned_min = _num(state.get("upcoming_planned_min"))
        past_total_min = _num(state.get("past_total_min"))
        past_active_days = _num(state.get("past_active_days"))

        # 過去の実績が無い、または今後の対象期間が無いと可処分時間を見積もれない
        if upcoming_days <= 0 or past_active_days <= 0 or past_total_min <= 0:
            return (
                None,
                0.3,
                "過去の実績、または今後の予定期間が無く可処分時間を見積もれないため不明",
            )

        daily_capacity = past_total_min / past_active_days
        threshold = upcoming_days * daily_capacity * _CAPACITY_TIGHT_RATIO
        if upcoming_planned_min > threshold:
            return (
                True,
                0.7,
                f"今後{int(upcoming_days)}日の予定{int(upcoming_planned_min)}分が、"
                f"過去実績ベースの可処分時間の目安{int(threshold)}分を超えているため逼迫と判断",
            )
        return (
            False,
            0.6,
            f"今後{int(upcoming_days)}日の予定{int(upcoming_planned_min)}分は、"
            f"過去実績ベースの可処分時間の目安{int(threshold)}分の範囲内のため余裕ありと判断",
        )

    def _capacity_is_tight(self, state: dict[str, Any], question: Question) -> _RuleResult:
        return self._compute_capacity_is_tight(state)

    def _focus_project(self, state: dict[str, Any], question: Question) -> _RuleResult:
        nearest = _nearest_deadline(state)
        if nearest is not None:
            project = nearest.get("project") or "不明"
            return "deadline", 0.65, f"直近の締切がある案件（{project}）を優先すべきと判断"

        items = _as_dict_list(state.get("upcoming_items"))
        upcoming_by_type = state.get("upcoming_by_type")
        has_upcoming = bool(items) or (isinstance(upcoming_by_type, dict) and upcoming_by_type)
        if has_upcoming and _num(state.get("upcoming_planned_min")) > 0:
            return (
                "planned",
                0.5,
                "締切は無いが今後の予定が入っているため、予定が多い案件を優先すべきと判断",
            )

        return "current", 0.4, "締切も今後の目立った予定も無いため現在の案件を継続と判断"

    def _week_risk(self, state: dict[str, Any], question: Question) -> _RuleResult:
        score = 2
        reasons: list[str] = []
        have_data = False

        nearest = _nearest_deadline(state)
        if nearest is not None and nearest.get("deadline_days") is not None:
            have_data = True
            days = _num(nearest.get("deadline_days"), default=999)
            if days <= 1:
                score = max(score, 5)
                reasons.append("最も近い締切が1日以内")
            elif days <= _DEADLINE_NEAR_DAYS:
                score = max(score, 4)
                reasons.append(f"最も近い締切が{_DEADLINE_NEAR_DAYS}日以内")
            elif days <= 7:
                score = max(score, 3)
                reasons.append("最も近い締切が7日以内")
            if bool(nearest.get("blocked")):
                score = max(score, 4)
                reasons.append("その締切のタスクがブロック中")

        tight, _tight_conf, _tight_rationale = self._compute_capacity_is_tight(state)
        if tight is not None:
            have_data = True
            if tight:
                score = max(score, 4)
                reasons.append("今後の予定が可処分時間に対して詰まっている")

        score = max(question.min, min(question.max, score))
        if not have_data:
            median = int(round((question.min + question.max) / 2))
            return median, 0.3, "締切・予定量とも材料が無いため中央値を返す"

        rationale = "、".join(reasons) if reasons else "顕著なリスク要因が無いため低めのスコア"
        confidence = 0.7 if reasons else 0.5
        return score, confidence, rationale

    def _compute_deadline_at_risk(self, state: dict[str, Any]) -> _RuleResult:
        """deadline_at_risk の判定本体。needs_reschedule からも再利用する。"""
        nearest = _nearest_deadline(state)
        if nearest is None or nearest.get("deadline_days") is None:
            return None, 0.3, "今後の締切情報が無いため不明"

        days = _num(nearest.get("deadline_days"), default=999)
        blocked = bool(nearest.get("blocked"))
        project = nearest.get("project")

        past_by_project = state.get("past_by_project")
        has_project_data = bool(project) and _num(state.get("past_active_days")) > 0
        time_spent = 0.0
        if has_project_data and isinstance(past_by_project, dict):
            time_spent = _num(past_by_project.get(project))
        barely_worked = has_project_data and time_spent < _LOW_PROJECT_MIN

        is_near = days <= _DEADLINE_NEAR_DAYS
        reasons = [f"最も近い締切まで{int(days)}日"]
        if blocked:
            reasons.append("該当タスクがブロック中")
        if barely_worked:
            reasons.append("過去N日の当該案件への投下時間が少ない")

        if is_near and (blocked or barely_worked):
            confidence = 0.8 if days <= 1 else 0.65
            return True, confidence, "、".join(reasons) + "のため締切に遅れる恐れあり"

        if not has_project_data:
            # 案件の過去実績が無く、blocked 情報だけでは根拠薄のため断定しない
            return (
                False,
                0.35,
                "、".join(reasons) + "。過去実績データが無く根拠薄のためFalse寄りで判定",
            )

        return False, 0.6, "、".join(reasons) + "のため締切遵守の見込みと判断"

    def _deadline_at_risk(self, state: dict[str, Any], question: Question) -> _RuleResult:
        return self._compute_deadline_at_risk(state)

    def _needs_reschedule(self, state: dict[str, Any], question: Question) -> _RuleResult:
        at_risk, at_risk_conf, _at_risk_rationale = self._compute_deadline_at_risk(state)
        if at_risk is None:
            return None, 0.3, "締切リスクが不明のため組み替えの要否も判断できない"
        if not at_risk:
            return False, 0.5, "締切に遅れる恐れが低いため予定の組み替えは不要と判断"

        tight, tight_conf, _tight_rationale = self._compute_capacity_is_tight(state)
        if tight is None:
            return (
                None,
                0.35,
                "締切に遅れる恐れはあるが今後の予定の詰まり具合が不明なため断定しない",
            )
        if tight:
            confidence = min(at_risk_conf, tight_conf)
            return True, confidence, "締切に遅れる恐れがあり、かつ今後の予定も詰まっているため組み替えが必要"
        return False, 0.5, "締切に遅れる恐れはあるが今後の予定には余裕があるため現状維持で判断"

    def _deadline_pressure(self, state: dict[str, Any], question: Question) -> _RuleResult:
        nearest = _nearest_deadline(state)
        if nearest is None or nearest.get("deadline_days") is None:
            median = int(round((question.min + question.max) / 2))
            return median, 0.3, "締切情報が無いため中央値を返す"

        days = _num(nearest.get("deadline_days"), default=999)
        if days <= 1:
            score = 5
        elif days <= 2:
            score = 4
        elif days <= 4:
            score = 3
        elif days <= 7:
            score = 2
        else:
            score = 1

        score = max(question.min, min(question.max, score))
        return score, 0.7, f"最も近い締切まで{int(days)}日のため切迫度{score}と判断"
