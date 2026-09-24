"""config.py の運用方針（mode）切り替えと、decision/registry.py の
エンジン連結（FallbackEngine / create_engine_chain）・planner.py の
オフライン退避の単体テスト。

標準ライブラリの unittest のみを使用する。API は絶対に呼ばない
（課金されるため）。ネットワークアクセスも一切しない。

llm_first / jev_first を試す際は、AppConfig の data に敢えて
[llm]/[github] セクションを含めない。これにより
`config.secret("llm.claude.api_key_env")` は必ず None を返す
（api_key_env そのものが未設定なら、実行環境の ANTHROPIC_API_KEY の
有無に関係なく参照されない）。さらに念のため、Claude Planner 経路
（llm_client._call_claude は api_key 判定より先に `import anthropic` する
ため上と同じ保証が効かない）だけは `sys.modules["anthropic"] = None`
で import 自体を失敗させ、二重に安全側へ倒す。

planner="claude" が実際に呼ばれる場合を確認するテストでは、
`sys.modules["anthropic"]` へ偽モジュール（MagicMock）を差し込み、
`urllib.request.urlopen` は使われないことを確認する。
planner="openai_compat" を確認するテストでは逆に `urllib.request.urlopen`
をモックし、`anthropic` は `sys.modules["anthropic"] = None` で
import させないことで、意図した提供元だけが呼ばれることを保証する。

設定は実ファイル（app/source/config/config.toml）を読んでよいが、
decision.engine などを試す箇所は AppConfig のインスタンスを作って
data を書き換える形にし、実ファイルは絶対に書き換えない。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from tests.conftest_path import add_source_path

add_source_path()

from contextflow.config import AppConfig, load_config, load_modes, resolve_mode  # noqa: E402
from contextflow.contracts.decision import (  # noqa: E402
    Answer,
    DecisionEngine,
    DecisionRequest,
    DecisionResponse,
    Question,
    QuestionType,
)
from contextflow.contracts.models import CurrentState  # noqa: E402
from contextflow.decision.adapters.rule_based import RuleBasedEngine  # noqa: E402
from contextflow.decision.registry import (  # noqa: E402
    FallbackEngine,
    available_modes,
    create_engine_chain,
)
from contextflow.planner.planner import Planner, render_offline_plan  # noqa: E402


def _noul_request(key: str = "continue_current_task") -> DecisionRequest:
    """rule_based が扱える既知 key の、最小の noul 質問リクエスト。"""
    question = Question(key=key, type=QuestionType.NOUL, instruction="dummy")
    return DecisionRequest(state={}, questions=[question])


def _minimal_state() -> CurrentState:
    """Planner のオフライン整形を確認するための最小 CurrentState。"""
    return CurrentState(
        generated_at=datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc),
        target_date=date(2026, 9, 23),
    )


def _minimal_decisions() -> DecisionResponse:
    answer = Answer(
        key="continue_current_task",
        value=True,
        raw_confidence=0.8,
        confidence=0.8,
        engine="rule_based",
    )
    return DecisionResponse(answers={"continue_current_task": answer}, engine="rule_based")


class _AlwaysFailEngine(DecisionEngine):
    """ask() が常に RuntimeError を投げるダミーエンジン（フォールバック検証用）。"""

    name = "always_fail"

    def __init__(self) -> None:
        super().__init__(calibrator=None)

    def _ask(self, request: DecisionRequest) -> DecisionResponse:
        raise RuntimeError("常に失敗するダミーエンジン")


class _DummyCalibrator:
    """confidence を固定値へ変換するだけのテスト用 Calibrator。"""

    def __init__(self, fixed: float) -> None:
        self.fixed = fixed

    def calibrate(self, engine: str, question_key: str, raw_confidence: float) -> float:
        return self.fixed


class LoadModesRealConfigTests(unittest.TestCase):
    """load_modes: config.toml の3方針が読めること（実ファイル読み込みのみ・書き換えなし）。"""

    @classmethod
    def setUpClass(cls):
        cls.config = load_config()
        cls.modes = load_modes(cls.config)

    def test_three_modes_present(self):
        self.assertEqual(set(self.modes), {"rule_first", "llm_first", "jev_first"})

    def test_rule_first_shape(self):
        mode = self.modes["rule_first"]
        self.assertEqual(mode.engines, ["rule_based"])
        self.assertEqual(mode.planner, "offline")
        self.assertFalse(mode.uses_llm_planner)

    def test_llm_first_shape(self):
        mode = self.modes["llm_first"]
        self.assertEqual(mode.engines, ["claude", "rule_based"])
        self.assertEqual(mode.planner, "claude")
        self.assertTrue(mode.uses_llm_planner)

    def test_jev_first_shape(self):
        mode = self.modes["jev_first"]
        self.assertEqual(mode.engines, ["jev", "claude", "rule_based"])
        self.assertEqual(mode.planner, "claude")
        self.assertTrue(mode.uses_llm_planner)


class ResolveModePriorityTests(unittest.TestCase):
    """resolve_mode: 引数 > config の decision.mode > 既定 rule_first。"""

    def test_argument_wins_over_config(self):
        config = AppConfig(data={"policy": {"mode": "llm_first"}})
        mode = resolve_mode(config, "jev_first")
        self.assertEqual(mode.name, "jev_first")

    def test_config_mode_used_when_no_argument(self):
        config = AppConfig(data={"policy": {"mode": "llm_first"}})
        mode = resolve_mode(config)
        self.assertEqual(mode.name, "llm_first")

    def test_default_rule_first_when_nothing_specified(self):
        config = AppConfig(data={})
        mode = resolve_mode(config)
        self.assertEqual(mode.name, "rule_first")


class ResolveModeUnknownNameTests(unittest.TestCase):
    """resolve_mode: 未知の方針名で ValueError（メッセージに選択肢を含む）。"""

    def test_unknown_mode_raises_with_choices(self):
        config = AppConfig(data={})
        with self.assertRaises(ValueError) as ctx:
            resolve_mode(config, "no_such_mode")
        message = str(ctx.exception)
        self.assertIn("no_such_mode", message)
        for name in ("rule_first", "llm_first", "jev_first"):
            self.assertIn(name, message)


class ResolveModeEngineOverrideTests(unittest.TestCase):
    """resolve_mode: decision.engine が非空のとき先頭に来て末尾が rule_based になること。"""

    def test_engine_override_is_prepended_and_ends_with_rule_based(self):
        config = AppConfig(data={"policy": {"mode": "rule_first"}, "decision": {"engine": "claude"}})
        mode = resolve_mode(config)
        self.assertEqual(mode.engines, ["claude", "rule_based"])

    def test_engine_override_rule_based_is_not_duplicated(self):
        config = AppConfig(data={"policy": {"mode": "rule_first"}, "decision": {"engine": "rule_based"}})
        mode = resolve_mode(config)
        self.assertEqual(mode.engines, ["rule_based"])

    def test_explicit_mode_argument_ignores_engine_override(self):
        config = AppConfig(data={"policy": {"mode": "rule_first"}, "decision": {"engine": "claude"}})
        mode = resolve_mode(config, "rule_first")
        self.assertEqual(mode.name, "rule_first")
        self.assertEqual(mode.engines, ["rule_based"])


class AvailableModesTests(unittest.TestCase):
    """available_modes: 3件をソート順で返すこと。"""

    def test_returns_three_sorted_names(self):
        config = AppConfig(data={})
        self.assertEqual(available_modes(config), ["jev_first", "llm_first", "rule_first"])


class CreateEngineChainTests(unittest.TestCase):
    """create_engine_chain: rule_first はそのまま返し、llm_first/jev_first は
    APIキー未設定でも例外にならず rule_based まで退避すること。
    """

    def test_rule_first_returns_rule_based_engine_directly(self):
        config = AppConfig(data={"policy": {"mode": "rule_first"}})
        engine = create_engine_chain(config)
        self.assertIsInstance(engine, RuleBasedEngine)
        self.assertNotIsInstance(engine, FallbackEngine)

    def test_llm_first_falls_back_to_rule_based(self):
        # [llm] セクションを与えないため api_key_env が未設定 = api_key は常に None
        config = AppConfig(data={"policy": {"mode": "llm_first"}})
        chain = create_engine_chain(config)
        self.assertIsInstance(chain, FallbackEngine)
        with mock.patch.dict(sys.modules, {"anthropic": None}):
            response = chain.ask(_noul_request())
        self.assertEqual(response.engine, "rule_based")

    def test_jev_first_falls_back_to_rule_based(self):
        config = AppConfig(data={"policy": {"mode": "jev_first"}})
        chain = create_engine_chain(config)
        self.assertIsInstance(chain, FallbackEngine)
        with mock.patch.dict(sys.modules, {"anthropic": None}):
            response = chain.ask(_noul_request())
        self.assertEqual(response.engine, "rule_based")


class FallbackEngineTests(unittest.TestCase):
    """FallbackEngine: 失敗した子を飛ばして次へ退避し、全滅時は RuntimeError。
    子が校正した confidence は壊さない。
    """

    def test_failing_child_is_skipped(self):
        engine = FallbackEngine([_AlwaysFailEngine(), RuleBasedEngine(calibrator=None, config=None)])
        response = engine.ask(_noul_request())
        self.assertEqual(response.engine, "rule_based")
        self.assertEqual(engine.last_engine, "rule_based")

    def test_all_children_fail_raises_runtime_error(self):
        engine = FallbackEngine([_AlwaysFailEngine(), _AlwaysFailEngine()])
        with self.assertRaises(RuntimeError):
            engine.ask(_noul_request())

    def test_child_calibrated_confidence_is_preserved_through_chain(self):
        calibrator = _DummyCalibrator(fixed=0.81)
        child = RuleBasedEngine(calibrator=calibrator, config=None)

        direct_response = child.ask(_noul_request())
        direct_confidence = direct_response.answers["continue_current_task"].confidence
        self.assertEqual(direct_confidence, 0.81)

        chain = FallbackEngine([_AlwaysFailEngine(), child])
        chain_response = chain.ask(_noul_request())
        chain_confidence = chain_response.answers["continue_current_task"].confidence

        self.assertEqual(chain_confidence, direct_confidence)
        self.assertEqual(chain_response.engine, "rule_based")


class PlannerOfflineFallbackTests(unittest.TestCase):
    """Planner.make_plan: rule_first(offline) は render_offline_plan と同じ文面。
    llm_first でも APIキー未設定なら例外にならずオフライン文面へ落ちること。
    """

    def test_rule_first_matches_render_offline_plan(self):
        config = AppConfig(data={"policy": {"mode": "rule_first"}})
        planner = Planner(config)
        state = _minimal_state()
        decisions = _minimal_decisions()

        text = planner.make_plan(state, decisions)
        expected = render_offline_plan(state, decisions)
        self.assertEqual(text, expected)

    def test_llm_first_without_api_key_falls_back_to_offline_text(self):
        config = AppConfig(data={"policy": {"mode": "llm_first"}})
        planner = Planner(config)
        state = _minimal_state()
        decisions = _minimal_decisions()
        expected = render_offline_plan(state, decisions)

        with mock.patch.dict(sys.modules, {"anthropic": None}):
            text = planner.make_plan(state, decisions)

        self.assertEqual(text, expected)


# ---------------------------------------------------------------------------
# AppConfig.secret: APIキー等をファイル（既定 .env）からも読めるようにした分の単体テスト。
# tempfile.TemporaryDirectory の外には書き込まない（app/data・実ファイルの .env は触らない）。
# 環境変数を触るテストは mock.patch.dict(os.environ, ...) で必ず復元する。
# ---------------------------------------------------------------------------


def _config_with_all_secret_keys(root: Path, secrets_file: str = ".env") -> AppConfig:
    """4箇所の api_key_env / token_env をひととおり持つ AppConfig を作る。"""
    data = {
        "paths": {"secrets_file": secrets_file},
        "llm": {
            "claude": {"api_key_env": "ANTHROPIC_API_KEY"},
            "jev": {"api_key_env": "JEV_API_KEY"},
            "openai_compat": {"api_key_env": "OPENAI_API_KEY"},
        },
        "github": {"token_env": "GITHUB_TOKEN"},
    }
    return AppConfig(data=data, root=root)


class AppConfigSecretFileTests(unittest.TestCase):
    """AppConfig.secret: ファイルからの読み込み・書式の解釈・優先順位を検証する。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_env_file(self, text: str, filename: str = ".env") -> None:
        (self.root / filename).write_text(text, encoding="utf-8")

    def test_reads_value_from_file(self) -> None:
        # 既定の .env（cfg.root 直下）からキーを読めること
        self._write_env_file("ANTHROPIC_API_KEY=sk-ant-from-file\n")
        config = _config_with_all_secret_keys(self.root)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            self.assertEqual(config.secret("llm.claude.api_key_env"), "sk-ant-from-file")

    def test_strips_double_and_single_quotes(self) -> None:
        # 値を囲む " と ' の両方を外すこと
        self._write_env_file(
            'JEV_API_KEY="quoted-value"\n'
            "OPENAI_API_KEY='single-quoted-value'\n"
        )
        config = _config_with_all_secret_keys(self.root)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JEV_API_KEY", None)
            os.environ.pop("OPENAI_API_KEY", None)
            self.assertEqual(config.secret("llm.jev.api_key_env"), "quoted-value")
            self.assertEqual(config.secret("llm.openai_compat.api_key_env"), "single-quoted-value")

    def test_strips_leading_export(self) -> None:
        # シェル用の "export KEY=..." をそのまま貼っても読めること
        self._write_env_file("export OPENAI_API_KEY=exported-value\n")
        config = _config_with_all_secret_keys(self.root)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            self.assertEqual(config.secret("llm.openai_compat.api_key_env"), "exported-value")

    def test_strips_surrounding_whitespace_on_name_and_value(self) -> None:
        # 名前・値の前後の空白を落とすこと（"=" の前後にスペースがあっても読める）
        self._write_env_file("  GITHUB_TOKEN = spaced-value  \n")
        config = _config_with_all_secret_keys(self.root)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_TOKEN", None)
            self.assertEqual(config.secret("github.token_env"), "spaced-value")

    def test_ignores_comment_blank_and_no_equals_lines(self) -> None:
        # "#" 行・空行・"=" の無い行は無視し、有効な行だけ読めること
        self._write_env_file(
            "# コメント行\n"
            "\n"
            "この行には等号が無いので無視される\n"
            "ANTHROPIC_API_KEY=sk-ant-from-file\n"
        )
        config = _config_with_all_secret_keys(self.root)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            self.assertEqual(config.secret("llm.claude.api_key_env"), "sk-ant-from-file")

    def test_environment_variable_takes_priority_over_file(self) -> None:
        # 同名の環境変数があれば、ファイルの値より優先されること
        self._write_env_file("ANTHROPIC_API_KEY=sk-ant-from-file\n")
        config = _config_with_all_secret_keys(self.root)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-from-env"}):
            self.assertEqual(config.secret("llm.claude.api_key_env"), "sk-ant-from-env")

    def test_returns_none_when_file_missing(self) -> None:
        # ファイルが存在しない場合は例外にせず None を返すこと
        config = _config_with_all_secret_keys(self.root)  # .env を書いていない
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            self.assertIsNone(config.secret("llm.claude.api_key_env"))

    def test_secrets_file_path_is_configurable(self) -> None:
        # [paths] secrets_file で場所を変えられること（既定 .env 以外の名前も読める）
        (self.root / "secrets").mkdir()
        self._write_env_file("ANTHROPIC_API_KEY=sk-ant-from-file\n", filename="secrets/creds.env")
        config = _config_with_all_secret_keys(self.root, secrets_file="secrets/creds.env")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            self.assertEqual(config.secret("llm.claude.api_key_env"), "sk-ant-from-file")
            # 既定の .env 側には何も無いので、無関係なキーは None のまま
            self.assertIsNone(config.secret("github.token_env"))

    def test_all_four_api_key_paths_read_from_file(self) -> None:
        # claude / jev / openai_compat / github の4箇所すべてで効くこと
        self._write_env_file(
            "ANTHROPIC_API_KEY=sk-ant-value\n"
            "JEV_API_KEY=jev-value\n"
            "OPENAI_API_KEY=openai-value\n"
            "GITHUB_TOKEN=ghp-value\n"
        )
        config = _config_with_all_secret_keys(self.root)
        env_keys = ("ANTHROPIC_API_KEY", "JEV_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN")
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in env_keys:
                os.environ.pop(key, None)
            self.assertEqual(config.secret("llm.claude.api_key_env"), "sk-ant-value")
            self.assertEqual(config.secret("llm.jev.api_key_env"), "jev-value")
            self.assertEqual(config.secret("llm.openai_compat.api_key_env"), "openai-value")
            self.assertEqual(config.secret("github.token_env"), "ghp-value")


# ---------------------------------------------------------------------------
# Planner の提供元切り替え（llm_client 経由）の単体テスト。
# 実際の通信は一切行わず、anthropic は sys.modules へ偽モジュール、
# openai_compat 側は urllib.request.urlopen をモックして確認する。
# ---------------------------------------------------------------------------


def _make_fake_anthropic_module(response_text: str, calls: list) -> mock.MagicMock:
    """`anthropic.Anthropic(...).messages.create(...)` を模した偽モジュール。

    呼び出し時の kwargs を calls へ記録し、常に response_text を含む応答を返す。
    """
    response = mock.MagicMock()
    response.stop_reason = "end_turn"
    response.content = [mock.MagicMock(type="text", text=response_text)]

    def _create(**kwargs):
        calls.append(kwargs)
        return response

    fake_client = mock.MagicMock()
    fake_client.messages.create.side_effect = _create

    fake_module = mock.MagicMock()
    fake_module.Anthropic.return_value = fake_client
    return fake_module


def _openai_compat_response_body(content: str) -> bytes:
    """{base_url}/chat/completions の応答本体（JSON）を組み立てる。"""
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")


class PlannerClaudeProviderTests(unittest.TestCase):
    """make_plan: planner="claude" かつキー有りのとき anthropic SDK が呼ばれること。"""

    def test_calls_anthropic_sdk_when_key_present(self) -> None:
        config = AppConfig(
            data={
                "policy": {"mode": "llm_first"},
                "llm": {"claude": {"model": "claude-opus-5", "api_key_env": "ANTHROPIC_API_KEY"}},
            }
        )
        planner = Planner(config)
        state = _minimal_state()
        decisions = _minimal_decisions()

        calls: list = []
        fake_module = _make_fake_anthropic_module("# 計画\n- ダミー", calls)

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-dummy"}), mock.patch.dict(
            sys.modules, {"anthropic": fake_module}
        ), mock.patch("urllib.request.urlopen", side_effect=AssertionError("network access attempted")):
            text = planner.make_plan(state, decisions)

        self.assertEqual(text, "# 計画\n- ダミー")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], "claude-opus-5")
        fake_module.Anthropic.assert_called_once_with(api_key="sk-ant-dummy")


class PlannerOpenAICompatProviderTests(unittest.TestCase):
    """make_plan: planner="openai_compat" のとき {base_url}/chat/completions へ POST されること。"""

    def _config(self, *, planner_base_url: str = "") -> AppConfig:
        llm: dict = {"openai_compat": {"model": "qwen2.5:14b", "base_url": "http://localhost:11434/v1", "api_key_env": "OPENAI_API_KEY"}}
        if planner_base_url:
            llm["planner"] = {"base_url": planner_base_url}
        return AppConfig(
            data={
                "policy": {
                    "mode": "custom_openai",
                    "modes": {
                        "custom_openai": {"engines": ["rule_based"], "planner": "openai_compat"},
                    },
                },
                "llm": llm,
            }
        )

    def test_posts_to_chat_completions_with_auth_header(self) -> None:
        config = self._config()
        planner = Planner(config)
        state = _minimal_state()
        decisions = _minimal_decisions()
        body = _openai_compat_response_body("# 計画\n- openai_compat")

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-oc-dummy"}), mock.patch(
            "urllib.request.urlopen"
        ) as mock_urlopen, mock.patch.dict(sys.modules, {"anthropic": None}):
            mock_urlopen.return_value.__enter__.return_value.read.return_value = body
            text = planner.make_plan(state, decisions)

        self.assertEqual(text, "# 計画\n- openai_compat")
        self.assertEqual(mock_urlopen.call_count, 1)
        request = mock_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:11434/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer sk-oc-dummy")

    def test_base_url_prefers_llm_planner_over_llm_openai_compat(self) -> None:
        # [llm.planner] base_url が指定されていれば、そちらが [llm.openai_compat] base_url より優先される
        config = self._config(planner_base_url="http://planner-host:9999/v1")
        planner = Planner(config)
        state = _minimal_state()
        decisions = _minimal_decisions()
        body = _openai_compat_response_body("# 計画\n- planner-host")

        with mock.patch.dict(os.environ, {}, clear=False), mock.patch(
            "urllib.request.urlopen"
        ) as mock_urlopen, mock.patch.dict(sys.modules, {"anthropic": None}):
            mock_urlopen.return_value.__enter__.return_value.read.return_value = body
            planner.make_plan(state, decisions)

        request = mock_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://planner-host:9999/v1/chat/completions")


class ParseActivityTextProviderTests(unittest.TestCase):
    """parse_activity_text: 運用方針の planner に応じて提供元が切り替わること。"""

    def test_follows_openai_compat_provider(self) -> None:
        config = AppConfig(
            data={
                "policy": {
                    "mode": "custom_openai",
                    "modes": {"custom_openai": {"engines": ["rule_based"], "planner": "openai_compat"}},
                },
                "llm": {"openai_compat": {"model": "qwen2.5:14b", "base_url": "http://localhost:11434/v1"}},
            }
        )
        planner = Planner(config)

        payload = {
            "start": "13:00",
            "end": "13:45",
            "activity_type": "meeting",
            "project": None,
            "task": None,
            "summary": "打ち合わせ",
        }
        body = _openai_compat_response_body(json.dumps(payload, ensure_ascii=False))

        with mock.patch("urllib.request.urlopen") as mock_urlopen, mock.patch.dict(
            sys.modules, {"anthropic": None}
        ):
            mock_urlopen.return_value.__enter__.return_value.read.return_value = body
            activity = planner.parse_activity_text(
                "13時から45分、打ち合わせをした", base_date=date(2026, 9, 23)
            )

        self.assertIsNotNone(activity)
        self.assertEqual(activity.summary, "打ち合わせ")
        request = mock_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:11434/v1/chat/completions")

    def test_offline_mode_returns_none_without_network(self) -> None:
        config = AppConfig(data={"policy": {"mode": "rule_first"}})
        planner = Planner(config)

        with mock.patch(
            "urllib.request.urlopen", side_effect=AssertionError("network access attempted")
        ), mock.patch.dict(sys.modules, {"anthropic": None}):
            activity = planner.parse_activity_text("13時から45分、打ち合わせをした")

        self.assertIsNone(activity)


class MovedConfigKeyTests(unittest.TestCase):
    """運用方針の設定を [decision] から [policy] へ移した件の回帰テスト。

    互換読み替えはしない。ただし黙って既定へ戻すと書き換え漏れに気づけないため、
    旧キーが書かれていたら ValueError で知らせる。
    """

    def test_old_mode_key_raises(self) -> None:
        config = AppConfig(data={"decision": {"mode": "llm_first"}})
        with self.assertRaises(ValueError) as ctx:
            resolve_mode(config)
        self.assertIn("[policy] mode", str(ctx.exception))

    def test_old_modes_key_raises(self) -> None:
        config = AppConfig(data={"decision": {"modes": {"x": {}}}})
        with self.assertRaises(ValueError) as ctx:
            load_modes(config)
        self.assertIn("[policy] modes", str(ctx.exception))

    def test_new_keys_work(self) -> None:
        config = AppConfig(
            data={
                "policy": {
                    "mode": "custom",
                    "modes": {"custom": {"engines": ["rule_based"], "planner": "offline"}},
                }
            }
        )
        self.assertEqual(resolve_mode(config).name, "custom")

    def test_decision_keeps_its_own_settings(self) -> None:
        """[decision] に残した判断固有のキーは、そのまま読めること。"""
        config = AppConfig(
            data={"policy": {"mode": "rule_first"}, "decision": {"confidence_threshold": 0.9}}
        )
        self.assertEqual(resolve_mode(config).name, "rule_first")
        self.assertEqual(config.get("decision.confidence_threshold"), 0.9)


class ResolveModeUnknownPlannerTests(unittest.TestCase):
    """load_modes / resolve_mode: 未知の planner は ValueError（黙って claude へ流さない）。"""

    def _config_with_unknown_planner(self) -> AppConfig:
        return AppConfig(
            data={
                "policy": {
                    "mode": "gpt_mode",
                    "modes": {"gpt_mode": {"engines": ["rule_based"], "planner": "gpt"}},
                },
            }
        )

    def test_load_modes_raises_with_choices(self) -> None:
        config = self._config_with_unknown_planner()
        with self.assertRaises(ValueError) as ctx:
            load_modes(config)
        message = str(ctx.exception)
        self.assertIn("gpt", message)
        for provider in ("offline", "claude", "openai_compat"):
            self.assertIn(provider, message)

    def test_resolve_mode_raises_too(self) -> None:
        config = self._config_with_unknown_planner()
        with self.assertRaises(ValueError):
            resolve_mode(config)

    def test_planner_falls_back_to_offline_instead_of_calling_claude(self) -> None:
        # Planner 側は resolve_mode の ValueError を捕まえてオフラインへ倒す（claudeは呼ばない）
        config = self._config_with_unknown_planner()
        planner = Planner(config)
        state = _minimal_state()
        decisions = _minimal_decisions()

        with mock.patch(
            "urllib.request.urlopen", side_effect=AssertionError("network access attempted")
        ), mock.patch.dict(sys.modules, {"anthropic": None}):
            text = planner.make_plan(state, decisions)

        self.assertEqual(text, render_offline_plan(state, decisions))


class UsesLlmPlannerProviderTests(unittest.TestCase):
    """ModeConfig.uses_llm_planner: offline=False / claude=True / openai_compat=True。"""

    def test_offline_is_false(self) -> None:
        config = AppConfig(data={"policy": {"mode": "rule_first"}})
        self.assertFalse(resolve_mode(config).uses_llm_planner)

    def test_claude_is_true(self) -> None:
        config = AppConfig(data={"policy": {"mode": "llm_first"}})
        self.assertTrue(resolve_mode(config).uses_llm_planner)

    def test_openai_compat_is_true(self) -> None:
        config = AppConfig(
            data={
                "policy": {
                    "mode": "custom_openai",
                    "modes": {"custom_openai": {"engines": ["rule_based"], "planner": "openai_compat"}},
                },
            }
        )
        self.assertTrue(resolve_mode(config).uses_llm_planner)


if __name__ == "__main__":
    unittest.main()
