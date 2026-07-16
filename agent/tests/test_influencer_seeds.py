"""Tests for the seed list construction and pending-uid handling.

Covers: verified/pending seed separation, pending placeholder uniqueness,
is_pending detection, and that the datasource returns all-NaN metrics for
pending uids (so they can never enter the pool unverified).
"""

from __future__ import annotations

import math

from src.influencer import seeds
from src.influencer.datasource import InfluencerMetrics, XueqiuDataSource


class TestSeedMap:
    def test_seed_map_contains_verified_uids(self) -> None:
        # The three browser-verified uids must be present with real numeric keys.
        assert "1247347556" in seeds.SEED_MAP
        assert "3491303582" in seeds.SEED_MAP
        assert "3079173340" in seeds.SEED_MAP

    def test_pending_entries_get_distinct_keys(self) -> None:
        """Multiple pending entries must not collapse on a duplicate key."""
        pending_keys = [k for k in seeds.SEED_MAP if seeds.is_pending(k)]
        # 22 pending big-Vs declared (6 round1 + 7 round2 + 9 round3; 唐朝 removed).
        assert len(pending_keys) == 22
        # All keys unique (dict guarantees this, but assert explicitly).
        assert len(set(pending_keys)) == 22

    def test_default_seed_entries_has_uid_screen_name_note(self) -> None:
        entries = seeds.default_seed_entries()
        assert len(entries) == 25  # 3 verified + 22 pending
        for e in entries:
            assert {"uid", "screen_name", "note"} <= set(e.keys())

    def test_tang_chao_removed_due_to_controversy(self) -> None:
        """唐朝 was removed (P2P scandal + inactive); must not be in seeds."""
        names = {m["screen_name"] for m in seeds.SEED_MAP.values()}
        assert "唐朝" not in names

    def test_inactive_accounts_filtered(self) -> None:
        """Accounts the user flagged as inactive/marketing must be excluded."""
        excluded = {"ETF拯救世界", "重力加速度", "小小辛巴", "sosme"}
        names = {m["screen_name"] for m in seeds.SEED_MAP.values()}
        for name in excluded:
            assert name not in names, f"{name} should be filtered (inactive)"

    def test_is_pending_detects_placeholder(self) -> None:
        assert seeds.is_pending("PENDING_VERIFICATION_1") is True
        assert seeds.is_pending("1247347556") is False

    def test_verified_and_pending_partition(self) -> None:
        """No verified uid should start with the pending prefix and vice versa."""
        for uid in seeds.VERIFIED_SEEDS:
            assert not seeds.is_pending(uid)
        # Pending keys are generated, not in VERIFIED_SEEDS.
        for uid in seeds.SEED_MAP:
            if seeds.is_pending(uid):
                assert uid not in seeds.VERIFIED_SEEDS


class TestPendingSkipInDataSource:
    def test_pending_uid_returns_all_nan_metrics(self) -> None:
        """A pending placeholder must never trigger a real fetch."""
        ds = XueqiuDataSource(token="fake-token")  # token set so we reach the pending check
        m = ds.fetch_all("PENDING_VERIFICATION_1")
        assert isinstance(m, InfluencerMetrics)
        # Every scoreable field must be NaN → scores 0 → can't enter pool.
        assert math.isnan(m.followers)
        assert math.isnan(m.posts_30d)
        assert m.has_portfolio is False

    def test_verified_uid_not_skipped_by_pending_check(self) -> None:
        """The pending guard must not match a real numeric uid."""
        assert not "1247347556".startswith(seeds.PENDING_PREFIX)
