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
（Planner._call_claude は api_key 判定より先に `import anthropic` する
ため上と同じ保証が効かない）だけは `sys.modules["anthropic"] = None`
で import 自体を失敗させ、二重に安全側へ倒す。

設定は実ファイル（app/source/config/config.toml）を読んでよいが、
decision.engine などを試す箇所は AppConfig のインスタンスを作って
data を書き換える形にし、実ファイルは絶対に書き換えない。
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timezone
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
        config = AppConfig(data={"decision": {"mode": "llm_first"}})
        mode = resolve_mode(config, "jev_first")
        self.assertEqual(mode.name, "jev_first")

    def test_config_mode_used_when_no_argument(self):
        config = AppConfig(data={"decision": {"mode": "llm_first"}})
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
        config = AppConfig(data={"decision": {"mode": "rule_first", "engine": "claude"}})
        mode = resolve_mode(config)
        self.assertEqual(mode.engines, ["claude", "rule_based"])

    def test_engine_override_rule_based_is_not_duplicated(self):
        config = AppConfig(data={"decision": {"mode": "rule_first", "engine": "rule_based"}})
        mode = resolve_mode(config)
        self.assertEqual(mode.engines, ["rule_based"])

    def test_explicit_mode_argument_ignores_engine_override(self):
        config = AppConfig(data={"decision": {"mode": "rule_first", "engine": "claude"}})
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
        config = AppConfig(data={"decision": {"mode": "rule_first"}})
        engine = create_engine_chain(config)
        self.assertIsInstance(engine, RuleBasedEngine)
        self.assertNotIsInstance(engine, FallbackEngine)

    def test_llm_first_falls_back_to_rule_based(self):
        # [llm] セクションを与えないため api_key_env が未設定 = api_key は常に None
        config = AppConfig(data={"decision": {"mode": "llm_first"}})
        chain = create_engine_chain(config)
        self.assertIsInstance(chain, FallbackEngine)
        with mock.patch.dict(sys.modules, {"anthropic": None}):
            response = chain.ask(_noul_request())
        self.assertEqual(response.engine, "rule_based")

    def test_jev_first_falls_back_to_rule_based(self):
        config = AppConfig(data={"decision": {"mode": "jev_first"}})
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
        config = AppConfig(data={"decision": {"mode": "rule_first"}})
        planner = Planner(config)
        state = _minimal_state()
        decisions = _minimal_decisions()

        text = planner.make_plan(state, decisions)
        expected = render_offline_plan(state, decisions)
        self.assertEqual(text, expected)

    def test_llm_first_without_api_key_falls_back_to_offline_text(self):
        config = AppConfig(data={"decision": {"mode": "llm_first"}})
        planner = Planner(config)
        state = _minimal_state()
        decisions = _minimal_decisions()
        expected = render_offline_plan(state, decisions)

        with mock.patch.dict(sys.modules, {"anthropic": None}):
            text = planner.make_plan(state, decisions)

        self.assertEqual(text, expected)


if __name__ == "__main__":
    unittest.main()
