"""decision/adapters/claude_api.py

Anthropic 公式 SDK (`anthropic`) を使う Decision Engine Adapter。
LLM には文章を書かせず、JSON Schema で縛った JSON だけを返させる
「賢いif文」として使う（`docs/20260923_module_contract.md` が唯一の正）。

`anthropic` は関数内で遅延 import する（標準ライブラリのみで import 可能にするため）。
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from typing import Any, Optional

from contextflow.config import AppConfig
from contextflow.contracts.decision import (
    Answer,
    DecisionEngine,
    DecisionRequest,
    DecisionResponse,
    Question,
    QuestionType,
)

_SYSTEM_PROMPT = (
    "あなたは状態を見て型付きの判断だけを返すコンポーネントである。"
    "文章を書かず、指定されたJSON Schemaに厳密に従うJSONのみを返すこと。"
    "confidence は自己申告値であり、過信しないこと。"
)


def build_output_schema(questions: Sequence[Question]) -> dict[str, Any]:
    """questions から Claude の structured outputs (`output_config.format`) 用 JSON Schema を組み立てる。"""
    properties: dict[str, Any] = {}
    for question in questions:
        if question.type == QuestionType.NOUL:
            value_schema: dict[str, Any] = {"type": "boolean"}
        elif question.type == QuestionType.CHOICE:
            value_schema = {"type": "string", "enum": list(question.choices)}
        else:  # QuestionType.SCORE
            value_schema = {"type": "integer", "minimum": question.min, "maximum": question.max}
        properties[question.key] = {
            "type": "object",
            "properties": {
                "value": value_schema,
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["value", "confidence"],
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties.keys()),
        "additionalProperties": False,
    }


def _build_user_payload(request: DecisionRequest) -> str:
    """{"state": ..., "questions": [...]} 形式のJSONテキストを組み立てる。"""
    questions_payload = [
        {
            "key": q.key,
            "type": q.type.value,
            "instruction": q.instruction,
            "choices": q.choices,
            "min": q.min,
            "max": q.max,
        }
        for q in request.questions
    ]
    payload: dict[str, Any] = {"state": request.state, "questions": questions_payload}
    if request.context:
        payload["context"] = request.context
    return json.dumps(payload, ensure_ascii=False)


def _clamp01(value: Any, default: float = 0.5) -> float:
    """confidence を 0.0〜1.0 へ丸める。数値化できなければ default。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN 対策
        return default
    return max(0.0, min(1.0, number))


class ClaudeDecisionEngine(DecisionEngine):
    """Anthropic 公式 SDK を使う Decision Engine。"""

    name = "claude"

    def __init__(self, config: AppConfig, calibrator=None) -> None:
        super().__init__(calibrator=calibrator)
        self.config = config
        self.model = config.get("llm.claude.model", "claude-opus-5")
        self.max_tokens = int(config.get("llm.claude.max_tokens", 2000))
        self.effort = config.get("llm.claude.effort", "low")
        self.api_key_env = config.get("llm.claude.api_key_env", "ANTHROPIC_API_KEY")
        self.api_key: Optional[str] = config.secret("llm.claude.api_key_env")

    # ------------------------------------------------------------------
    # DecisionEngine 実装
    # ------------------------------------------------------------------

    def _ask(self, request: DecisionRequest) -> DecisionResponse:
        if not self.api_key:
            raise RuntimeError(f"Claude APIキーが未設定。環境変数 {self.api_key_env} を設定してください。")

        try:
            import anthropic  # 標準ライブラリのみの制約があるため関数内で遅延 import
        except ImportError as exc:
            raise RuntimeError("anthropicパッケージが未インストール。`pip install anthropic` を実行してください。") from exc

        schema = build_output_schema(request.questions)
        payload_json_text = _build_user_payload(request)

        client = anthropic.Anthropic(api_key=self.api_key)

        start = time.monotonic()
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": payload_json_text}],
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
            )
        except Exception as exc:
            raise RuntimeError(
                f"Claude API呼び出しに失敗: {exc}. APIキー・ネットワーク・model名を確認してください。"
            ) from exc
        latency_ms = int((time.monotonic() - start) * 1000)

        # stop_reason を見ずに JSON 解析へ進むと、max_tokens 打ち切りや
        # refusal のときに「schemaを見直せ」という誤った案内になる。
        # 原因ごとに区別してから解析する。
        if response.stop_reason == "max_tokens":
            raise RuntimeError(
                "Claudeの応答がmax_tokensで打ち切られた。"
                "config の llm.claude.max_tokens を増やすこと。"
            )
        if response.stop_reason == "refusal":
            # stop_details は refusal 以外では None なので、存在確認してから読む
            stop_details = getattr(response, "stop_details", None)
            category = stop_details.category if stop_details is not None else None
            raise RuntimeError(
                f"Claudeが応答を拒否した（refusal, category={category}）。"
            )

        text = next((block.text for block in response.content if block.type == "text"), None)
        if text is None:
            raise RuntimeError("Claudeのレスポンスにテキストブロックが無い。プロンプトかスキーマを見直してください。")

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Claudeのレスポンスがスキーマ通りのJSONとして解釈できない。schemaの内容を見直してください。") from exc

        answers: dict[str, Answer] = {}
        for question in request.questions:
            item = data.get(question.key)
            if not isinstance(item, dict):
                continue
            answers[question.key] = Answer(
                key=question.key,
                value=item.get("value"),
                raw_confidence=_clamp01(item.get("confidence")),
                engine=self.name,
            )

        return DecisionResponse(answers=answers, engine=self.name, latency_ms=latency_ms, raw=data)
