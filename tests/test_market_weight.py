"""The fitted market weight is what keeps reported edges honest."""

import numpy as np
import pandas as pd

from nflpicker.ml.train import fit_market_weight


def _frame(model_err_sd, market_err_sd, n=3000, seed=1):
    """Simulate a season where truth is known, so the right answer is known too."""
    rng = np.random.default_rng(seed)
    true_margin = rng.normal(0, 6, n)
    actual = true_margin + rng.normal(0, 13.2, n)
    return pd.DataFrame({
        "pred_margin": true_margin + rng.normal(0, model_err_sd, n),
        "market_margin": true_margin + rng.normal(0, market_err_sd, n),
        "margin_home": actual,
    })


def test_a_model_worse_than_the_market_yields_a_high_weight():
    """This is the case that matters: without it, a weak model claims large
    edges on exactly the games where it is most wrong."""
    w, _ = fit_market_weight(_frame(model_err_sd=6.0, market_err_sd=1.0),
                             "pred_margin", "market_margin", "margin_home")
    assert w > 0.85


def test_a_model_better_than_the_market_yields_a_low_weight():
    w, _ = fit_market_weight(_frame(model_err_sd=1.0, market_err_sd=6.0),
                             "pred_margin", "market_margin", "margin_home")
    assert w < 0.35


def test_equally_good_estimators_split_the_difference():
    w, _ = fit_market_weight(_frame(model_err_sd=3.0, market_err_sd=3.0),
                             "pred_margin", "market_margin", "margin_home")
    assert 0.35 < w < 0.65


def test_the_blend_is_at_least_as_accurate_as_either_input():
    frame = _frame(model_err_sd=4.0, market_err_sd=3.0)
    w, blend_sd = fit_market_weight(frame, "pred_margin", "market_margin", "margin_home")
    model_sd = float(np.std(frame["margin_home"] - frame["pred_margin"]))
    market_sd = float(np.std(frame["margin_home"] - frame["market_margin"]))
    assert blend_sd <= min(model_sd, market_sd) + 1e-6


def test_too_little_data_falls_back_rather_than_overfitting():
    w, _ = fit_market_weight(_frame(1.0, 6.0, n=20), "pred_margin", "market_margin",
                             "margin_home")
    assert w == 0.65


def test_weight_is_clamped_into_a_usable_range():
    # Even a hopeless model never goes past the cap, so some model signal
    # always survives and the app keeps showing its own number.
    w, _ = fit_market_weight(_frame(model_err_sd=40.0, market_err_sd=0.2),
                             "pred_margin", "market_margin", "margin_home")
    assert 0.30 <= w <= 0.98
