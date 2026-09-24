"""Decision Engine の生成レジストリ。

engine 名から Adapter を組み立てる。rule_based 以外の Adapter と
SqliteCalibrator は未実装・未インストールでも registry の import が壊れないよう
関数の中で遅延 import する。
"""

from __future__ import annotations

import sys
from typing import Optional

from contextflow.config import AppConfig, load_modes, resolve_mode
from contextflow.contracts.decision import (
    Calibrator,
    DecisionEngine,
    DecisionRequest,
    DecisionResponse,
)
from contextflow.decision.adapters.rule_based import RuleBasedEngine
from contextflow.storage.db import Database

_ENGINE_NAMES = ["rule_based", "claude", "openai_compat", "jev"]


def available_engines() -> list[str]:
    """利用可能な Decision Engine 名の一覧。"""
    return list(_ENGINE_NAMES)


def available_modes(config: AppConfig) -> list[str]:
    """利用可能な運用方針名の一覧（ソート済み）。"""
    return sorted(load_modes(config))


class FallbackEngine(DecisionEngine):
    """複数エンジンを順に試し、最初に成功したものの結果を返す。

    子エンジンの ask（検証・実行・confidence校正まで含む）をそのまま呼び、
    結果を加工せず返す。ここで _ask を上書きしてしまうと、基底クラスの ask が
    もう一度校正を行い、子エンジンで校正済みの confidence が生の値で
    上書きされてしまうため、必ず ask を上書きする。
    """

    name = "fallback"

    def __init__(self, engines: list[DecisionEngine]) -> None:
        # 校正は子エンジン側でそれぞれ完結しているため親では持たない
        super().__init__(calibrator=None)
        self.engines = engines
        self.last_engine: Optional[str] = None

    def ask(self, request: DecisionRequest) -> DecisionResponse:
        """子エンジンを順に試し、最初に成功した ask の結果をそのまま返す。"""
        last_error: Optional[Exception] = None
        for engine in self.engines:
            try:
                response = engine.ask(request)
            except RuntimeError as exc:
                print(
                    f"警告: {engine.name} が使えないため次のエンジンへ退避: {exc}",
                    file=sys.stderr,
                )
                last_error = exc
                continue
            self.last_engine = engine.name
            return response
        raise RuntimeError(f"すべての Decision Engine が失敗: {last_error}") from last_error

    def _ask(self, request: DecisionRequest) -> DecisionResponse:
        # ask を上書きしているため、ここが呼ばれることは無い
        raise RuntimeError("FallbackEngine._ask は直接呼ばれない（ask を使うこと）")


def _build_calibrator(db: Optional[Database]) -> Optional[Calibrator]:
    """db があれば SqliteCalibrator を返す。未実装なら calibrator=None で続行。"""
    if db is None:
        return None
    try:
        from contextflow.decision.calibration import SqliteCalibrator

        return SqliteCalibrator(db)
    except Exception as exc:  # noqa: BLE001 - 未実装でも動作を止めない
        print(
            f"警告: SqliteCalibrator の読み込みに失敗したため校正無しで続行: {exc}",
            file=sys.stderr,
        )
        return None


def _build_engine(
    name: str, config: AppConfig, calibrator: Optional[Calibrator]
) -> DecisionEngine:
    """名前から Adapter を1つ組み立てる。claude/openai_compat/jev は遅延 import。"""
    if name == "rule_based":
        return RuleBasedEngine(calibrator=calibrator, config=config)
    if name == "claude":
        from contextflow.decision.adapters.claude_api import ClaudeDecisionEngine

        return ClaudeDecisionEngine(config=config, calibrator=calibrator)
    if name == "openai_compat":
        from contextflow.decision.adapters.openai_compat import OpenAICompatEngine

        return OpenAICompatEngine(config=config, calibrator=calibrator)
    if name == "jev":
        from contextflow.decision.adapters.jev import JevEngine

        return JevEngine(config=config, calibrator=calibrator)
    raise ValueError(f"未知の Decision Engine 名: {name}")


def create_engine_chain(
    config: AppConfig, db: Optional[Database] = None, mode: Optional[str] = None
) -> DecisionEngine:
    """運用方針（mode）に従って、複数の Decision Engine を束ねて生成する。

    resolve_mode で決めた engines を順にインスタンス化する。生成に失敗したものは
    警告を出してチェーンから落とすだけで、rule_based への自動差し替えはしない
    （最終的に rule_based まで落ちなかった場合、答えが返らないこともあり得る）。
    生成できたものが0個なら RuleBasedEngine を返し、1個ならそのまま返し、
    2個以上なら FallbackEngine で束ねる。
    """
    mode_config = resolve_mode(config, mode)
    calibrator = _build_calibrator(db)

    engines: list[DecisionEngine] = []
    for engine_name in mode_config.engines:
        try:
            engines.append(_build_engine(engine_name, config, calibrator))
        except Exception as exc:  # noqa: BLE001 - チェーンから落として続行するため広く捕捉
            print(
                f"警告: {engine_name} の生成に失敗したためチェーンから除外: {exc}",
                file=sys.stderr,
            )

    if not engines:
        return RuleBasedEngine(calibrator=calibrator, config=config)
    if len(engines) == 1:
        return engines[0]
    return FallbackEngine(engines)


def create_engine(
    config: AppConfig, db: Optional[Database] = None, name: Optional[str] = None
) -> DecisionEngine:
    """設定（と任意の DB）から Decision Engine を生成する。

    name 未指定時は config.get("decision.engine")、それも無ければ現在の運用方針
    （resolve_mode）の先頭エンジン。指定エンジンの生成に失敗した場合は
    rule_based へフォールバックする。
    """
    if name:
        engine_name = name
    else:
        configured = config.get("decision.engine") or ""
        engine_name = configured or resolve_mode(config).engines[0]
    if engine_name not in _ENGINE_NAMES:
        raise ValueError(f"未知の Decision Engine 名: {engine_name}")

    calibrator = _build_calibrator(db)

    try:
        return _build_engine(engine_name, config, calibrator)
    except Exception as exc:  # noqa: BLE001 - フォールバックのため広く捕捉
        print(
            f"警告: Decision Engine '{engine_name}' の生成に失敗したため rule_based へ切替: {exc}",
            file=sys.stderr,
        )
        return RuleBasedEngine(calibrator=calibrator, config=config)
