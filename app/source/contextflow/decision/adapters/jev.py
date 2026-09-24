"""decision/adapters/jev.py

Jev（Typesafe.ai の Decision API）向けの薄い Decision Engine Adapter。
Jev のネイティブ形式 `{state, questions}` にほぼそのまま寄せて POST するだけの実装。
`urllib.request` のみで実装し、外部ライブラリには依存しない。
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


def _build_questions_payload(request: DecisionRequest) -> dict[str, Any]:
    """Jevのネイティブ形式 {key: {...}} へ questions を変換する。"""
    return {
        q.key: {
            "type": q.type.value,
            "instruction": q.instruction,
            "choices": q.choices,
            "min": q.min,
            "max": q.max,
        }
        for q in request.questions
    }


def _clamp01(value: Any, default: float = 0.5) -> float:
    """confidence を 0.0〜1.0 へ丸める。数値化できなければ default。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN 対策
        return default
    return max(0.0, min(1.0, number))


class JevEngine(DecisionEngine):
    """Jevネイティブ API (`POST {base_url}/decide`) を叩く薄い Adapter。"""

    name = "jev"

    def __init__(self, config: AppConfig, calibrator=None) -> None:
        super().__init__(calibrator=calibrator)
        self.config = config
        self.base_url = str(config.get("llm.jev.base_url", "")).rstrip("/")
        self.model = config.get("llm.jev.model", "")
        self.api_key_env = config.get("llm.jev.api_key_env", "JEV_API_KEY")
        self.api_key: Optional[str] = config.secret("llm.jev.api_key_env")

    # ------------------------------------------------------------------
    # DecisionEngine 実装
    # ------------------------------------------------------------------

    def _ask(self, request: DecisionRequest) -> DecisionResponse:
        if not self.base_url:
            raise RuntimeError("llm.jev.base_url が未設定。config.tomlに接続先を設定してください。")
        if not self.api_key:
            raise RuntimeError(f"Jev APIキーが未設定。環境変数 {self.api_key_env} を設定してください。")

        url = f"{self.base_url}/decide"
        body: dict[str, Any] = {
            "state": request.state,
            "questions": _build_questions_payload(request),
        }
        if request.context:
            body["context"] = request.context
        data = json.dumps(body).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        http_request = urllib.request.Request(url, data=data, headers=headers, method="POST")

        start = time.monotonic()
        try:
            with urllib.request.urlopen(http_request, timeout=_TIMEOUT_SEC) as response:
                raw_body = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Jev API ({url}) への接続に失敗: {exc}. base_urlとAPIキーを確認してください。"
            ) from exc
        latency_ms = int((time.monotonic() - start) * 1000)

        try:
            data_dict = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("JevのレスポンスがJSONとして解釈できない。APIの応答形式を確認してください。") from exc
        if not isinstance(data_dict, dict):
            raise RuntimeError("Jevのレスポンス形式が想定外（JSONオブジェクトではない）。")

        answers: dict[str, Answer] = {}
        for question in request.questions:
            item = data_dict.get(question.key)
            if item is None:
                continue
            if isinstance(item, dict):
                value = item.get("value")
                # Jevは確率が校正済みという前提のサービスのため、raw_confidenceをそのまま使う
                # （基底クラス側のcalibratorによる二重補正はconfidence側で行われる）
                raw_confidence = _clamp01(item.get("confidence"))
            else:
                # レスポンス形状が想定と違っても value だけで動くようにする
                value = item
                raw_confidence = 0.5
            answers[question.key] = Answer(
                key=question.key,
                value=value,
                raw_confidence=raw_confidence,
                engine=self.name,
            )

        return DecisionResponse(answers=answers, engine=self.name, latency_ms=latency_ms, raw=data_dict)
