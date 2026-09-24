"""contracts/decision.py・decision/registry.py・decision/adapters の単体テスト。

API は絶対に呼ばない（課金されるため）。ネットワークアクセスも一切しない。
claude Adapter に関するテストは build_output_schema（純粋なJSON Schema組み立て）と
registry のフォールバック機構のみを対象とし、実際の API 呼び出し（_ask）は呼ばない。
"""

from __future__ import annotations

import unittest
from unittest import mock

from tests.conftest_path import add_source_path

add_source_path()

from contextflow.config import AppConfig, load_question_sets  # noqa: E402
from contextflow.contracts.decision import (  # noqa: E402
    Answer,
    DecisionRequest,
    Question,
    QuestionType,
)
from contextflow.decision import registry  # noqa: E402
from contextflow.decision.adapters.claude_api import build_output_schema  # noqa: E402
from contextflow.decision.adapters.rule_based import RuleBasedEngine  # noqa: E402


class QuestionCoerceTests(unittest.TestCase):
    """Question.coerce: noul/choice/score それぞれの型寄せ。"""

    def test_noul_from_string(self):
        q = Question(key="q", type=QuestionType.NOUL)
        self.assertTrue(q.coerce("はい"))
        self.assertTrue(q.coerce("YES"))
        self.assertTrue(q.coerce("1"))
        self.assertFalse(q.coerce("no"))
        self.assertFalse(q.coerce("いいえ"))
        self.assertIs(q.coerce(True), True)
        self.assertIs(q.coerce(False), False)

    def test_choice_case_insensitive(self):
        q = Question(key="q", type=QuestionType.CHOICE, choices=["Continue", "Break"])
        self.assertEqual(q.coerce("continue"), "Continue")
        self.assertEqual(q.coerce("BREAK"), "Break")
        self.assertIsNone(q.coerce("unknown_choice"))

    def test_score_range_clip(self):
        q = Question(key="q", type=QuestionType.SCORE, min=1, max=5)
        self.assertEqual(q.coerce(10), 5)
        self.assertEqual(q.coerce(-3), 1)
        self.assertEqual(q.coerce(3.7), 4)
        self.assertIsNone(q.coerce("abc"))

    def test_none_value_returns_none_for_every_type(self):
        cases = [
            Question(key="q", type=QuestionType.NOUL),
            Question(key="q", type=QuestionType.CHOICE, choices=["a", "b"]),
            Question(key="q", type=QuestionType.SCORE, min=1, max=5),
        ]
        for question in cases:
            with self.subTest(type=question.type):
                self.assertIsNone(question.coerce(None))


class DecisionRequestValidateTests(unittest.TestCase):
    """DecisionRequest.validate: key重複・選択肢不足・min>=max で ValueError。"""

    def test_duplicate_key_raises(self):
        questions = [
            Question(key="a", type=QuestionType.NOUL),
            Question(key="a", type=QuestionType.NOUL),
        ]
        request = DecisionRequest(state={}, questions=questions)
        with self.assertRaises(ValueError):
            request.validate()

    def test_choice_needs_two_or_more_choices(self):
        questions = [Question(key="a", type=QuestionType.CHOICE, choices=["only_one"])]
        request = DecisionRequest(state={}, questions=questions)
        with self.assertRaises(ValueError):
            request.validate()

    def test_score_min_ge_max_raises(self):
        questions = [Question(key="a", type=QuestionType.SCORE, min=5, max=5)]
        request = DecisionRequest(state={}, questions=questions)
        with self.assertRaises(ValueError):
            request.validate()

    def test_empty_questions_raises(self):
        request = DecisionRequest(state={}, questions=[])
        with self.assertRaises(ValueError):
            request.validate()


class RuleBasedEngineTests(unittest.TestCase):
    """RuleBasedEngine: questions.toml の3セットすべてで全keyに答えが返ること、型が正しいこと。"""

    @classmethod
    def setUpClass(cls):
        # 実ファイル app/source/config/questions.toml を読むだけで、書き込みはしない
        cls.question_sets = load_question_sets()

    def setUp(self):
        self.engine = RuleBasedEngine(calibrator=None, config=None)

    @staticmethod
    def _type_checker(question: Question):
        if question.type == QuestionType.NOUL:
            return lambda v: isinstance(v, bool)
        if question.type == QuestionType.CHOICE:
            return lambda v: v in question.choices
        return lambda v: isinstance(v, int) and question.min <= v <= question.max

    def test_all_question_sets_answer_every_key_with_correct_type(self):
        # 判定材料をひととおり埋めたダミー state
        state = {
            "task_elapsed_min": 20,
            "last_break_min_ago": 30,
            "recent_context_switches": 1,
            "blocked_tasks": 1,
            "open_tasks": 3,
            "current_task": "Implement X",
            "task_deadline_days": 2,
            "task_blocked": False,
            "task_priority": 2,
            "gap_has_calendar_event": True,
            # week_outlook / deadline_risk 用の材料（材料ありのケースを網羅するため）
            "past_days": 7,
            "past_total_min": 1400,
            "past_active_days": 5,
            "past_by_project": {"sample_project": 200},
            "upcoming_days": 7,
            "upcoming_planned_min": 300,
            "upcoming_by_type": {"meeting": 300},
            "upcoming_items": [
                {
                    "start": "2026-09-25T10:00:00+09:00",
                    "end": "2026-09-25T11:00:00+09:00",
                    "activity_type": "meeting",
                    "summary": "定例MTG",
                    "project": "sample_project",
                    "duration_min": 60,
                }
            ],
            "upcoming_deadlines": [
                {
                    "title": "資料提出",
                    "project": "sample_project",
                    "deadline_days": 3,
                    "priority": 2,
                    "blocked": False,
                }
            ],
        }
        self.assertGreaterEqual(len(self.question_sets), 3)
        for set_name, questions in self.question_sets.items():
            with self.subTest(set_name=set_name):
                request = DecisionRequest(state=state, questions=questions)
                response = self.engine.ask(request)
                for question in questions:
                    self.assertIn(question.key, response.answers)
                    answer = response.answers[question.key]
                    self.assertIsInstance(answer, Answer)
                    checker = self._type_checker(question)
                    self.assertTrue(
                        checker(answer.value),
                        f"{set_name}.{question.key} の値型が不正: {answer.value!r}",
                    )

    def test_unknown_key_still_answers(self):
        question = Question(key="totally_unknown_key", type=QuestionType.NOUL, instruction="dummy")
        request = DecisionRequest(state={}, questions=[question])
        response = self.engine.ask(request)
        self.assertIn("totally_unknown_key", response.answers)
        self.assertIsInstance(response.answers["totally_unknown_key"].value, bool)


class _DummyCalibrator:
    """テスト用の固定変換 Calibrator（contracts.decision.Calibrator を満たす）。"""

    def __init__(self, fixed: float = 0.42):
        self.fixed = fixed
        self.calls: list[tuple[str, str, float]] = []

    def calibrate(self, engine: str, question_key: str, raw_confidence: float) -> float:
        self.calls.append((engine, question_key, raw_confidence))
        return self.fixed


class DecisionEngineAskCalibratorTests(unittest.TestCase):
    """DecisionEngine.ask が calibrator を通すこと。"""

    def test_confidence_is_calibrated(self):
        calibrator = _DummyCalibrator(fixed=0.42)
        engine = RuleBasedEngine(calibrator=calibrator, config=None)
        question = Question(key="continue_current_task", type=QuestionType.NOUL)
        request = DecisionRequest(state={}, questions=[question])

        response = engine.ask(request)
        answer = response.answers["continue_current_task"]

        # raw_confidence はルール側の自己申告値のまま、confidence だけ校正後の値になる
        self.assertEqual(answer.confidence, 0.42)
        self.assertNotEqual(answer.raw_confidence, 0.42)
        self.assertEqual(len(calibrator.calls), 1)
        self.assertEqual(calibrator.calls[0][0], "rule_based")
        self.assertEqual(calibrator.calls[0][1], "continue_current_task")


class UrgencyProjectTests(unittest.TestCase):
    """urgency が recent_changes の案件名を見て判断すること。"""

    def setUp(self) -> None:
        self.engine = RuleBasedEngine()
        self.questions = load_question_sets()["next_action"]
        self.base = {
            "time": "14:10",
            "open_tasks": 3,
            "blocked_tasks": 0,
            "current_project": "sample_project",
            "task_elapsed_min": 30,
        }

    def _urgency(self, recent_changes):
        state = dict(self.base, recent_changes=recent_changes)
        response = self.engine.ask(
            DecisionRequest(state=state, questions=self.questions)
        )
        return response.answers["urgency"]

    def test_recent_change_in_current_project_raises_urgency(self):
        answer = self._urgency(
            [{"description": "API不可が判明", "project": "sample_project", "minutes_ago": 30}]
        )
        self.assertGreaterEqual(answer.value, 4)
        self.assertIn("sample_project", answer.rationale)

    def test_old_change_does_not_raise_urgency(self):
        """何日も前の変化は「直近の変化」として扱わない。"""
        answer = self._urgency(
            [{"description": "古い変化", "project": "sample_project", "minutes_ago": 7200}]
        )
        self.assertLess(answer.value, 4)

    def test_change_without_minutes_ago_does_not_raise_urgency(self):
        """日時が無い（古い形式の）データは、新しさが不明なので押し上げない。"""
        answer = self._urgency([{"description": "旧形式", "project": "sample_project"}])
        self.assertLess(answer.value, 4)

    def test_change_in_other_project_does_not_raise_urgency(self):
        answer = self._urgency(
            [{"description": "仕様変更", "project": "ds_review", "minutes_ago": 30}]
        )
        self.assertLess(answer.value, 4)

    def test_broken_recent_changes_does_not_raise(self):
        """state が想定外の形でも落ちない（外部から来る値のため）。"""
        self.assertLess(self._urgency("not-a-list").value, 4)
        self.assertLess(self._urgency([None, "x"]).value, 4)


class WeekOutlookDeadlineRiskTests(unittest.TestCase):
    """week_outlook / deadline_risk: 新設6問の型・材料の有無によるconfidenceの違い。

    未来の判断は答え合わせが遅れて校正が効きにくいため、材料が無いときは
    断定せず（Noneや中央値）confidenceも低くする、という設計方針を確認する。
    """

    _NO_MATERIAL_THRESHOLD = 0.7

    def setUp(self) -> None:
        self.engine = RuleBasedEngine()
        self.week_outlook = load_question_sets()["week_outlook"]
        self.deadline_risk = load_question_sets()["deadline_risk"]

    def _ask(self, questions, state):
        return self.engine.ask(DecisionRequest(state=state, questions=questions)).answers

    def test_week_outlook_all_keys_answer_with_correct_type(self) -> None:
        """材料ありの state では3問すべてが具体的な値（型どおり）で返る。"""
        state = {
            "past_active_days": 5,
            "past_total_min": 1400,
            "past_by_project": {"sample_project": 200},
            "upcoming_days": 7,
            "upcoming_planned_min": 300,
            "upcoming_by_type": {"meeting": 300},
            "upcoming_items": [
                {
                    "start": "2026-09-25T10:00:00+09:00",
                    "end": "2026-09-25T11:00:00+09:00",
                    "activity_type": "meeting",
                    "summary": "定例MTG",
                    "project": "sample_project",
                    "duration_min": 60,
                }
            ],
            "upcoming_deadlines": [
                {
                    "title": "資料提出",
                    "project": "sample_project",
                    "deadline_days": 3,
                    "priority": 2,
                    "blocked": False,
                }
            ],
        }
        answers = self._ask(self.week_outlook, state)
        self.assertIsInstance(answers["capacity_is_tight"].value, bool)
        self.assertIn(answers["focus_project"].value, ["current", "deadline", "planned"])
        self.assertIsInstance(answers["week_risk"].value, int)
        self.assertTrue(1 <= answers["week_risk"].value <= 5)

    def test_deadline_risk_all_keys_answer_with_correct_type(self) -> None:
        """材料ありの state では3問すべてが具体的な値（型どおり）で返る。"""
        state = {
            "past_active_days": 5,
            "past_total_min": 1000,
            "past_by_project": {"sample_project": 500},
            "upcoming_days": 5,
            "upcoming_planned_min": 200,
            "upcoming_deadlines": [
                {
                    "title": "資料提出",
                    "project": "sample_project",
                    "deadline_days": 2,
                    "priority": 1,
                    "blocked": True,
                }
            ],
        }
        answers = self._ask(self.deadline_risk, state)
        self.assertIsInstance(answers["deadline_at_risk"].value, bool)
        self.assertIsInstance(answers["needs_reschedule"].value, bool)
        self.assertIsInstance(answers["deadline_pressure"].value, int)
        self.assertTrue(1 <= answers["deadline_pressure"].value <= 5)

    def test_no_material_state_keeps_confidence_low(self) -> None:
        """upcoming_deadlinesが空・past_active_daysが0 の状態では、
        6問すべてでconfidenceが閾値(0.7)未満になる（断定しない）。"""
        state = {"upcoming_deadlines": [], "past_active_days": 0}

        for questions in (self.week_outlook, self.deadline_risk):
            answers = self._ask(questions, state)
            for key, answer in answers.items():
                with self.subTest(key=key):
                    self.assertLess(
                        answer.confidence,
                        self._NO_MATERIAL_THRESHOLD,
                        f"{key} は材料が無いのにconfidenceが高すぎる: {answer.confidence}",
                    )

    def test_deadline_at_risk_true_and_confident_when_near_and_blocked(self) -> None:
        """締切が2日後・blockedなら deadline_at_risk は真になり、
        材料が無い場合よりconfidenceが上がる。"""
        state = {
            "upcoming_deadlines": [
                {
                    "title": "資料提出",
                    "project": "sample_project",
                    "deadline_days": 2,
                    "priority": 1,
                    "blocked": True,
                }
            ],
            "past_active_days": 5,
            "past_by_project": {"sample_project": 500},
        }
        answer = self._ask(self.deadline_risk, state)["deadline_at_risk"]
        self.assertTrue(answer.value)
        self.assertGreaterEqual(answer.confidence, 0.6)

        no_material_answer = self._ask(
            self.deadline_risk, {"upcoming_deadlines": [], "past_active_days": 0}
        )["deadline_at_risk"]
        self.assertLess(no_material_answer.confidence, answer.confidence)

    def test_capacity_is_tight_true_when_planned_far_exceeds_past(self) -> None:
        """今後の予定時間が、過去実績ベースの可処分時間の目安を明らかに超えると真になる。"""
        state = {
            "past_active_days": 5,
            "past_total_min": 500,  # 1日あたり100分の実績
            "upcoming_days": 5,
            "upcoming_planned_min": 800,  # 目安(5*100*0.8=400分)を大幅に超える
        }
        answer = self._ask(self.week_outlook, state)["capacity_is_tight"]
        self.assertTrue(answer.value)

    def test_existing_sets_unchanged_for_representative_input(self) -> None:
        """代表的な入力で next_action の既存挙動が変わっていないことを念のため確認。"""
        state = {
            "task_elapsed_min": 20,
            "last_break_min_ago": 30,
            "recent_context_switches": 1,
            "open_tasks": 3,
            "blocked_tasks": 1,
            "current_task": "Implement X",
            "task_deadline_days": 2,
        }
        answers = self._ask(load_question_sets()["next_action"], state)
        self.assertTrue(answers["continue_current_task"].value)
        self.assertEqual(answers["next_task_type"].value, "continue")
        self.assertGreaterEqual(answers["urgency"].value, 1)


class CreateEngineTests(unittest.TestCase):
    """registry.create_engine: 未知名でValueError、claude生成失敗でrule_basedへフォールバック。"""

    def setUp(self):
        self.config = AppConfig(data={"decision": {"engine": "rule_based"}})

    def test_unknown_name_raises_value_error(self):
        with self.assertRaises(ValueError):
            registry.create_engine(self.config, name="not_a_real_engine")

    def test_default_name_is_rule_based(self):
        engine = registry.create_engine(self.config)
        self.assertEqual(engine.name, "rule_based")

    def test_claude_construction_failure_falls_back_to_rule_based(self):
        # 実装メモ（疑わしい点）: 現行の ClaudeDecisionEngine.__init__ は
        # APIキー未設定でも例外を送出しない（RuntimeError は _ask 実行時に
        # 初めて出る）ため、「APIキー未設定」単体では registry のフォールバックは
        # 発火しない。ここでは claude Adapter の生成失敗を模し、
        # registry 側のフォールバック機構そのものを検証する。
        with mock.patch(
            "contextflow.decision.adapters.claude_api.ClaudeDecisionEngine.__init__",
            side_effect=RuntimeError("APIキー未設定を模した生成失敗"),
        ):
            engine = registry.create_engine(self.config, name="claude")
        self.assertEqual(engine.name, "rule_based")


class BuildOutputSchemaTests(unittest.TestCase):
    """build_output_schema: noul->boolean / choice->enum / score->integer。API は呼ばない。"""

    def test_schema_shapes(self):
        questions = [
            Question(key="q_noul", type=QuestionType.NOUL, instruction="x"),
            Question(key="q_choice", type=QuestionType.CHOICE, choices=["a", "b"], instruction="x"),
            Question(key="q_score", type=QuestionType.SCORE, min=1, max=5, instruction="x"),
        ]
        schema = build_output_schema(questions)

        self.assertEqual(schema["type"], "object")
        self.assertEqual(schema["additionalProperties"], False)
        self.assertEqual(set(schema["required"]), {"q_noul", "q_choice", "q_score"})

        props = schema["properties"]
        self.assertEqual(props["q_noul"]["properties"]["value"], {"type": "boolean"})
        self.assertEqual(
            props["q_choice"]["properties"]["value"], {"type": "string", "enum": ["a", "b"]}
        )
        self.assertEqual(
            props["q_score"]["properties"]["value"],
            {"type": "integer", "minimum": 1, "maximum": 5},
        )
        for key in ("q_noul", "q_choice", "q_score"):
            self.assertEqual(props[key]["additionalProperties"], False)
            self.assertEqual(props[key]["required"], ["value", "confidence"])


if __name__ == "__main__":
    unittest.main()
