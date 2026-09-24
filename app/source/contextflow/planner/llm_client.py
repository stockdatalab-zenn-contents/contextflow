"""planner/llm_client.py

Planner が計画生成・自然文構造化のために「LLMを1回呼び出してテキストを返す」処理を、
提供元（provider）ごとに分けて持つ。decision/adapters/ の作り（claude / openai_compat
で実装を分ける）に倣う。

- claude        : anthropic SDK（関数内で遅延 import）。既存 Planner._call_claude の中身そのまま。
- openai_compat : urllib.request のみ。decision/adapters/openai_compat.py の書き方に倣う。

失敗時は理由を問わず None を返す（SDK未導入／APIキー無し／通信失敗／応答が壊れている、など）。
呼び出し側（Planner）はオフライン生成（render_offline_plan）へ退避する既存の挙動を変えない。

接続情報（base_url / APIキー）は `[llm.planner]` を優先し、無ければ `[llm.<provider>]` へ落とす
（`_resolve_base_url` / `_resolve_api_key`）。max_tokens・effort は呼び出し側が解決して渡す（model は resolve_model）。

標準ライブラリのみを使用する（`anthropic` だけは関数内で遅延 import）。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from contextflow.config import AppConfig

# openai_compat の通信タイムアウト秒数（decision/adapters/openai_compat.py に合わせる）
_TIMEOUT_SEC = 60

# call_llm が受け付ける提供元
_PROVIDERS = frozenset({"claude", "openai_compat"})


def call_llm(
    config: AppConfig,
    provider: str,
    *,
    system_prompt: str,
    user_content: str,
    model: str,
    max_tokens: int,
    effort: str,
    json_schema: dict[str, Any] | None = None,
) -> str | None:
    """LLMを1回呼び出しテキストを返す。provider ごとの実装へ振り分けるだけの窓口。

    provider は "claude" / "openai_compat" のみ受け付ける。それ以外は ValueError
    （未知の提供元を黙って Claude へ流さないため。呼び出し側で防ぐのが本筋だが、
    ここでも最後の砦として弾く）。
    通信・認証まわりの失敗は例外にせず None（理由を問わない）。
    """
    # モデル名が解決できないまま送ると、接続先で「不明なモデル」として弾かれる。
    # 他の失敗と同じく None を返し、呼び出し側のオフライン生成へ退避する
    if not model:
        return None
    if provider not in _PROVIDERS:
        raise ValueError(f"未知のPlanner提供元: {provider}（使える値: claude / openai_compat）")
    if provider == "claude":
        return _call_claude(
            config,
            system_prompt=system_prompt,
            user_content=user_content,
            model=model,
            max_tokens=max_tokens,
            effort=effort,
            json_schema=json_schema,
        )
    return _call_openai_compat(
        config,
        system_prompt=system_prompt,
        user_content=user_content,
        model=model,
        max_tokens=max_tokens,
        json_schema=json_schema,
    )


def _resolve_base_url(config: AppConfig, provider: str) -> str:
    """接続先URL。`llm.planner.base_url` → `llm.<provider>.base_url` の順で解決する。"""
    base_url = str(config.get("llm.planner.base_url", "") or "")
    if not base_url:
        base_url = str(config.get(f"llm.{provider}.base_url", "") or "")
    return base_url.rstrip("/")


def resolve_model(config: AppConfig, provider: str) -> str:
    """モデル名。`llm.planner.model` → `llm.<provider>.model` の順で解決する。

    base_url / api_key_env と同じ規則にそろえる。planner 側を空にしておけば、
    提供元を切り替えたときにモデル名も自動で追従する
    （ここを Claude 用の既定値で固定すると、OpenAI互換の接続先へ
    `claude-opus-5` のような無効なモデル名を送ってしまう）。
    """
    model = str(config.get("llm.planner.model", "") or "")
    if not model:
        model = str(config.get(f"llm.{provider}.model", "") or "")
    return model


def _resolve_api_key(config: AppConfig, provider: str) -> str | None:
    """APIキー。`llm.planner.api_key_env` → `llm.<provider>.api_key_env` の順で解決する。

    どちらの参照先も、実際の値の取得は AppConfig.secret（環境変数 → secrets_file の順）に従う。
    """
    return config.secret("llm.planner.api_key_env") or config.secret(f"llm.{provider}.api_key_env")


def _call_claude(
    config: AppConfig,
    *,
    system_prompt: str,
    user_content: str,
    model: str,
    max_tokens: int,
    effort: str,
    json_schema: dict[str, Any] | None,
) -> str | None:
    """Claude API（anthropic SDK）を1回呼び出しテキストを返す。

    旧 Planner._call_claude の中身をそのまま移したもの（判定・組み立て・後処理は変えない）。
    """
    # APIキーの判定を import より先に行う。SDK は環境変数やログイン済みプロファイルから
    # 独自に資格情報を解決するため、ここで止めないと Decision Engine 側
    # （キー未設定なら呼ばない）と挙動が食い違い、意図しない課金につながる
    api_key = _resolve_api_key(config, "claude")
    if not api_key:
        return None

    try:
        import anthropic  # 遅延 import（未インストールでも起動時に落とさない）

        client = anthropic.Anthropic(api_key=api_key)
        output_config: dict[str, Any] = {"effort": effort}
        if json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
            output_config=output_config,
        )
        # stop_reason を確認せずテキストへ進むと、max_tokens 打ち切りの
        # 断片や refusal を正常応答として扱ってしまう。ここで先に弾く
        # （最終的には呼び出し元へ None を返すだけだが、原因を明確に
        # RuntimeError にしてから握りつぶすことで意図を残す）。
        if response.stop_reason == "max_tokens":
            raise RuntimeError(
                "Claudeの応答がmax_tokensで打ち切られた。"
                "config の llm.planner.max_tokens を増やすこと。"
            )
        if response.stop_reason == "refusal":
            stop_details = getattr(response, "stop_details", None)
            category = stop_details.category if stop_details is not None else None
            raise RuntimeError(
                f"Claudeが応答を拒否した（refusal, category={category}）。"
            )
        text = "".join(
            block.text for block in response.content if block.type == "text"
        )
        return text or None
    except Exception:
        return None


def _call_openai_compat(
    config: AppConfig,
    *,
    system_prompt: str,
    user_content: str,
    model: str,
    max_tokens: int,
    json_schema: dict[str, Any] | None,
) -> str | None:
    """OpenAI互換 `{base_url}/chat/completions`（Ollama 等）を1回呼び出しテキストを返す。

    decision/adapters/openai_compat.py の書き方に倣い urllib.request のみで実装する。
    """
    base_url = _resolve_base_url(config, "openai_compat")
    if not base_url:
        return None

    url = f"{base_url}/chat/completions"
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": max_tokens,
    }
    if json_schema is not None:
        # 注意: response_format={"type": "json_object"} は「JSONを返す」ことしか強制しない。
        # json_schema が指定する各フィールドの型・必須項目までは、この経路では強制されない
        # （プロンプト側の指示と、受け取り側の json.loads 失敗時 None 化に頼る）。
        body["response_format"] = {"type": "json_object"}
    data = json.dumps(body).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    # Ollama等はキー不要なので、未設定でもエラーにはせずヘッダを省略する
    api_key = _resolve_api_key(config, "openai_compat")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    http_request = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(http_request, timeout=_TIMEOUT_SEC) as response:
            raw_body = response.read().decode("utf-8")
        response_json = json.loads(raw_body)
        content = response_json["choices"][0]["message"]["content"]
        return content or None
    except (urllib.error.URLError, KeyError, IndexError, TypeError, ValueError):
        return None
