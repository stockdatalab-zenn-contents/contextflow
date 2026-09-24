"""コントラクト層: Jev風 Decision Engine のインターフェース。

アプリ側は noul / choice / score の3種類だけを呼ぶ。
Jev・Claude API・OpenAI互換API・ルールベースは Adapter で差し替える。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol, Union


class QuestionType(str, Enum):
    """問いの型。"""

    NOUL = "noul"      # yes / no
    CHOICE = "choice"  # 選択肢から1つ
    SCORE = "score"    # 整数スコア


AnswerValue = Union[bool, str, int, float, None]


@dataclass
class Question:
    """1つの型付き質問。"""

    key: str
    type: QuestionType
    instruction: str = ""
    choices: list[str] = field(default_factory=list)
    min: int = 1
    max: int = 5

    def validate(self) -> None:
        """定義の整合性を検査。不正なら ValueError。"""
        if not self.key:
            raise ValueError("Question.key が空")
        if self.type == QuestionType.CHOICE and len(self.choices) < 2:
            raise ValueError(f"choice 型 '{self.key}' は選択肢が2つ以上必要")
        if self.type == QuestionType.SCORE and self.min >= self.max:
            raise ValueError(f"score 型 '{self.key}' は min < max が必要")

    def coerce(self, value: Any) -> AnswerValue:
        """エンジンが返した生値を、この問いの型へ寄せる。"""
        if value is None:
            return None
        if self.type == QuestionType.NOUL:
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("true", "yes", "y", "1", "はい")
        if self.type == QuestionType.CHOICE:
            text = str(value).strip()
            for choice in self.choices:
                if text.lower() == choice.lower():
                    return choice
            return None
        try:
            number = int(round(float(value)))
        except (TypeError, ValueError):
            return None
        return max(self.min, min(self.max, number))


@dataclass
class DecisionRequest:
    """1回の問い合わせ。state は Context Builder が作った構造化済みの値のみ。"""

    state: dict[str, Any]
    questions: list[Question]
    context: str = ""

    def validate(self) -> None:
        if not self.questions:
            raise ValueError("questions が空")
        keys = [q.key for q in self.questions]
        if len(keys) != len(set(keys)):
            raise ValueError("questions の key が重複")
        for question in self.questions:
            question.validate()


@dataclass
class Answer:
    """1つの問いへの答え。

    raw_confidence はエンジンの自己申告値、confidence は校正後の値。
    校正前の値をそのまま閾値判定に使わない。
    """

    key: str
    value: AnswerValue
    raw_confidence: float = 0.5
    confidence: float = 0.5
    engine: str = ""
    rationale: str = ""


@dataclass
class DecisionResponse:
    """1回の問い合わせの結果。"""

    answers: dict[str, Answer]
    engine: str = ""
    latency_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    def value(self, key: str, default: AnswerValue = None) -> AnswerValue:
        answer = self.answers.get(key)
        return default if answer is None else answer.value

    def confidence(self, key: str, default: float = 0.0) -> float:
        answer = self.answers.get(key)
        return default if answer is None else answer.confidence


class Calibrator(Protocol):
    """自己申告 confidence を実測ベースへ校正する役。"""

    def calibrate(self, engine: str, question_key: str, raw_confidence: float) -> float:
        ...


class DecisionEngine(abc.ABC):
    """Adapter が実装する唯一のインターフェース。"""

    name: str = "base"

    def __init__(self, calibrator: Optional[Calibrator] = None) -> None:
        self.calibrator = calibrator

    @abc.abstractmethod
    def _ask(self, request: DecisionRequest) -> DecisionResponse:
        """Adapter 固有の実処理。"""

    def ask(self, request: DecisionRequest) -> DecisionResponse:
        """検証・実行・confidence 校正までをまとめて行う共通入口。"""
        request.validate()
        response = self._ask(request)
        for question in request.questions:
            answer = response.answers.get(question.key)
            if answer is None:
                continue
            answer.value = question.coerce(answer.value)
            answer.engine = answer.engine or response.engine or self.name
            answer.confidence = self._calibrate(question.key, answer.raw_confidence)
        return response

    def _calibrate(self, question_key: str, raw_confidence: float) -> float:
        if self.calibrator is None:
            return raw_confidence
        return self.calibrator.calibrate(self.name, question_key, raw_confidence)

    # --- 呼び出し側が使う3つの便利メソッド -------------------------------

    def noul(self, state: dict[str, Any], key: str, instruction: str) -> Answer:
        question = Question(key=key, type=QuestionType.NOUL, instruction=instruction)
        return self.ask(DecisionRequest(state=state, questions=[question])).answers[key]

    def choice(
        self, state: dict[str, Any], key: str, choices: list[str], instruction: str = ""
    ) -> Answer:
        question = Question(
            key=key, type=QuestionType.CHOICE, instruction=instruction, choices=choices
        )
        return self.ask(DecisionRequest(state=state, questions=[question])).answers[key]

    def score(
        self,
        state: dict[str, Any],
        key: str,
        instruction: str = "",
        min_value: int = 1,
        max_value: int = 5,
    ) -> Answer:
        question = Question(
            key=key,
            type=QuestionType.SCORE,
            instruction=instruction,
            min=min_value,
            max=max_value,
        )
        return self.ask(DecisionRequest(state=state, questions=[question])).answers[key]
