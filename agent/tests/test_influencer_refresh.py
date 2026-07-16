"""Tests for the refresh decision logic (enter/exit/promote).

These test the pure ``_decide`` function and the full ``refresh_pool`` flow
with a stub datasource — no network. Verifies anti-churn guarantees: separated
thresholds, PROBATION protection, and pool-cap enforcement.
"""

from __future__ import annotations

import time
from typing import Dict, List

import pytest

from src.influencer.datasource import InfluencerMetrics
from src.influencer.models import Influencer, PoolStatus, ScoreBreakdown
from src.influencer.refresh import RefreshConfig, _decide, refresh_pool
from src.influencer.store import InfluencerPoolStore


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class StubDataSource:
    """Returns canned metrics per uid, no network."""

    def __init__(self, metrics_by_uid: Dict[str, InfluencerMetrics]) -> None:
        self._metrics = metrics_by_uid
        self.fetched: List[str] = []

    def fetch_all(self, uid: str) -> InfluencerMetrics:
        self.fetched.append(uid)
        return self._metrics.get(uid, InfluencerMetrics(uid=uid))


def _metrics(score: float) -> InfluencerMetrics:
    """Build metrics that will produce a given score under default weights.

    We pick a simple shape: vary followers to move the influence dimension,
    and portfolio to move performance. Exact score isn't critical — we just
    need to straddle the thresholds, so we use score_bigv directly in tests
    to pin inputs.
    """
    # High across the board → ~1.0; we then lower individual fields in callers.
    return InfluencerMetrics(
        uid="x",
        followers=1_000_000,
        avg_likes_30d=10_000,
        original_ratio=1.0,
        long_post_ratio=0.5,
        posts_30d=30,
        has_portfolio=True,
        annual_return=0.30,
        sharpe=2.0,
        max_drawdown=-0.10,
    )


def _sb(total: float) -> ScoreBreakdown:
    """Build a score breakdown with a pinned total (bypassing real scoring)."""
    return ScoreBreakdown(total=total, components={"x": total}, weights_used={"x": 1.0})


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestRefreshConfig:
    def test_rejects_exit_ge_entry(self) -> None:
        with pytest.raises(ValueError):
            RefreshConfig(entry_threshold=0.5, exit_threshold=0.5)

    def test_rejects_exit_above_entry(self) -> None:
        with pytest.raises(ValueError):
            RefreshConfig(entry_threshold=0.3, exit_threshold=0.4)

    def test_rejects_zero_pool_cap(self) -> None:
        with pytest.raises(ValueError):
            RefreshConfig(pool_cap=0)

    def test_defaults_are_strict(self) -> None:
        c = RefreshConfig()
        assert c.exit_threshold < c.entry_threshold
        assert c.pool_cap == 50


# ---------------------------------------------------------------------------
# Decision logic — _decide
# ---------------------------------------------------------------------------


class TestDecideExit:
    def test_active_member_below_exit_removed(self) -> None:
        now = int(time.time() * 1000)
        pool = {"1": Influencer(uid="1", screen_name="a", added_at=now, status=PoolStatus.ACTIVE)}
        seeds = {}
        scores = {"1": _sb(0.10)}  # well below default exit 0.35
        cfg = RefreshConfig()
        d = _decide(pool, seeds, scores, cfg)
        assert "1" in d["removed"]

    def test_active_member_above_exit_kept(self) -> None:
        now = int(time.time() * 1000)
        pool = {"1": Influencer(uid="1", screen_name="a", added_at=now, status=PoolStatus.ACTIVE)}
        scores = {"1": _sb(0.90)}
        cfg = RefreshConfig()
        d = _decide(pool, {}, scores, cfg)
        assert d["removed"] == []

    def test_probation_member_protected_from_removal(self) -> None:
        """PROBATION members are not removable even with a low score."""
        now = int(time.time() * 1000)
        pool = {"1": Influencer(uid="1", screen_name="a", added_at=now, status=PoolStatus.PROBATION)}
        scores = {"1": _sb(0.01)}
        cfg = RefreshConfig()
        d = _decide(pool, {}, scores, cfg)
        assert d["removed"] == []
        assert "1" not in d["promoted"]  # not old enough to promote either

    def test_nan_score_member_not_removed(self) -> None:
        """Don't eject on data gaps (NaN score)."""
        now = int(time.time() * 1000)
        pool = {"1": Influencer(uid="1", screen_name="a", added_at=now, status=PoolStatus.ACTIVE)}
        scores = {"1": _sb(float("nan"))}
        cfg = RefreshConfig()
        d = _decide(pool, {}, scores, cfg)
        assert d["removed"] == []


class TestDecidePromote:
    def test_old_probation_promoted(self) -> None:
        cfg = RefreshConfig(probation_days=31)
        old_added = int(time.time() * 1000) - cfg.probation_ms - 1000  # past window
        pool = {"1": Influencer(uid="1", screen_name="a", added_at=old_added, status=PoolStatus.PROBATION)}
        d = _decide(pool, {}, {"1": _sb(0.9)}, cfg)
        assert "1" in d["promoted"]


class TestDecideEnter:
    def test_high_scoring_candidate_added(self) -> None:
        pool = {}
        seeds = {"100": {"screen_name": "cand", "note": ""}}
        scores = {"100": _sb(0.90)}
        cfg = RefreshConfig()
        d = _decide(pool, seeds, scores, cfg)
        assert "100" in d["added"]

    def test_low_scoring_candidate_not_added(self) -> None:
        seeds = {"100": {"screen_name": "cand", "note": ""}}
        scores = {"100": _sb(0.40)}  # below entry 0.55
        cfg = RefreshConfig()
        d = _decide({}, seeds, scores, cfg)
        assert d["added"] == []

    def test_respects_pool_cap(self) -> None:
        """When pool is full, no new entries even if score is high."""
        now = int(time.time() * 1000)
        # Fill pool to cap with high scorers.
        pool = {
            str(i): Influencer(uid=str(i), screen_name="m", added_at=now, status=PoolStatus.ACTIVE)
            for i in range(50)
        }
        scores = {str(i): _sb(0.9) for i in range(50)}
        seeds = {"999": {"screen_name": "cand", "note": ""}}
        scores["999"] = _sb(0.99)
        cfg = RefreshConfig(pool_cap=50)
        d = _decide(pool, seeds, scores, cfg)
        assert d["added"] == []

    def test_candidates_ranked_by_score(self) -> None:
        """When cap allows only one slot, the highest-scoring candidate wins."""
        seeds = {
            "a": {"screen_name": "a", "note": ""},
            "b": {"screen_name": "b", "note": ""},
        }
        scores = {"a": _sb(0.60), "b": _sb(0.95)}
        cfg = RefreshConfig(pool_cap=1)  # only room for one
        d = _decide({}, seeds, scores, cfg)
        assert d["added"] == ["b"]


# ---------------------------------------------------------------------------
# Full refresh_pool with stubs
# ---------------------------------------------------------------------------


class TestRefreshPoolFlow:
    def test_dry_run_does_not_persist(self, tmp_path) -> None:
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        ds = StubDataSource({"1": _metrics(1.0)})
        cfg = RefreshConfig()
        snap = refresh_pool(store=store, datasource=ds, config=cfg, dry_run=True)
        # Pool should still be empty after dry run.
        assert store.load_pool() == {}
        assert snap.pool_size_after == 0  # nothing applied
        assert len(snap.scores) >= 0

    def test_full_run_promotes_candidate_into_pool(self, tmp_path) -> None:
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        # Pre-seed a candidate.
        store.add_seed("1", "cand", "")
        ds = StubDataSource({"1": _metrics(1.0)})
        cfg = RefreshConfig()
        snap = refresh_pool(store=store, datasource=ds, config=cfg)
        # Candidate 1 should now be in the pool on probation.
        inf = store.get("1")
        assert inf is not None
        assert inf.status == PoolStatus.PROBATION
        assert "1" in snap.added
        assert inf.raw_metrics != {}  # metrics attached

    def test_second_run_keeps_probation_member(self, tmp_path) -> None:
        """A probation member with a low score is NOT removed on the next run."""
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        store.add_seed("1", "cand", "")
        # First run: high score → admitted.
        ds_hi = StubDataSource({"1": _metrics(1.0)})
        refresh_pool(store=store, datasource=ds_hi, config=RefreshConfig())
        # Second run: score drops to ~0 (no portfolio). Probation protects it.
        ds_lo = StubDataSource({"1": InfluencerMetrics(uid="1", has_portfolio=False)})
        snap = refresh_pool(store=store, datasource=ds_lo, config=RefreshConfig())
        assert "1" not in snap.removed
        assert store.get("1") is not None
