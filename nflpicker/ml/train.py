"""Model training and walk-forward evaluation.

Two deliberate design choices drive this file.

1. **Walk-forward only.** Models are validated by training on every season
   before season *S* and testing on *S*, rolling forward.  Random k-fold on
   sports data leaks the future into the past through the rolling features and
   produces flattering, meaningless scores.

2. **Two model variants.** A *market-blind* model never sees the betting line;
   a *market-aware* one does.  A market-aware model rapidly learns to echo the
   closing line, so using it to measure "our edge versus the line" is circular
   and the edge collapses to zero.  The displayed edge therefore comes from the
   blind model.  The aware model is kept because it is the better pure
   predictor, and it is used for calibration and the blended projection.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.isotonic import IsotonicRegression

from ..config import get_config
from ..util import MARGIN_SD, now_iso
from .features import MARKET_FEATURES, NUMERIC_FEATURES

MODEL_VERSION = "1.0"
MIN_TRAIN_GAMES = 400

REGRESSOR_PARAMS = dict(
    loss="absolute_error",      # margins are heavy-tailed; MAE resists blowouts
    max_iter=400,
    learning_rate=0.05,
    max_depth=5,
    min_samples_leaf=40,
    l2_regularization=1.0,
    early_stopping=True,
    validation_fraction=0.15,
    random_state=7,
)

CLASSIFIER_PARAMS = dict(
    max_iter=350,
    learning_rate=0.05,
    max_depth=4,
    min_samples_leaf=40,
    l2_regularization=1.0,
    early_stopping=True,
    validation_fraction=0.15,
    random_state=7,
)


@dataclass
class Metrics:
    n: int = 0
    margin_mae: float | None = None
    margin_rmse: float | None = None
    market_margin_mae: float | None = None
    total_mae: float | None = None
    market_total_mae: float | None = None
    ats_picks: int = 0
    ats_wins: int = 0
    ats_rate: float | None = None
    su_rate: float | None = None
    brier: float | None = None
    log_loss: float | None = None


@dataclass
class TrainingReport:
    trained_at: str = field(default_factory=now_iso)
    model_version: str = MODEL_VERSION
    n_games: int = 0
    seasons: list[int] = field(default_factory=list)
    blind: Metrics = field(default_factory=Metrics)
    aware: Metrics = field(default_factory=Metrics)
    residual_sd: float = MARGIN_SD
    market_weight: float = 0.65
    market_weight_total: float = 0.65
    blend_sd: float = MARGIN_SD
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "blind": asdict(self.blind),
            "aware": asdict(self.aware),
        }


def usable_features(frame: pd.DataFrame, features: list[str]) -> list[str]:
    """Drop columns the booster cannot bin.

    A column that is entirely missing or holds a single distinct value carries
    no signal and makes the histogram binner raise.  This is not a corner case:
    in Week 1 there is no EPA history and often no posted total, so several
    columns are legitimately empty.
    """
    keep: list[str] = []
    for col in features:
        if col not in frame.columns:
            continue
        series = pd.to_numeric(frame[col], errors="coerce").dropna()
        if series.nunique() >= 2:
            keep.append(col)
    return keep


def _fit_regressor(frame: pd.DataFrame, features: list[str], target: str):
    usable = frame[frame[target].notna()]
    cols = usable_features(usable, features)
    if len(usable) < 50 or not cols:
        return None
    model = HistGradientBoostingRegressor(**REGRESSOR_PARAMS)
    model.fit(usable[cols], usable[target])
    model.feature_names_used_ = cols
    return model


def _fit_classifier(frame: pd.DataFrame, features: list[str]):
    usable = frame[frame["home_win"].notna() & (frame["home_win"] != 0.5)]
    cols = usable_features(usable, features)
    if len(usable) < 50 or not cols:
        return None
    model = HistGradientBoostingClassifier(**CLASSIFIER_PARAMS)
    model.fit(usable[cols], usable["home_win"].astype(int))
    model.feature_names_used_ = cols
    return model


def _predict_with(model, frame: pd.DataFrame) -> np.ndarray:
    """Predict using exactly the columns a model was fitted on."""
    cols = getattr(model, "feature_names_used_", None) or list(frame.columns)
    return model.predict(frame.reindex(columns=cols))


def _proba_with(model, frame: pd.DataFrame) -> np.ndarray:
    cols = getattr(model, "feature_names_used_", None) or list(frame.columns)
    return model.predict_proba(frame.reindex(columns=cols))[:, 1]


def evaluate(
    frame: pd.DataFrame,
    *,
    include_market: bool,
    min_train: int = MIN_TRAIN_GAMES,
) -> tuple[Metrics, pd.DataFrame]:
    """Walk-forward evaluation, one season at a time.

    Returns the pooled metrics and the out-of-sample predictions, which are what
    the probability calibrator is later fitted on.
    """
    features = NUMERIC_FEATURES + (MARKET_FEATURES if include_market else [])
    seasons = sorted(frame["season"].dropna().unique())
    records: list[pd.DataFrame] = []

    for season in seasons:
        train = frame[frame["season"] < season]
        test = frame[(frame["season"] == season) & frame["margin_home"].notna()]
        if len(train) < min_train or test.empty:
            continue
        margin_model = _fit_regressor(train, features, "margin_home")
        total_model = _fit_regressor(train, features, "total_points")
        win_model = _fit_classifier(train, features)
        if margin_model is None:
            continue

        out = test[["game_id", "season", "week", "home", "away", "margin_home",
                    "total_points", "home_win", "spread_home", "market_total"]].copy()
        out["pred_margin"] = _predict_with(margin_model, test)
        out["pred_total"] = (
            _predict_with(total_model, test) if total_model is not None else np.nan
        )
        out["pred_home_prob"] = (
            _proba_with(win_model, test) if win_model is not None else np.nan
        )
        records.append(out)

    if not records:
        return Metrics(), pd.DataFrame()

    preds = pd.concat(records, ignore_index=True)
    return _score(preds), preds


def _score(preds: pd.DataFrame) -> Metrics:
    m = Metrics(n=len(preds))
    err = preds["pred_margin"] - preds["margin_home"]
    m.margin_mae = float(np.abs(err).mean())
    m.margin_rmse = float(np.sqrt((err**2).mean()))

    has_line = preds["spread_home"].notna()
    if has_line.any():
        # The market's own implied margin is -spread_home; this is the bar to beat.
        market_err = (-preds.loc[has_line, "spread_home"]) - preds.loc[has_line, "margin_home"]
        m.market_margin_mae = float(np.abs(market_err).mean())

        # ATS: bet the side our margin disagrees with the line about.
        sub = preds[has_line].copy()
        sub["edge"] = sub["pred_margin"] + sub["spread_home"]
        sub["result"] = sub["margin_home"] + sub["spread_home"]
        graded = sub[(sub["edge"].abs() >= 0.5) & (sub["result"] != 0)]
        if not graded.empty:
            wins = ((graded["edge"] > 0) == (graded["result"] > 0)).sum()
            m.ats_picks = int(len(graded))
            m.ats_wins = int(wins)
            m.ats_rate = float(wins / len(graded))

    if preds["pred_total"].notna().any():
        mask = preds["pred_total"].notna() & preds["total_points"].notna()
        m.total_mae = float(np.abs(preds.loc[mask, "pred_total"] - preds.loc[mask, "total_points"]).mean())
        line = mask & preds["market_total"].notna()
        if line.any():
            m.market_total_mae = float(
                np.abs(preds.loc[line, "market_total"] - preds.loc[line, "total_points"]).mean()
            )

    prob = preds["pred_home_prob"]
    decided = preds["home_win"].isin([0.0, 1.0]) & prob.notna()
    if decided.any():
        p = prob[decided].clip(1e-6, 1 - 1e-6)
        y = preds.loc[decided, "home_win"]
        m.brier = float(((p - y) ** 2).mean())
        m.log_loss = float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())
        m.su_rate = float((((p > 0.5).astype(float)) == y).mean())
    return m


def train(
    frame: pd.DataFrame,
    *,
    model_dir: Path | None = None,
    evaluate_first: bool = True,
) -> TrainingReport:
    """Fit final models on all available history and persist them."""
    import joblib

    cfg = get_config()
    model_dir = Path(model_dir) if model_dir else cfg.model_dir
    model_dir.mkdir(parents=True, exist_ok=True)

    completed = frame[frame["margin_home"].notna()].copy()
    report = TrainingReport(
        n_games=int(len(completed)),
        seasons=sorted(int(s) for s in completed["season"].dropna().unique()),
    )
    if len(completed) < MIN_TRAIN_GAMES:
        report.notes.append(
            f"only {len(completed)} completed games; need {MIN_TRAIN_GAMES} for a "
            "trustworthy model. Predictions fall back to power ratings."
        )
        return report

    calibrator = None
    if evaluate_first:
        report.blind, blind_preds = evaluate(completed, include_market=False)
        report.aware, _ = evaluate(completed, include_market=True)
        if not blind_preds.empty:
            resid = blind_preds["pred_margin"] - blind_preds["margin_home"]
            report.residual_sd = float(resid.std()) or MARGIN_SD
            calibrator = _fit_calibrator(blind_preds)

            # Market implied margin is the negation of the posted home line.
            preds_with_market = blind_preds.assign(
                market_margin=-blind_preds["spread_home"]
            )
            report.market_weight, report.blend_sd = fit_market_weight(
                preds_with_market, "pred_margin", "market_margin", "margin_home"
            )
            report.market_weight_total, _ = fit_market_weight(
                blind_preds, "pred_total", "market_total", "total_points"
            )
            if report.market_weight >= 0.9:
                report.notes.append(
                    f"Fitted market weight is {report.market_weight:.2f}: on this data the "
                    "closing line carries almost all the information and the model adds "
                    "little. Edges will be small, which is the honest result."
                )

    blind_features = NUMERIC_FEATURES
    aware_features = NUMERIC_FEATURES + MARKET_FEATURES
    bundle = {
        "model_version": MODEL_VERSION,
        "trained_at": report.trained_at,
        "blind_features": blind_features,
        "aware_features": aware_features,
        "residual_sd": report.residual_sd,
        "market_weight": report.market_weight,
        "market_weight_total": report.market_weight_total,
        "blend_sd": report.blend_sd,
        "margin_blind": _fit_regressor(completed, blind_features, "margin_home"),
        "total_blind": _fit_regressor(completed, blind_features, "total_points"),
        "win_blind": _fit_classifier(completed, blind_features),
        "margin_aware": _fit_regressor(completed, aware_features, "margin_home"),
        "total_aware": _fit_regressor(completed, aware_features, "total_points"),
        "calibrator": calibrator,
    }
    joblib.dump(bundle, model_dir / "models.joblib")
    (model_dir / "training_report.json").write_text(json.dumps(report.to_dict(), indent=2))
    return report


def fit_market_weight(
    preds: pd.DataFrame,
    model_col: str,
    market_col: str,
    actual_col: str,
    *,
    lo: float = 0.30,
    hi: float = 0.98,
) -> tuple[float, float]:
    """Fit how much to trust the market versus the model, from real results.

    This weight must not be a guessed constant.  It decides the size of every
    edge the app reports: too low and a model that is *worse* than the closing
    line will claim large edges on the games where it is most wrong, which is
    precisely the failure mode that makes model-versus-market dashboards
    dangerous.

    So it is fitted.  Minimising the squared error of
    ``(1 - w) * model + w * market`` against the actual result is a one
    dimensional least squares with a closed form: writing ``d = market - model``
    and ``e = actual - model``, the optimum is ``w = Σ(d·e) / Σ(d²)``.  If the
    model adds nothing over the market the fit drives w toward 1 on its own,
    and the reported edges collapse toward zero — which is the correct answer.

    Returns the weight and the residual SD of the blended estimate.
    """
    usable = preds[[model_col, market_col, actual_col]].dropna()
    if len(usable) < 100:
        return 0.65, MARGIN_SD
    model = usable[model_col].to_numpy(dtype=float)
    market = usable[market_col].to_numpy(dtype=float)
    actual = usable[actual_col].to_numpy(dtype=float)
    d = market - model
    e = actual - model
    denom = float(np.sum(d * d))
    if denom <= 1e-9:
        return 0.65, float(np.std(actual - model)) or MARGIN_SD
    w = float(np.sum(d * e) / denom)
    w = float(min(hi, max(lo, w)))
    blended = (1.0 - w) * model + w * market
    return w, float(np.std(actual - blended)) or MARGIN_SD


def _fit_calibrator(preds: pd.DataFrame):
    """Isotonic map from raw classifier probability to observed frequency.

    Gradient boosting is usually over-confident at the tails; this is what makes
    a stated 72% actually win about 72% of the time, which matters because every
    Kelly stake and survivor decision downstream trusts that number.
    """
    decided = preds[preds["home_win"].isin([0.0, 1.0]) & preds["pred_home_prob"].notna()]
    if len(decided) < 200:
        return None
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.01, y_max=0.99)
    iso.fit(decided["pred_home_prob"], decided["home_win"])
    return iso


def load_report(model_dir: Path | None = None) -> dict | None:
    path = (Path(model_dir) if model_dir else get_config().model_dir) / "training_report.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None
