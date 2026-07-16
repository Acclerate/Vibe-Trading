"""Tests for the snowball-follow export bridge.

Verifies that export_sources() correctly translates the influencer pool +
verified seeds into the snowball-follow skill's ``{name, slug, tag}`` format,
skips PENDING placeholders, and de-duplicates pool members vs seeds.
"""

from __future__ import annotations

from pathlib import Path

from src.influencer.export_sources import export_sources
from src.influencer.models import Influencer, PoolStatus
from src.influencer.store import InfluencerPoolStore


class TestExportSources:
    def test_empty_pool_falls_back_to_verified_seeds(self, tmp_path: Path) -> None:
        """With no pool members, verified seeds are exported so digest works pre-refresh."""
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        sources = export_sources(store=store, include_seeds=True)
        # The 3 verified seeds (大道/闲来一坐/银行螺丝钉) must appear.
        names = {s["name"] for s in sources}
        assert "大道无形我有型" in names
        assert "闲来一坐s话投资" in names
        assert "银行螺丝钉" in names
        # PENDING placeholders must never appear.
        for s in sources:
            assert "待校验" not in s["name"]
            assert not s["slug"].startswith("PENDING_VERIFICATION")

    def test_pool_members_exported_with_notes_as_tags(self, tmp_path: Path) -> None:
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        store.upsert(Influencer(
            uid="9999999999",
            screen_name="测试大V",
            status=PoolStatus.ACTIVE,
            note="测试风格",
        ))
        sources = export_sources(store=store, include_seeds=False)
        test_entry = next(s for s in sources if s["slug"] == "9999999999")
        assert test_entry["name"] == "测试大V"
        assert test_entry["tag"] == "测试风格"

    def test_pending_pool_members_skipped(self, tmp_path: Path) -> None:
        """A pool member with a PENDING uid must not be exported (unfetchable)."""
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        store.upsert(Influencer(
            uid="PENDING_VERIFICATION_1",
            screen_name="未校验",
            status=PoolStatus.ACTIVE,
        ))
        sources = export_sources(store=store, include_seeds=False)
        assert all(not s["slug"].startswith("PENDING") for s in sources)

    def test_pool_member_takes_priority_over_seed(self, tmp_path: Path) -> None:
        """If a verified seed is also a pool member, it appears once."""
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        store.upsert(Influencer(
            uid="1247347556",  # 大道, also a verified seed
            screen_name="大道无形我有型",
            status=PoolStatus.ACTIVE,
        ))
        sources = export_sources(store=store, include_seeds=True)
        dadao_count = sum(1 for s in sources if s["slug"] == "1247347556")
        assert dadao_count == 1

    def test_exclude_seeds_flag(self, tmp_path: Path) -> None:
        """include_seeds=False exports pool members only."""
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        sources = export_sources(store=store, include_seeds=False)
        # No pool members → empty (verified seeds excluded).
        assert sources == []

    def test_output_shape_matches_skill_schema(self, tmp_path: Path) -> None:
        """Each entry must have exactly name/slug/tag for the skill's config."""
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        sources = export_sources(store=store, include_seeds=True)
        for s in sources:
            assert set(s.keys()) == {"name", "slug", "tag"}
            assert isinstance(s["name"], str) and s["name"]
            assert isinstance(s["slug"], str) and s["slug"]
