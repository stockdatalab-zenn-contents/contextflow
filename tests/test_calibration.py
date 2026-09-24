"""tests/test_calibration.py

decision/calibration.py のテスト。
DB は tempfile.TemporaryDirectory 上に作り、プロジェクト内（app/data 等）は汚さない。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.conftest_path import add_source_path

add_source_path()

from contextflow.decision.calibration import (  # noqa: E402
    METHOD_IDENTITY,
    METHOD_ISOTONIC,
    SqliteCalibrator,
    fit_all,
    fit_isotonic,
)
from contextflow.storage.db import Database  # noqa: E402
from contextflow.storage.repositories import FeedbackRepository  # noqa: E402


# ---------------------------------------------------------------------------
# fit_isotonic（PAV）
# ---------------------------------------------------------------------------


class TestFitIsotonic(unittest.TestCase):
    """fit_isotonic のテスト。"""

    def test_overconfident_sample_is_pulled_down(self) -> None:
        """自己申告0.9のうち実際に正解したのは6割程度（過信）
        -> apply(0.9) は生値(0.9)より明確に小さくなること。"""
        samples = (
            [(0.5, True)] * 2 + [(0.5, False)] * 3  # x=0.5: 正解率0.4
            + [(0.9, True)] * 6 + [(0.9, False)] * 4  # x=0.9: 正解率0.6（過信）
        )
        model = fit_isotonic(samples)
        calibrated = model.apply(0.9)
        self.assertLess(calibrated, 0.9)
        self.assertLessEqual(calibrated, 0.65)

    def test_monotonic_non_decreasing(self) -> None:
        """x（自己申告値）が増えても y（校正後の値）が減らないこと。"""
        samples = (
            [(0.1, True)] * 1 + [(0.1, False)] * 4
            + [(0.3, True)] * 4 + [(0.3, False)] * 1
            + [(0.5, True)] * 2 + [(0.5, False)] * 3
            + [(0.7, True)] * 3 + [(0.7, False)] * 2
            + [(0.9, True)] * 4 + [(0.9, False)] * 1
        )
        model = fit_isotonic(samples)
        xs = [i / 20 for i in range(21)]  # 0.00, 0.05, ..., 1.00
        ys = [model.apply(x) for x in xs]
        for prev, cur in zip(ys, ys[1:]):
            self.assertLessEqual(prev, cur + 1e-9)

    def test_empty_samples_falls_back_to_identity(self) -> None:
        model = fit_isotonic([])
        self.assertEqual(model.method, METHOD_IDENTITY)
        self.assertEqual(model.apply(0.7), 0.7)


# ---------------------------------------------------------------------------
# fit_all（サンプル数に応じた手法選択）
# ---------------------------------------------------------------------------


class TestFitAll(unittest.TestCase):
    """fit_all: decision_feedback を集計してモデルを選ぶ。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db = Database(Path(self._tmpdir.name) / "calib_test.db")
        self.db.initialize()
        self.addCleanup(self.db.close)

    def _add_feedback(self, question_key: str, raw_confidence: float, correct: bool) -> None:
        FeedbackRepository(self.db).add(
            engine="rule_based",
            question_key=question_key,
            predicted="yes",
            actual="yes" if correct else "no",
            correct=correct,
            raw_confidence=raw_confidence,
        )

    def test_few_samples_choose_identity(self) -> None:
        """サンプルが少ない（min_samples にも platt 最低件数にも満たない）と identity になること。"""
        for i in range(5):
            self._add_feedback("gap_fill", 0.8, i % 2 == 0)
        results = fit_all(self.db, min_samples=20)
        row = next(r for r in results if r["question_key"] == "gap_fill")
        self.assertEqual(row["method"], METHOD_IDENTITY)
        self.assertEqual(row["sample_count"], 5)

    def test_enough_samples_choose_isotonic(self) -> None:
        for _ in range(15):
            self._add_feedback("next_action", 0.9, True)
        for _ in range(10):
            self._add_feedback("next_action", 0.9, False)
        results = fit_all(self.db, min_samples=20)
        row = next(r for r in results if r["question_key"] == "next_action")
        self.assertEqual(row["method"], METHOD_ISOTONIC)
        self.assertEqual(row["sample_count"], 25)


# ---------------------------------------------------------------------------
# SqliteCalibrator
# ---------------------------------------------------------------------------


class TestSqliteCalibrator(unittest.TestCase):
    """SqliteCalibrator: fit_all -> calibrate の往復、未登録は素通し。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.db = Database(Path(self._tmpdir.name) / "calib_test2.db")
        self.db.initialize()
        self.addCleanup(self.db.close)

    def test_unregistered_model_passes_through(self) -> None:
        """校正モデルが未登録の (engine, question_key) は生値をそのまま返す。"""
        calibrator = SqliteCalibrator(self.db)
        self.assertEqual(calibrator.calibrate("rule_based", "unknown_key", 0.73), 0.73)

    def test_fit_all_then_calibrate_round_trip(self) -> None:
        """一時DBへフィードバックを溜めて fit_all -> calibrate まで一巡させる。"""
        feedback_repo = FeedbackRepository(self.db)
        for _ in range(15):
            feedback_repo.add(
                engine="rule_based",
                question_key="gap_fill",
                predicted="yes",
                actual="yes",
                correct=True,
                raw_confidence=0.9,
            )
        for _ in range(10):
            feedback_repo.add(
                engine="rule_based",
                question_key="gap_fill",
                predicted="yes",
                actual="no",
                correct=False,
                raw_confidence=0.9,
            )
        fit_all(self.db, min_samples=20)

        calibrator = SqliteCalibrator(self.db)
        calibrated = calibrator.calibrate("rule_based", "gap_fill", 0.9)
        # 実測正解率0.6のはずなので、生値(0.9)より低くなる
        self.assertLess(calibrated, 0.9)

        # 別の question_key は未登録のまま -> 素通し
        self.assertEqual(calibrator.calibrate("rule_based", "other_key", 0.4), 0.4)

    def test_refresh_clears_cache(self) -> None:
        """fit_all 直後は古いキャッシュのままで、refresh() 後に新しいモデルが反映される。"""
        calibrator = SqliteCalibrator(self.db)
        self.assertIsNone(calibrator.model_for("rule_based", "gap_fill"))  # キャッシュに None が入る

        feedback_repo = FeedbackRepository(self.db)
        for _ in range(12):
            feedback_repo.add(
                engine="rule_based",
                question_key="gap_fill",
                predicted="a",
                actual="a",
                correct=True,
                raw_confidence=0.6,
            )
        fit_all(self.db, min_samples=20)

        # refresh() を呼ばない限り、キャッシュされた None のまま
        self.assertIsNone(calibrator.model_for("rule_based", "gap_fill"))
        calibrator.refresh()
        self.assertIsNotNone(calibrator.model_for("rule_based", "gap_fill"))


if __name__ == "__main__":
    unittest.main()
