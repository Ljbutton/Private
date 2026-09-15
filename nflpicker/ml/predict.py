"""Inference: turn features + market into a projection for one game.

Three numbers per game, and keeping them distinct is the whole point:

* ``model_margin`` — **market-blind**.  It never sees the line, so comparing it
  to the line is a real disagreement rather than a rounding error.  This is the
  number to show a human as "what our model thinks".
* ``fair_margin`` — the precision-weighted blend of model and market.  This is
  our actual best estimate of the true margin.
* ``spread_edge`` — ``fair_margin`` against the line, **not** ``model_margin``
  against the line.

That last point is the one that is easy to get wrong.  The raw disagreement
between a model and a sharp market is not an edge you can bet: both are noisy
estimates of the same unknown, and the market's is usually the more precise of
the two.  If the model says +7 and the line says 0, the honest conclusion is
"the truth is probably around +2.5", not "there are seven points of value here".
Quoting the raw disagreement as the edge inflates every expected value on the
board and produces confident recommendations on exactly the games where the
model is most likely to be wrong.  So the actionable edge is the shrunk one,
and the raw disagreement is kept beside it as a diagnostic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import get_config
from ..ratings.power import PowerRatings
from ..util import MARGIN_SD, TOTAL_SD, margin_to_win_prob, over_prob
from .features import MARKET_FEATURES, NUMERIC_FEATURES

# Fallback weight for the market in the blended "fair" number, used only until
# a trained bundle supplies one.  The real value is *fitted* on walk-forward
# predictions (see ``ml/train.fit_market_weight``): a model that turns out to be
# worse than the closing line gets a weight near 1 and stops claiming edges it
# has not earned.
DEFAULT_MARKET_WEIGHT = 0.65

# When no trained model exists, power ratings carry the projection alone.
FALLBACK_NOTE = "no trained model; using power ratings"

# Margin spread to assume when no trained model exists. Wider than MARGIN_SD
# because that constant describes scatter around a *perfect* projection, and an
# untrained power rating is not one.
UNTRAINED_SD = 14.5

# Games each side must have played before the model's opinion is worth acting
# on.  Before that, ratings are still close to their priors, so any apparent
# disagreement with the market is the model's ignorance rather than an edge.
INFORMATION_FULL_AT = 5.0


log = logging.getLogger("nflpicker.predict")


@dataclass
class GamePrediction:
    game_id: str
    home: str
    away: str
    model_margin: float        # market-blind projected home margin
    model_total: float
    fair_margin: float         # model blended with the market
    fair_total: float
    home_win_prob: float       # from fair_margin, calibrated
    market_spread: float | None = None
    market_total: float | None = None
    spread_edge: float | None = None       # actionable: fair_margin vs the line
    total_edge: float | None = None
    raw_spread_edge: float | None = None   # diagnostic: model_margin vs the line
    raw_total_edge: float | None = None
    home_cover_prob: float | None = None
    over_probability: float | None = None
    information: float = 1.0   # 0-1: how much data backs this projection
    components: dict = field(default_factory=dict)

    def to_row(self) -> dict:
        return {
            "game_id": self.game_id,
            "home": self.home,
            "away": self.away,
            "model_margin": self.model_margin,
            "model_total": self.model_total,
            "fair_margin": self.fair_margin,
            "fair_total": self.fair_total,
            "home_win_prob": self.home_win_prob,
            "market_spread": self.market_spread,
            "market_total": self.market_total,
            "spread_edge": self.spread_edge,
            "total_edge": self.total_edge,
            "raw_spread_edge": self.raw_spread_edge,
            "raw_total_edge": self.raw_total_edge,
            "home_cover_prob": self.home_cover_prob,
            "over_probability": self.over_probability,
            "information": self.information,
            "components": self.components,
        }


class Predictor:
    """Wraps the trained bundle, degrading to power ratings when absent."""

    def __init__(self, bundle: dict | None = None, model_dir: Path | None = None) -> None:
        self.bundle = bundle if bundle is not None else load_bundle(model_dir)
        b = self.bundle or {}
        self.residual_sd = float(b.get("residual_sd") or MARGIN_SD)
        # SD of the *blended* estimate, which is what win probabilities use.
        self.blend_sd = float(b.get("blend_sd") or 0) or self.residual_sd
        self.market_weight = float(b.get("market_weight") or DEFAULT_MARKET_WEIGHT)
        self.market_weight_total = float(
            b.get("market_weight_total") or self.market_weight
        )

    @property
    def trained(self) -> bool:
        return bool(self.bundle and self.bundle.get("margin_blind") is not None)

    @property
    def version(self) -> str:
        if not self.trained:
            return "power-only"
        return f"{self.bundle.get('model_version', '?')}@{self.bundle.get('trained_at', '?')[:10]}"

    # ------------------------------------------------------------------ core
    def predict_frame(
        self,
        frame: pd.DataFrame,
        power: PowerRatings | None = None,
        *,
        market_weight: float | None = None,
        adjustments: dict[str, float] | None = None,
    ) -> list[GamePrediction]:
        market_weight = self.market_weight if market_weight is None else market_weight
        total_weight = (
            self.market_weight_total if market_weight is None else market_weight
        )
        if frame is None or frame.empty:
            return []

        n = len(frame)
        power_margin = np.array(
            [
                power.margin(r["home"], r["away"], neutral_site=bool(r.get("neutral_site")))
                if power else np.nan
                for r in frame.to_dict("records")
            ],
            dtype=float,
        )
        power_total = np.array(
            [power.total(r["home"], r["away"]) if power else np.nan
             for r in frame.to_dict("records")],
            dtype=float,
        )

        if self.trained:
            ml_margin = _predict(self.bundle["margin_blind"], frame, n)
            ml_total = _predict(self.bundle.get("total_blind"), frame, n)
            raw_prob = _proba(self.bundle.get("win_blind"), frame, n)
        else:
            ml_margin = np.full(n, np.nan)
            ml_total = np.full(n, np.nan)
            raw_prob = np.full(n, np.nan)

        # Market-blind projection: ML where available, power ratings otherwise,
        # and an even blend when we have both (they err differently).
        model_margin = np.where(
            np.isnan(ml_margin), power_margin,
            np.where(np.isnan(power_margin), ml_margin, 0.6 * ml_margin + 0.4 * power_margin),
        )
        model_total = np.where(
            np.isnan(ml_total), power_total,
            np.where(np.isnan(power_total), ml_total, 0.6 * ml_total + 0.4 * power_total),
        )
        model_margin = np.nan_to_num(model_margin, nan=0.0)
        model_total = np.where(np.isnan(model_total), 44.0, model_total)

        # Availability adjustment. Injury reports are not in the training data,
        # so this is applied to the projection rather than learned. It mostly
        # *removes* false disagreement: a model that has not noticed a
        # ruled-out starter will claim its largest edge on the game it
        # understands least.
        if adjustments:
            home_adj = np.array(
                [adjustments.get(r["home"], 0.0) for r in frame.to_dict("records")],
                dtype=float,
            )
            away_adj = np.array(
                [adjustments.get(r["away"], 0.0) for r in frame.to_dict("records")],
                dtype=float,
            )
            model_margin = model_margin + home_adj - away_adj

        # How much has actually been observed about these two teams?  Both the
        # games they have played and whether a trained model exists count.
        played = np.minimum(
            frame.reindex(columns=["games_played_home"])["games_played_home"].to_numpy(dtype=float),
            frame.reindex(columns=["games_played_away"])["games_played_away"].to_numpy(dtype=float),
        )
        information = np.clip(np.nan_to_num(played) / INFORMATION_FULL_AT, 0.0, 1.0)
        if not self.trained:
            information = information * 0.7

        market_spread = frame.reindex(columns=["spread_home"])["spread_home"].to_numpy(dtype=float)
        market_total = frame.reindex(columns=["market_total"])["market_total"].to_numpy(dtype=float)
        market_margin = -market_spread

        # A data-starved model defers to the market rather than fighting it:
        # the effective market weight rises to 1.0 when we know nothing.
        effective_weight = market_weight + (1.0 - market_weight) * (1.0 - information)
        has_line = ~np.isnan(market_margin)
        fair_margin = np.where(
            has_line,
            (1 - effective_weight) * model_margin
            + effective_weight * np.nan_to_num(market_margin),
            model_margin,
        )
        effective_total_weight = total_weight + (1.0 - total_weight) * (1.0 - information)
        has_total_line = ~np.isnan(market_total)
        fair_total = np.where(
            has_total_line,
            (1 - effective_total_weight) * model_total
            + effective_total_weight * np.nan_to_num(market_total),
            model_total,
        )

        # The spread of actual margins around *our* projection, which is wider
        # than the spread around a perfect one. When trained, residual_sd is
        # measured out-of-sample and already includes model error; untrained, add
        # an allowance for it rather than pretending the projection is exact.
        sd = self.blend_sd if self.trained else UNTRAINED_SD
        prob = margin_to_win_prob(fair_margin, sd=sd)
        calibrator = (self.bundle or {}).get("calibrator")
        if calibrator is not None and not np.isnan(raw_prob).all():
            calibrated = calibrator.predict(np.nan_to_num(raw_prob, nan=0.5))
            # The classifier is market-blind, so how much it may move the number
            # has to obey the same fitted weight the margin blend uses. A fixed
            # share here would quietly contradict a high market weight and put
            # the disagreement straight back into moneyline prices, where a few
            # points of probability on a longshot become a huge apparent edge.
            classifier_share = 0.30 * (1.0 - market_weight)
            blended = (1.0 - classifier_share) * prob + classifier_share * calibrated
            prob = np.where(np.isnan(raw_prob), prob, blended)
        prob = np.clip(prob, 0.01, 0.99)

        results: list[GamePrediction] = []
        for i, row in enumerate(frame.to_dict("records")):
            spread = None if np.isnan(market_spread[i]) else float(market_spread[i])
            total_line = None if np.isnan(market_total[i]) else float(market_total[i])
            # Actionable edge uses the blended estimate; the raw disagreement is
            # reported alongside it but never bet on directly.
            spread_edge = None if spread is None else float(fair_margin[i] + spread)
            total_edge = None if total_line is None else float(fair_total[i] - total_line)
            raw_spread_edge = None if spread is None else float(model_margin[i] + spread)
            raw_total_edge = None if total_line is None else float(model_total[i] - total_line)
            results.append(
                GamePrediction(
                    game_id=str(row.get("game_id")),
                    home=row["home"],
                    away=row["away"],
                    model_margin=float(model_margin[i]),
                    model_total=float(model_total[i]),
                    fair_margin=float(fair_margin[i]),
                    fair_total=float(fair_total[i]),
                    home_win_prob=float(prob[i]),
                    market_spread=spread,
                    market_total=total_line,
                    spread_edge=spread_edge,
                    total_edge=total_edge,
                    raw_spread_edge=raw_spread_edge,
                    raw_total_edge=raw_total_edge,
                    home_cover_prob=(
                        None if spread is None
                        else float(margin_to_win_prob(fair_margin[i] + spread, sd=sd))
                    ),
                    over_probability=(
                        None if total_line is None
                        else float(over_prob(float(fair_total[i]), total_line, sd=TOTAL_SD))
                    ),
                    information=round(float(information[i]), 3),
                    components={
                        "ml_margin": _opt(ml_margin[i]),
                        "power_margin": _opt(power_margin[i]),
                        "market_margin": _opt(market_margin[i]),
                        "ml_total": _opt(ml_total[i]),
                        "power_total": _opt(power_total[i]),
                        "raw_win_prob": _opt(raw_prob[i]),
                        "market_weight": round(float(effective_weight[i]), 3)
                        if spread is not None else 0.0,
                        "availability_home": round(
                            float((adjustments or {}).get(row["home"], 0.0)), 2),
                        "availability_away": round(
                            float((adjustments or {}).get(row["away"], 0.0)), 2),
                        "source": "model" if self.trained else FALLBACK_NOTE,
                    },
                )
            )
        return results


def _model_columns(model) -> list[str]:
    """The exact columns a model was fitted on, recorded at training time."""
    return list(getattr(model, "feature_names_used_", None) or NUMERIC_FEATURES)


def _predict(model, frame: pd.DataFrame, n: int) -> np.ndarray:
    if model is None:
        return np.full(n, np.nan)
    return np.asarray(model.predict(frame.reindex(columns=_model_columns(model))), dtype=float)


def _proba(model, frame: pd.DataFrame, n: int) -> np.ndarray:
    if model is None:
        return np.full(n, np.nan)
    proba = model.predict_proba(frame.reindex(columns=_model_columns(model)))
    return np.asarray(proba[:, 1], dtype=float)


def _opt(value) -> float | None:
    value = float(value)
    return None if np.isnan(value) else round(value, 3)


def load_bundle(model_dir: Path | None = None) -> dict | None:
    path = (Path(model_dir) if model_dir else get_config().model_dir) / "models.joblib"
    if not path.exists():
        return None
    try:
        import joblib

        return joblib.load(path)
    except Exception as exc:  # noqa: BLE001 - a stale artifact must not brick the app
        # Falling back to power ratings is right; doing it silently is not. A
        # bundle pickled under a different scikit-learn is the likely cause, and
        # the only symptom would otherwise be the sidebar quietly reading
        # "power-only" with no way to tell that from never having trained.
        log.warning(
            "could not load %s (%s: %s) — falling back to power ratings; "
            "retrain with `nflpicker train` to rebuild it for this environment",
            path, type(exc).__name__, exc,
        )
        return None


def market_features_present(frame: pd.DataFrame) -> bool:
    return bool(frame is not None and not frame.empty
                and frame.reindex(columns=MARKET_FEATURES).notna().any().any())
