"""自己申告 confidence を実測ベースへ校正する層。

## なぜ校正前の値を閾値判定に使ってはいけないか

Decision Engine（特に汎用 LLM）が返す `raw_confidence` は「自信の強さの自己申告」であって、
実測の的中率ではない。一般に次の歪みが出る。

- **過信**: 「0.9」と言いながら実際に当たるのは 6 割、といったズレが常態化する。
- **不整合**: 同じ 0.8 でも、質問 key やエンジンが変われば意味する的中率が違う。
- **非線形**: 0.6 → 0.7 の差と 0.8 → 0.9 の差が、同じだけ的中率を動かすとは限らない。

そのため `raw_confidence >= 0.8 なら自動実行` のような閾値判定を生値に対して行うと、
「エンジンが強気に出る質問」だけ自動化が暴走し、「弱気に出る質問」は永久に人手へ回る。
閾値は必ず、過去の答え合わせ（`decision_feedback`）から学習した校正後の
`Answer.confidence` に対して掛ける。校正モデルが無い間は恒等変換となり、
生値がそのまま通るが、その状態は「まだ校正できていない」と理解して扱う。

## 構成

- `fit_isotonic` / `fit_platt`: (raw_confidence, 正解か) のサンプル列からモデルを当てはめる。
- `fit_all`: DB のフィードバックを (engine, question_key) 単位で集計し、件数に応じて手法を選ぶ。
- `SqliteCalibrator`: `contracts.decision.Calibrator` の実装。保存済みモデルを引いて適用する。

外部ライブラリ（numpy / scikit-learn）は使わず純 Python で実装する。
noul（yes/no）に限らず choice / score も「正解 / 不正解」の 0/1 に落ちているため、
同じ仕組みでそのまま校正できる。
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from contextflow.storage.db import Database
from contextflow.storage.repositories import CalibrationRepository, FeedbackRepository

# 手法名
METHOD_IDENTITY = "identity"
METHOD_ISOTONIC = "isotonic"
METHOD_PLATT = "platt"

# Platt scaling の既定ハイパーパラメータ（勾配降下）
_PLATT_LR = 0.5
_PLATT_ITERS = 2000
# exp の引数クリップ。これを超えると桁溢れするので手前で止める
_EXP_CLIP = 60.0

# fit_all が platt を選ぶ最低サンプル数
_PLATT_MIN_SAMPLES = 10


# ---------------------------------------------------------------------------
# 小さなヘルパ
# ---------------------------------------------------------------------------


def _to_float(value: Any, default: float = 0.5) -> float:
    """値を float へ寄せる。NaN / inf / 変換不能は default に落とす。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def _clip01(value: float) -> float:
    """0.0〜1.0 に収める。"""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _sigmoid(z: float) -> float:
    """1 / (1 + exp(z))。exp の引数をクリップして発散を防ぐ。"""
    if z > _EXP_CLIP:
        z = _EXP_CLIP
    elif z < -_EXP_CLIP:
        z = -_EXP_CLIP
    return 1.0 / (1.0 + math.exp(z))


def _interpolate(x: float, xs: Sequence[float], ys: Sequence[float]) -> float:
    """区分線形補間。範囲外は端の値でクリップする。"""
    if not xs or not ys:
        return x
    if len(xs) == 1:
        return ys[0]
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    # xs は狭義単調増加。x が入る区間を求めて線形に按分する
    index = bisect_right(list(xs), x) - 1
    index = max(0, min(index, len(xs) - 2))
    x0, x1 = xs[index], xs[index + 1]
    y0, y1 = ys[index], ys[index + 1]
    width = x1 - x0
    if width <= 0.0:
        return y1
    return y0 + (y1 - y0) * (x - x0) / width


def _clean_samples(samples: Sequence[tuple[float, bool]]) -> list[tuple[float, float]]:
    """(raw_confidence, 正解か) を (x:0〜1, y:0.0/1.0) へ正規化する。

    型崩れ・NaN が混じっても落とさず、raw_confidence は 0〜1 にクリップする。
    """
    cleaned: list[tuple[float, float]] = []
    for item in samples:
        if item is None or len(item) < 2:
            continue
        x = _clip01(_to_float(item[0], 0.5))
        y = 1.0 if bool(item[1]) else 0.0
        cleaned.append((x, y))
    return cleaned


# ---------------------------------------------------------------------------
# 校正モデル
# ---------------------------------------------------------------------------


@dataclass
class CalibrationModel:
    """校正関数 1 つ。params は DB に JSON で保存できる素の dict のみ持つ。"""

    method: str = METHOD_IDENTITY
    params: dict = field(default_factory=dict)

    def apply(self, raw_confidence: float) -> float:
        """生の confidence を校正後の値（0.0〜1.0）へ変換する。"""
        x = _clip01(_to_float(raw_confidence, 0.5))
        if self.method == METHOD_ISOTONIC:
            xs = [_to_float(v, 0.0) for v in self.params.get("x") or []]
            ys = [_to_float(v, 0.0) for v in self.params.get("y") or []]
            return _clip01(_interpolate(x, xs, ys))
        if self.method == METHOD_PLATT:
            a = _to_float(self.params.get("a"), 0.0)
            b = _to_float(self.params.get("b"), 0.0)
            return _clip01(_sigmoid(a * x + b))
        # identity / 未知の手法は素通し（校正しない）
        return x

    @classmethod
    def from_dict(cls, row: Optional[dict]) -> "CalibrationModel":
        """CalibrationRepository.get/list が返す dict からモデルを復元する。"""
        if not row:
            return cls(method=METHOD_IDENTITY, params={})
        params = row.get("params")
        if not isinstance(params, dict):
            params = {}
        return cls(method=str(row.get("method") or METHOD_IDENTITY), params=params)


def identity_model() -> CalibrationModel:
    """校正しない恒等モデル。サンプル不足時のフォールバック。"""
    return CalibrationModel(method=METHOD_IDENTITY, params={})


# ---------------------------------------------------------------------------
# 当てはめ: isotonic（PAV）
# ---------------------------------------------------------------------------


def fit_isotonic(samples: Sequence[tuple[float, bool]]) -> CalibrationModel:
    """PAV（Pool Adjacent Violators）で単調非減少な校正曲線を当てはめる。

    手順:
    1. raw_confidence が同値のサンプルをまとめる（x を狭義単調増加にする）。
    2. x 昇順に並べ、正解ラベルの平均が単調非減少になるまで隣接ブロックを併合する。
    3. 各ブロックの左端・右端を区分点として params["x"] / params["y"] に保存する。
    """
    cleaned = _clean_samples(samples)
    if not cleaned:
        return identity_model()

    # 1. 同じ x をまとめる: x -> [正解数, 件数]
    grouped: dict[float, list[float]] = {}
    for x, y in cleaned:
        bucket = grouped.setdefault(x, [0.0, 0.0])
        bucket[0] += y
        bucket[1] += 1.0

    # ブロック: [x_min, x_max, 正解数の合計, 件数の合計]
    blocks: list[list[float]] = []
    for x in sorted(grouped):
        total, count = grouped[x]
        block = [x, x, total, count]
        # 2. 直前ブロックの平均より小さい間は併合し続ける（単調性の回復）
        while blocks and (blocks[-1][2] / blocks[-1][3]) > (block[2] / block[3]):
            prev = blocks.pop()
            block = [prev[0], block[1], prev[2] + block[2], prev[3] + block[3]]
        blocks.append(block)

    # 3. 区分点へ展開。ブロック内は平坦、ブロック間は線形に繋がる
    xs: list[float] = []
    ys: list[float] = []
    for x_min, x_max, total, count in blocks:
        mean = total / count
        xs.append(x_min)
        ys.append(mean)
        if x_max > x_min:
            xs.append(x_max)
            ys.append(mean)

    return CalibrationModel(method=METHOD_ISOTONIC, params={"x": xs, "y": ys})


# ---------------------------------------------------------------------------
# 当てはめ: Platt scaling
# ---------------------------------------------------------------------------


def fit_platt(
    samples: Sequence[tuple[float, bool]],
    *,
    lr: float = _PLATT_LR,
    iters: int = _PLATT_ITERS,
) -> CalibrationModel:
    """シグモイド `1 / (1 + exp(a * x + b))` を勾配降下で当てはめる。

    対数尤度を最大化する向きに a, b を更新する（学習率固定・反復上限あり）。
    a <= 0 に射影しているため、得られる曲線は必ず単調非減少になる
    （自己申告が高いほど校正後も下がらない、という当たり前の性質を守るため）。
    """
    cleaned = _clean_samples(samples)
    if not cleaned:
        return identity_model()

    n = float(len(cleaned))
    a = 0.0
    b = 0.0
    for _ in range(max(1, int(iters))):
        grad_a = 0.0
        grad_b = 0.0
        for x, y in cleaned:
            p = _sigmoid(a * x + b)
            # p = 1/(1+exp(a*x+b)) の対数損失の勾配: dL/da = (y - p) * x, dL/db = (y - p)
            diff = y - p
            grad_a += diff * x
            grad_b += diff
        grad_a /= n
        grad_b /= n
        a -= lr * grad_a
        b -= lr * grad_b
        # 単調非減少を保つため a は 0 以下へ射影する
        if a > 0.0:
            a = 0.0
        # 数値が暴れたら直前の健全な値で打ち切る
        if math.isnan(a) or math.isnan(b) or math.isinf(a) or math.isinf(b):
            return identity_model()
        if abs(grad_a) < 1e-9 and abs(grad_b) < 1e-9:
            break

    return CalibrationModel(method=METHOD_PLATT, params={"a": a, "b": b})


# ---------------------------------------------------------------------------
# DB を使った一括当てはめ
# ---------------------------------------------------------------------------


def _choose_method(count: int, min_samples: int) -> str:
    """サンプル数から手法を選ぶ。足りなければ校正しない。"""
    if count >= min_samples:
        return METHOD_ISOTONIC
    if count >= _PLATT_MIN_SAMPLES:
        return METHOD_PLATT
    return METHOD_IDENTITY


def fit_all(db: Database, *, min_samples: int = 20) -> list[dict]:
    """decision_feedback を (engine, question_key) ごとに集計して校正モデルを更新する。

    - サンプル数 >= min_samples なら isotonic
    - >= 10 なら platt
    - それ未満は identity（校正しない）

    noul だけでなく choice / score も `correct` が 0/1 で入っているため同じ扱いで校正する。
    戻り値は `[{'engine','question_key','method','sample_count'}]`。
    """
    feedback_repo = FeedbackRepository(db)
    calibration_repo = CalibrationRepository(db)

    # (engine, question_key) -> サンプル列
    groups: dict[tuple[str, str], list[tuple[float, bool]]] = {}
    for row in feedback_repo.list():
        engine = str(row.get("engine") or "")
        question_key = str(row.get("question_key") or "")
        if not engine or not question_key:
            continue
        x = _to_float(row.get("raw_confidence"), 0.5)
        correct = bool(row.get("correct"))
        groups.setdefault((engine, question_key), []).append((x, correct))

    results: list[dict] = []
    for (engine, question_key), samples in sorted(groups.items()):
        count = len(samples)
        method = _choose_method(count, min_samples)
        if method == METHOD_ISOTONIC:
            model = fit_isotonic(samples)
        elif method == METHOD_PLATT:
            model = fit_platt(samples)
        else:
            model = identity_model()

        calibration_repo.save(
            engine=engine,
            question_key=question_key,
            method=model.method,
            params=model.params,
            sample_count=count,
        )
        results.append(
            {
                "engine": engine,
                "question_key": question_key,
                "method": model.method,
                "sample_count": count,
            }
        )
    return results


# ---------------------------------------------------------------------------
# Calibrator 実装
# ---------------------------------------------------------------------------


class SqliteCalibrator:
    """`contracts.decision.Calibrator` の実装。calibration_models を引いて適用する。

    毎回 DB を引かないよう、(engine, question_key) 単位でインスタンス内にキャッシュする。
    モデルを再学習した後は `refresh()` でキャッシュを捨てる。
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._repo = CalibrationRepository(db)
        # 「モデル無し」も None として覚え、毎回 DB を叩かないようにする
        self._cache: dict[tuple[str, str], Optional[CalibrationModel]] = {}

    def calibrate(self, engine: str, question_key: str, raw_confidence: float) -> float:
        """自己申告 confidence を校正後の値（0.0〜1.0）へ変換する。"""
        x = _clip01(_to_float(raw_confidence, 0.5))
        model = self._model_for(engine, question_key)
        if model is None:
            return x
        return _clip01(model.apply(x))

    def refresh(self) -> None:
        """キャッシュを破棄する。fit_all の直後に呼ぶ。"""
        self._cache.clear()

    def model_for(self, engine: str, question_key: str) -> Optional[CalibrationModel]:
        """適用されるモデルを返す（デバッグ・確認用）。無ければ None。"""
        return self._model_for(engine, question_key)

    # ------------------------------------------------------------------

    def _model_for(self, engine: str, question_key: str) -> Optional[CalibrationModel]:
        cache_key = (str(engine), str(question_key))
        if cache_key in self._cache:
            return self._cache[cache_key]
        try:
            row = self._repo.get(cache_key[0], cache_key[1])
        except Exception:  # noqa: BLE001 - 校正の失敗で判断を止めない
            row = None
        model = CalibrationModel.from_dict(row) if row else None
        self._cache[cache_key] = model
        return model
