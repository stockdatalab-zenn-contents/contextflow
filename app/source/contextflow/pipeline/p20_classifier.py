"""pipeline/p20_classifier.py

プロセス名・ウィンドウタイトルから activity_type / project を推定する。
categories.toml のルールだけで判定する（LLM は使わない）。

評価順: title_rules（ウィンドウタイトルの部分一致）
      → process_rules（プロセス名の一致）
      → default_activity_type
project は project_rules をタイトル・プロセス名の両方に対して部分一致で判定する。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from contextflow.config import load_categories
from contextflow.contracts.models import ActivityType


class Classifier:
    """categories.toml のルールに基づく単純な分類器。"""

    def __init__(self, categories: dict[str, Any]) -> None:
        self._title_rules: list[dict[str, Any]] = categories.get("title_rules", [])
        self._process_rules: list[dict[str, Any]] = categories.get("process_rules", [])
        self._project_rules: list[dict[str, Any]] = categories.get("project_rules", [])
        self._default = self._to_activity_type(categories.get("default_activity_type", "other"))

    def classify(self, process: str, window_title: str) -> tuple[ActivityType, Optional[str]]:
        """process / window_title から (activity_type, project) を返す。"""
        activity_type = self._classify_activity_type(process, window_title)
        project = self._classify_project(process, window_title)
        return activity_type, project

    def _classify_activity_type(self, process: str, window_title: str) -> ActivityType:
        title_lower = window_title.lower()
        process_lower = process.lower()

        # 1. title_rules: ウィンドウタイトルの部分一致（大文字小文字無視）
        for rule in self._title_rules:
            keywords = rule.get("contains", [])
            if any(keyword.lower() in title_lower for keyword in keywords):
                return self._to_activity_type(rule.get("activity_type", ""))

        # 2. process_rules: プロセス名の一致（大文字小文字無視）
        for rule in self._process_rules:
            candidates = rule.get("match", [])
            if any(candidate.lower() == process_lower for candidate in candidates):
                return self._to_activity_type(rule.get("activity_type", ""))

        # 3. どちらにも該当しなければ既定値
        return self._default

    def _classify_project(self, process: str, window_title: str) -> Optional[str]:
        # タイトル・プロセス名の両方に対する部分一致（大文字小文字無視）
        title_lower = window_title.lower()
        process_lower = process.lower()
        for rule in self._project_rules:
            keywords = rule.get("contains", [])
            for keyword in keywords:
                keyword_lower = keyword.lower()
                if keyword_lower in title_lower or keyword_lower in process_lower:
                    return rule.get("project")
        return None

    @staticmethod
    def _to_activity_type(value: str) -> ActivityType:
        # 未知の activity_type 文字列は例外にせず OTHER にフォールバックする
        try:
            return ActivityType(value)
        except ValueError:
            return ActivityType.OTHER


def load_classifier(path: Path | None = None) -> Classifier:
    """categories.toml を読み込み Classifier を組み立てる。"""
    categories = load_categories(path)
    return Classifier(categories)
