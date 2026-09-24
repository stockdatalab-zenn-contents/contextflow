"""設定の読み込み。

設定は TOML（標準ライブラリ tomllib で読む）。追加インストール不要。
config.toml / categories.toml / questions.toml の3本を扱う。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from contextflow.contracts.decision import Question, QuestionType

# このファイルは app/source/contextflow/config.py にある
_SOURCE_DIR = Path(__file__).resolve().parent.parent      # app/source
_PROJECT_ROOT = _SOURCE_DIR.parent.parent                 # プロジェクトルート
CONFIG_DIR = _SOURCE_DIR / "config"


def project_root() -> Path:
    """プロジェクトルート（app/ の親）。"""
    return _PROJECT_ROOT


@dataclass
class AppConfig:
    """config.toml の内容と、解決済みパスを保持する。"""

    data: dict[str, Any] = field(default_factory=dict)
    root: Path = field(default_factory=project_root)
    config_dir: Path = CONFIG_DIR
    # 秘密情報ファイルの読み込み結果。None は「まだ読んでいない」
    _secrets_cache: Optional[dict[str, str]] = field(default=None, repr=False, compare=False)

    def get(self, dotted_key: str, default: Any = None) -> Any:
        """'llm.claude.model' のようなドット区切りで値を取得。"""
        node: Any = self.data
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path(self, key: str) -> Path:
        """paths セクションの値を絶対パスとして返す。"""
        value = self.get(f"paths.{key}")
        if not value:
            raise KeyError(f"paths.{key} が未設定")
        candidate = Path(value)
        return candidate if candidate.is_absolute() else (self.root / candidate)

    def sibling(self, filename: str) -> Path:
        """config.toml と同じ場所にある設定ファイルのパスを返す。

        `--config` で config.toml だけを別の場所へ置いた場合でも動くよう、
        隣に無ければ同梱の `app/source/config/` を見る。
        """
        candidate = self.config_dir / filename
        if candidate.is_file():
            return candidate
        return CONFIG_DIR / filename

    def secret(self, env_key_path: str) -> Optional[str]:
        """'llm.claude.api_key_env' のような設定から APIキー等の値を取得。

        探す順は次のとおり。どちらも無ければ None（キーが無くても落とさない）。

        1. 同名の環境変数
        2. 秘密情報ファイル（`[paths] secrets_file`。既定は `.env`）

        環境変数を先に見るのは、一時的な上書き（別のキーで試す・CI で渡す）を
        効かせるため。ファイルへ書いておけば環境変数の設定は要らない。
        """
        env_name = self.get(env_key_path)
        if not env_name:
            return None
        from_env = os.environ.get(env_name)
        if from_env:
            return from_env
        return self._secrets().get(env_name)

    def _secrets(self) -> dict[str, str]:
        """秘密情報ファイルを読む（1度読んだら保持する）。

        ファイルが無い・読めない場合は空のまま返す。設定漏れで落とさないため。
        """
        if self._secrets_cache is None:
            self._secrets_cache = _read_secrets_file(self._secrets_path())
        return self._secrets_cache

    def _secrets_path(self) -> Path:
        """秘密情報ファイルの場所。未設定なら プロジェクトルート直下の `.env`。"""
        value = self.get("paths.secrets_file") or ".env"
        candidate = Path(value)
        return candidate if candidate.is_absolute() else (self.root / candidate)


def _read_secrets_file(path: Path) -> dict[str, str]:
    """`KEY=VALUE` 形式の秘密情報ファイルを読む。

    - `#` で始まる行と空行は無視する
    - 先頭の `export ` は取り除く（シェル用の書き方をそのまま貼れるように）
    - 値を囲む `"` `'` は取り除く
    - 値は**ログに出さない**。読めなければ空の dict を返す

    tomllib を使わないのは、この形式が最も貼り付けやすく、
    `.gitignore` の慣例（`.env`）とも合うため。
    """
    result: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return result
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("\"", "'"):
            value = value[1:-1]
        if name:
            result[name] = value
    return result


@dataclass
class ModeConfig:
    """運用方針。判断エンジンの並びと、Planner の担当を1組にしたもの。"""

    name: str
    description: str = ""
    engines: list[str] = field(default_factory=lambda: ["rule_based"])
    planner: str = "offline"  # "claude" か "offline"

    @property
    def uses_llm_planner(self) -> bool:
        return self.planner != "offline"


# config.toml に [decision.modes.*] が無い場合の既定値
DEFAULT_MODES: dict[str, ModeConfig] = {
    "rule_first": ModeConfig(
        name="rule_first",
        description="ルールベース先行。費用ゼロで運用し、feedback を貯めてから LLM へ移行する",
        engines=["rule_based"],
        planner="offline",
    ),
    "llm_first": ModeConfig(
        name="llm_first",
        description="判断も Claude に任せる",
        engines=["claude", "rule_based"],
        planner="claude",
    ),
    "jev_first": ModeConfig(
        name="jev_first",
        description="Jev で小さな判断、LLM は説明・計画",
        engines=["jev", "claude", "rule_based"],
        planner="claude",
    ),
}


def load_modes(config: AppConfig) -> dict[str, ModeConfig]:
    """config の [decision.modes.*] を ModeConfig へ変換する。"""
    raw = config.get("decision.modes") or {}
    if not isinstance(raw, dict) or not raw:
        return dict(DEFAULT_MODES)
    modes: dict[str, ModeConfig] = {}
    for name, body in raw.items():
        engines = [str(e) for e in body.get("engines", []) if str(e)]
        modes[name] = ModeConfig(
            name=name,
            description=str(body.get("description", "")),
            engines=engines or ["rule_based"],
            planner=str(body.get("planner", "offline")),
        )
    return modes


def resolve_mode(config: AppConfig, name: Optional[str] = None) -> ModeConfig:
    """使用する運用方針を決める。

    優先順位は 引数 > config の decision.mode > rule_first。
    decision.engine が明示されている場合は、そのエンジンを先頭に置いた一時的な方針を返す
    （最後は必ず rule_based へ退避し、判断が止まらないようにする）。
    """
    modes = load_modes(config)
    selected = name or config.get("decision.mode") or "rule_first"
    if selected not in modes:
        available = " / ".join(sorted(modes))
        raise ValueError(f"未知の運用方針: {selected}（選択肢: {available}）")
    mode = modes[selected]

    override = (config.get("decision.engine") or "").strip() if name is None else ""
    if override:
        engines = [override] if override == "rule_based" else [override, "rule_based"]
        return ModeConfig(
            name=f"{mode.name}+engine={override}",
            description=f"decision.engine で {override} を直接指定",
            engines=engines,
            planner=mode.planner,
        )
    return mode


def load_config(path: Optional[Path] = None) -> AppConfig:
    """config.toml を読み込む。"""
    target = Path(path) if path else CONFIG_DIR / "config.toml"
    data = tomllib.loads(target.read_text(encoding="utf-8"))
    return AppConfig(data=data, config_dir=target.parent)


def load_categories(path: Optional[Path] = None) -> dict[str, Any]:
    """categories.toml（分類ルール）を読み込む。"""
    target = Path(path) if path else CONFIG_DIR / "categories.toml"
    return tomllib.loads(target.read_text(encoding="utf-8"))


def load_question_sets(path: Optional[Path] = None) -> dict[str, list[Question]]:
    """questions.toml を Question のリストへ変換。"""
    target = Path(path) if path else CONFIG_DIR / "questions.toml"
    raw = tomllib.loads(target.read_text(encoding="utf-8"))
    sets: dict[str, list[Question]] = {}
    for set_name, body in raw.items():
        questions: list[Question] = []
        for item in body.get("questions", []):
            question = Question(
                key=item["key"],
                type=QuestionType(item["type"]),
                instruction=item.get("instruction", ""),
                choices=list(item.get("choices", [])),
                min=int(item.get("min", 1)),
                max=int(item.get("max", 5)),
            )
            question.validate()
            questions.append(question)
        sets[set_name] = questions
    return sets
