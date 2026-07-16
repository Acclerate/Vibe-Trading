"""Tests for the influencer scoring function.

Covers: per-dimension normalization boundaries, weight re-normalization over
missing dimensions, the ``performance`` absence penalty, default-weight sum,
and the monotonicity of the main scoring curve. All pure (no network).
"""

from __future__ import annotations

import math

import pytest

from src.influencer.scoring import (
    DEFAULT_WEIGHTS,
    score_activity,
    score_bigv,
    score_engagement,
    score_influence,
    score_originality,
    score_performance,
)


# ---------------------------------------------------------------------------
# Per-dimension scorers
# ---------------------------------------------------------------------------


class TestScoreInfluence:
    def test_zero_followers_is_nan(self) -> None:
        assert math.isnan(score_influence(0))

    def test_negative_followers_is_nan(self) -> None:
        assert math.isnan(score_influence(-5))

    def test_monotonic_in_followers(self) -> None:
        a = score_influence(1_000)
        b = score_influence(10_000)
        c = score_influence(100_000)
        d = score_influence(1_000_000)
        assert a < b < c < d

    def test_saturates_at_one_million(self) -> None:
        assert score_influence(1_000_000) == pytest.approx(1.0)
        assert score_influence(10_000_000) == 1.0  # clipped

    def test_low_floor(self) -> None:
        # 1 follower should be near 0 but non-negative.
        assert 0.0 <= score_influence(1) < 0.2


class TestScoreEngagement:
    def test_nan_when_followers_zero(self) -> None:
        assert math.isnan(score_engagement(100, 0))

    def test_high_engagement_rate_saturates(self) -> None:
        # 1000 followers * 0.1 reach = 100 impressions; 10 likes = 10% rate → 1.0
        assert score_engagement(10, 1000) == pytest.approx(1.0)

    def test_low_engagement(self) -> None:
        # 1 like out of 100 impressions = 1% → 0.1
        assert score_engagement(1, 1000) == pytest.approx(0.1)

    def test_zero_likes(self) -> None:
        assert score_engagement(0, 1000) == 0.0


class TestScoreOriginality:
    def test_all_original_all_long(self) -> None:
        # original_ratio=1.0, long_ratio saturates at 0.5 → 0.5*1 + 0.5*1 = 1.0
        assert score_originality(1.0, 0.5) == pytest.approx(1.0)

    def test_all_reposts_short(self) -> None:
        assert score_originality(0.0, 0.0) == 0.0

    def test_nan_propagation(self) -> None:
        assert math.isnan(score_originality(float("nan"), 0.5))


class TestScorePerformance:
    def test_all_nan_is_nan(self) -> None:
        assert math.isnan(score_performance(float("nan"), float("nan"), float("nan")))

    def test_strong_performance_saturates(self) -> None:
        # 30% return, sharpe 2.0, -10% drawdown → calmar 3.0 → all sub-parts 1.0
        s = score_performance(0.30, 2.0, -0.10)
        assert s == pytest.approx(1.0)

    def test_negative_return_clips_to_zero(self) -> None:
        s = score_performance(-0.5, 1.0, -0.2)
        # return part = 0; sharpe part = 0.5; calmar = negative/0.2 < 0 → clipped 0
        # mean of available parts
        assert 0.0 <= s <= 0.5

    def test_clmar_handles_no_drawdown(self) -> None:
        # max_drawdown = 0 → calmar skipped, only return + sharpe averaged
        s = score_performance(0.20, 1.0, 0.0)
        assert 0.0 < s < 1.0


class TestScoreActivity:
    def test_zero_posts(self) -> None:
        assert score_activity(0) == 0.0

    def test_one_per_day_saturates(self) -> None:
        assert score_activity(30) == pytest.approx(1.0)

    def test_negative_is_nan(self) -> None:
        assert math.isnan(score_activity(-1))


# ---------------------------------------------------------------------------
# Composite score_bigv
# ---------------------------------------------------------------------------


class TestScoreBigV:
    def _full_metrics(self) -> dict:
        """A high-quality influencer: maxes most dimensions."""
        return {
            "followers": 1_000_000,
            "avg_likes_30d": 10_000,        # 10% rate → engagement 1.0
            "original_ratio": 1.0,
            "long_post_ratio": 0.5,
            "posts_30d": 30,
            "annual_return": 0.30,
            "sharpe": 2.0,
            "max_drawdown": -0.10,
            "has_portfolio": True,
        }

    def test_default_weights_sum_to_one(self) -> None:
        assert sum(DEFAULT_WEIGHTS.values()) == pytest.approx(1.0)

    def test_top_influencer_scores_near_one(self) -> None:
        sb = score_bigv(self._full_metrics())
        assert sb.total > 0.95
        assert "performance" in sb.components
        assert sb.components["performance"] == pytest.approx(1.0)
        assert sb.missing_dims == []

    def test_no_portfolio_penalizes_performance(self) -> None:
        m = self._full_metrics()
        m["has_portfolio"] = False
        sb = score_bigv(m)
        # performance forced to 0, but still counted in weights
        assert sb.components["performance"] == 0.0
        assert "performance" not in sb.missing_dims
        # total must be lower than the full case
        full = score_bigv(self._full_metrics()).total
        assert sb.total < full

    def test_missing_dimension_excluded_and_renormalized(self) -> None:
        """Drop all activity fields → activity excluded, weights renormalized."""
        m = self._full_metrics()
        m["posts_30d"] = float("nan")
        sb = score_bigv(m)
        assert "activity" in sb.missing_dims
        assert "activity" not in sb.components
        # weights_used should sum to 1 over remaining dims
        assert sum(sb.weights_used.values()) == pytest.approx(1.0)
        # and performance weight should be larger than default (renormalized up)
        assert sb.weights_used["performance"] > DEFAULT_WEIGHTS["performance"]

    def test_all_social_missing_keeps_performance(self) -> None:
        """If only portfolio data exists, score is still valid (performance-driven)."""
        m = {
            "has_portfolio": True,
            "annual_return": 0.30,
            "sharpe": 2.0,
            "max_drawdown": -0.10,
        }
        sb = score_bigv(m)
        assert sb.total > 0.0
        assert set(sb.components.keys()) == {"performance"}
        assert len(sb.missing_dims) == 4

    def test_empty_metrics_gives_zero_or_low(self) -> None:
        sb = score_bigv({})
        # No portfolio → performance=0, everything else missing.
        assert sb.total == 0.0
        assert "performance" in sb.components
        assert sb.components["performance"] == 0.0

    def test_total_in_unit_interval(self) -> None:
        for followers in (0, 100, 1_000_000):
            for ret in (-0.5, 0.0, 0.3):
                m = self._full_metrics()
                m["followers"] = followers
                m["annual_return"] = ret
                sb = score_bigv(m)
                assert 0.0 <= sb.total <= 1.0

    def test_custom_weights_respected(self) -> None:
        """With performance weight = 1.0 and a great portfolio, total ≈ 1.0."""
        m = self._full_metrics()
        sb = score_bigv(m, weights={"performance": 1.0})
        assert sb.total == pytest.approx(1.0)

    def test_to_from_dict_roundtrip(self) -> None:
        from src.influencer.models import ScoreBreakdown

        sb = score_bigv(self._full_metrics())
        rebuilt = ScoreBreakdown.from_dict(sb.to_dict())
        assert rebuilt.total == pytest.approx(sb.total)
        assert rebuilt.components == sb.components
        assert rebuilt.missing_dims == sb.missing_dims
