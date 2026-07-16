"""Dynamic scoring function for Xueqiu influencers (大V).

Five weighted dimensions, each normalized to ``[0, 1]`` so no single metric
dominates by raw magnitude. Missing dimensions (fetch failure) are excluded
and the weights are re-normalized over the remaining dimensions — except
``performance``, where a total absence of portfolios is scored as 0 (a
penalty) rather than dropped, because "no skin in the game" is itself a
negative signal for a finance influencer.

Design notes — see ``influencer_RESEARCH.md`` for the full rationale. The
headline points:

* ``influence`` uses ``log10(followers)``: a 10x fan gap should be worth a
  roughly fixed increment, not a linear blowout.
* ``engagement`` is an interaction *rate* (likes ÷ estimated impressions),
  which de-ranks zombie-fan accounts.
* ``performance`` weights annualized return, Sharpe, and Calmar equally —
  penalizing both low return and high drawdown.
* Default weights put ``performance`` at 0.35 (the largest single weight)
  because verified P&L is the hardest signal to fake.
"""

from __future__ import annotations

import math
from typing import Dict, Mapping, Optional

from src.influencer.models import ScoreBreakdown

# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

DEFAULT_WEIGHTS: Dict[str, float] = {
    "influence": 0.20,
    "engagement": 0.20,
    "originality": 0.15,
    "performance": 0.35,
    "activity": 0.10,
}

assert abs(sum(DEFAULT_WEIGHTS.values()) - 1.0) < 1e-9, "default weights must sum to 1"

# Per-dimension saturation anchors. These map a raw metric to a [0,1] score.
# A "saturation" value is the point at which the dimension scores ~1.0; values
# below are scaled linearly (or via log), values above are clipped. Tunable.
_ANCHORS = {
    # followers at which `influence` saturates to 1.0 (1M)
    "influence_followers_saturation": 1_000_000,
    # engagement rate (likes / impressions) at which `engagement` saturates.
    # Xueqiu's platform average is ~2.3%; 5% is "good", 10% is elite.
    "engagement_rate_saturation": 0.10,
    "originality_long_post_ratio_saturation": 0.50,  # half of posts are long-form
    "performance_annual_return_saturation": 0.30,   # 30% annualized = max score
    "performance_sharpe_saturation": 2.0,
    "performance_calmar_saturation": 3.0,
    "activity_posts_per_30d_saturation": 30,        # ~1 post/day
}


# ---------------------------------------------------------------------------
# Normalization primitives
# ---------------------------------------------------------------------------


def _clip01(x: float) -> float:
    """Clamp to ``[0, 1]``; NaN maps to NaN (propagates, handled by caller)."""
    if math.isnan(x):
        return float("nan")
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    return x


def _ratio_saturation(value: float, saturation: float) -> float:
    """Linear ``value / saturation`` clipped to [0,1]. NaN stays NaN."""
    if math.isnan(value) or saturation <= 0:
        return float("nan")
    return _clip01(value / saturation)


# ---------------------------------------------------------------------------
# Per-dimension scorers
# ---------------------------------------------------------------------------


def score_influence(followers: float) -> float:
    """log10-compressed follower score in [0,1].

    1k followers → ~0.50, 10k → ~0.67, 100k → ~0.83, 1M → 1.0 (saturation).
    log compression prevents megastars from drowning out everyone else.
    """
    if math.isnan(followers) or followers <= 0:
        return float("nan")
    sat = _ANCHORS["influence_followers_saturation"]
    # log10(followers) / log10(saturation), clipped.
    return _clip01(math.log10(max(followers, 1.0)) / math.log10(sat))


def score_engagement(avg_likes: float, followers: float) -> float:
    """Interaction *rate* score in [0,1].

    Impressions are estimated as ``followers * 0.1`` (a 10% reach assumption —
    conservative). The rate ``avg_likes / impressions`` is then scaled against
    the platform-elite anchor. This deliberately de-ranks accounts whose high
    like counts come purely from a huge follower base with low per-fan reach.
    """
    if math.isnan(avg_likes) or math.isnan(followers) or followers <= 0:
        return float("nan")
    impressions = followers * 0.1
    rate = avg_likes / impressions
    return _ratio_saturation(rate, _ANCHORS["engagement_rate_saturation"])


def score_originality(original_ratio: float, long_post_ratio: float) -> float:
    """Blend of original-post ratio and long-form ratio, each in [0,1].

    Args:
        original_ratio: Fraction of recent posts that are original (not
            reposts). Expected in [0,1].
        long_post_ratio: Fraction of recent posts that are long-form. Expected
            in [0,1]; 0.5 saturates.
    """
    if math.isnan(original_ratio) or math.isnan(long_post_ratio):
        return float("nan")
    original_part = _clip01(original_ratio)
    long_part = _ratio_saturation(long_post_ratio, _ANCHORS["originality_long_post_ratio_saturation"])
    return 0.5 * original_part + 0.5 * long_part


def score_performance(
    annual_return: float,
    sharpe: float,
    max_drawdown: float,
) -> float:
    """Risk-adjusted performance score in [0,1].

    Combines annualized return, Sharpe ratio, and Calmar ratio (return ÷
    abs(drawdown)) with equal sub-weights. A *missing* portfolio (all NaN) is
    handled by the caller (:func:`score_bigv`) as a 0 penalty, not here.

    Args:
        annual_return: Annualized return as a fraction (0.20 = 20%).
        sharpe: Annualized Sharpe ratio.
        max_drawdown: Max drawdown as a non-positive fraction (-0.30 = -30%).
    """
    has_any = not all(math.isnan(v) for v in (annual_return, sharpe, max_drawdown))
    if not has_any:
        return float("nan")

    parts = []
    # annual return: 30% saturates; negative returns clip to 0.
    if not math.isnan(annual_return):
        parts.append(_ratio_saturation(max(annual_return, 0.0), _ANCHORS["performance_annual_return_saturation"]))
    # sharpe: 2.0 saturates.
    if not math.isnan(sharpe):
        parts.append(_ratio_saturation(sharpe, _ANCHORS["performance_sharpe_saturation"]))
    # calmar: return / abs(drawdown); 3.0 saturates. NaN if no drawdown data.
    if not math.isnan(annual_return) and not math.isnan(max_drawdown) and max_drawdown < 0:
        calmar = annual_return / abs(max_drawdown)
        parts.append(_ratio_saturation(calmar, _ANCHORS["performance_calmar_saturation"]))
    if not parts:
        return float("nan")
    return sum(parts) / len(parts)


def score_activity(posts_30d: float) -> float:
    """Posting frequency score in [0,1]. 30 posts/30d (~1/day) saturates."""
    if math.isnan(posts_30d) or posts_30d < 0:
        return float("nan")
    return _ratio_saturation(posts_30d, _ANCHORS["activity_posts_per_30d_saturation"])


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

# Keys the metrics dict is expected to expose. Anything missing is treated as
# NaN for that dimension. See :class:`InfluencerMetrics` in datasource.py for
# the canonical producer.
_REQUIRED_METRIC_KEYS = (
    "followers",
    "avg_likes_30d",
    "original_ratio",
    "long_post_ratio",
    "posts_30d",
    "annual_return",
    "sharpe",
    "max_drawdown",
    "has_portfolio",
)


def _get(metrics: Mapping[str, float], key: str) -> float:
    """Fetch a numeric metric, returning NaN if absent or non-numeric."""
    val = metrics.get(key)
    if val is None:
        return float("nan")
    try:
        return float(val)
    except (TypeError, ValueError):
        return float("nan")


def score_bigv(
    metrics: Mapping[str, float],
    weights: Optional[Mapping[str, float]] = None,
) -> ScoreBreakdown:
    """Score an influencer on five weighted dimensions.

    Args:
        metrics: Mapping with keys such as ``followers``, ``avg_likes_30d``,
            ``original_ratio``, ``long_post_ratio``, ``posts_30d``,
            ``annual_return``, ``sharpe``, ``max_drawdown``, ``has_portfolio``.
            Missing keys are treated as NaN (dimension unavailable).
        weights: Optional weight override. Defaults to :data:`DEFAULT_WEIGHTS`.
            Need not sum to 1 — re-normalized over available dimensions.

    Returns:
        A :class:`ScoreBreakdown` with the weighted total, per-dimension
        components, the effective (re-normalized) weights, and the list of
        dimensions that were unavailable.

    Notes:
        * ``performance`` is special: if ``has_portfolio`` is falsy or all
          performance fields are NaN, the dimension scores **0** (penalty) and
          is *included* in the weighting — "no track record" should lower the
          total, not be silently excused.
        * Other dimensions, when NaN, are excluded and their weight mass is
          redistributed across the available dimensions, so the total still
          lands in ``[0, 1]``.
    """
    w = dict(weights) if weights is not None else dict(DEFAULT_WEIGHTS)

    followers = _get(metrics, "followers")
    avg_likes = _get(metrics, "avg_likes_30d")
    original_ratio = _get(metrics, "original_ratio")
    long_post_ratio = _get(metrics, "long_post_ratio")
    posts_30d = _get(metrics, "posts_30d")
    annual_return = _get(metrics, "annual_return")
    sharpe = _get(metrics, "sharpe")
    max_drawdown = _get(metrics, "max_drawdown")
    has_portfolio = bool(metrics.get("has_portfolio", False))

    # Compute each dimension; NaN means "unavailable".
    components: Dict[str, float] = {}
    missing: list[str] = []

    inf = score_influence(followers)
    if math.isnan(inf):
        missing.append("influence")
    else:
        components["influence"] = inf

    eng = score_engagement(avg_likes, followers)
    if math.isnan(eng):
        missing.append("engagement")
    else:
        components["engagement"] = eng

    orig = score_originality(original_ratio, long_post_ratio)
    if math.isnan(orig):
        missing.append("originality")
    else:
        components["originality"] = orig

    act = score_activity(posts_30d)
    if math.isnan(act):
        missing.append("activity")
    else:
        components["activity"] = act

    # performance: special handling — no portfolio → 0 (penalty), included.
    perf = score_performance(annual_return, sharpe, max_drawdown)
    if not has_portfolio or math.isnan(perf):
        components["performance"] = 0.0
        # Do NOT add to missing — it's a deliberate 0, not "unknown".
    else:
        components["performance"] = perf

    # Re-normalize weights over available (non-missing) dimensions.
    active_dims = set(components.keys())  # performance always in here (as 0 if penalized)
    active_weight_total = sum(w.get(d, 0.0) for d in active_dims)
    if active_weight_total <= 0:
        # Degenerate: no weights on any available dimension. Flat fallback.
        return ScoreBreakdown(
            total=0.0,
            components=components,
            weights_used={d: 0.0 for d in active_dims},
            missing_dims=missing,
        )
    weights_used = {d: w.get(d, 0.0) / active_weight_total for d in active_dims}

    total = sum(components[d] * weights_used[d] for d in active_dims)
    # Guard against fp drift pushing slightly outside [0,1].
    total = _clip01(total)
    return ScoreBreakdown(
        total=total,
        components=components,
        weights_used=weights_used,
        missing_dims=missing,
    )
