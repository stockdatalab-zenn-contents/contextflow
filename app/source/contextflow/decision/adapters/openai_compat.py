"""decision/adapters/openai_compat.py

OpenAI互換 chat/completions API（Ollama 等）向けの Decision Engine Adapter。
`urllib.request` のみで実装し、外部ライブラリには依存しない。
LLM には文章を書かせず、JSON だけを返させる「賢いif文」として使う。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from contextflow.config import AppConfig
from contextflow.contracts.decision import Answer, DecisionEngine, DecisionRequest, DecisionResponse

_TIMEOUT_SEC = 60

_SYSTEM_PROMPT = (
    "あなたは状態を見て型付きの判断だけを返すコンポーネントである。"
    "文章を書かず、次の形式のJSONオブジェクトのみを返すこと: "
    '{"<質問のkey>": {"value": <型に応じた値>, "confidence": <0から1の数値>}, ...}。'
    "confidence は自己申告値であり、過信しないこと。"
)


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


def _extract_json(text: str) -> dict[str, Any]:
    """レスポンス本文からJSONだけを取り出す。前後に文章が混ざる場合は最初の{〜最後の}を抜き出す。"""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise
        parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise json.JSONDecodeError("JSONオブジェクトではない", text, 0)
    return parsed


def _clamp01(value: Any, default: float = 0.5) -> float:
    """confidence を 0.0〜1.0 へ丸める。数値化できなければ default。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN 対策
        return default
    return max(0.0, min(1.0, number))


class OpenAICompatEngine(DecisionEngine):
    """OpenAI互換 `{base_url}/chat/completions` を叩く Adapter（Ollama 等）。"""

    name = "openai_compat"

    def __init__(self, config: AppConfig, calibrator=None) -> None:
        super().__init__(calibrator=calibrator)
        self.config = config
        self.base_url = str(config.get("llm.openai_compat.base_url", "")).rstrip("/")
        self.model = config.get("llm.openai_compat.model", "")
        # Ollama等はキー不要なので、未設定でもエラーにはせずヘッダを省略する
        self.api_key: Optional[str] = config.secret("llm.openai_compat.api_key_env")

    # ------------------------------------------------------------------
    # DecisionEngine 実装
    # ------------------------------------------------------------------

    def _ask(self, request: DecisionRequest) -> DecisionResponse:
        if not self.base_url:
            raise RuntimeError("llm.openai_compat.base_url が未設定。config.tomlに接続先を設定してください。")

        url = f"{self.base_url}/chat/completions"
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_payload(request)},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        data = json.dumps(body).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        http_request = urllib.request.Request(url, data=data, headers=headers, method="POST")

        start = time.monotonic()
        try:
            with urllib.request.urlopen(http_request, timeout=_TIMEOUT_SEC) as response:
                raw_body = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"OpenAI互換API ({url}) への接続に失敗: {exc}. base_urlとサーバー起動状態を確認してください。"
            ) from exc
        latency_ms = int((time.monotonic() - start) * 1000)

        try:
            response_json = json.loads(raw_body)
            content = response_json["choices"][0]["message"]["content"]
            data_dict = _extract_json(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "OpenAI互換APIのレスポンスがJSONとして解釈できない。モデルとプロンプトを見直してください。"
            ) from exc

        answers: dict[str, Answer] = {}
        for question in request.questions:
            item = data_dict.get(question.key)
            if not isinstance(item, dict):
                continue
            answers[question.key] = Answer(
                key=question.key,
                value=item.get("value"),
                raw_confidence=_clamp01(item.get("confidence")),
                engine=self.name,
            )

        return DecisionResponse(answers=answers, engine=self.name, latency_ms=latency_ms, raw=data_dict)
