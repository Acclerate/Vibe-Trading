"""Tests for the influencer pool store.

Covers: empty-store behavior, CRUD on pool members and seeds, atomic write
(no leftover temp files), corruption quarantine, snapshot append + trim, and
full round-trip persistence across store instances. Mirrors the style of
``test_scheduled_research_store.py``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.influencer.models import Influencer, PoolSnapshot, PoolStatus
from src.influencer.store import CorruptStoreError, InfluencerPoolStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_influencer(
    uid: str = "100001",
    screen_name: str = "测试大V",
    status: PoolStatus = PoolStatus.ACTIVE,
) -> Influencer:
    return Influencer(
        uid=uid,
        screen_name=screen_name,
        added_at=int(time.time() * 1000),
        status=status,
        note="价值投资",
    )


# ---------------------------------------------------------------------------
# Empty store
# ---------------------------------------------------------------------------


class TestEmptyStore:
    def test_load_pool_missing_file_returns_empty(self, tmp_path: Path) -> None:
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        assert store.load_pool() == {}

    def test_list_active_empty(self, tmp_path: Path) -> None:
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        assert store.list_active() == []

    def test_load_seeds_missing_file_returns_empty(self, tmp_path: Path) -> None:
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        assert store.load_seeds() == {}

    def test_load_snapshots_missing_file_returns_empty(self, tmp_path: Path) -> None:
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        assert store.load_snapshots() == []


# ---------------------------------------------------------------------------
# Pool CRUD
# ---------------------------------------------------------------------------


class TestPoolCRUD:
    def test_upsert_then_get(self, tmp_path: Path) -> None:
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        inf = _make_influencer()
        store.upsert(inf)
        fetched = store.get(inf.uid)
        assert fetched is not None
        assert fetched.uid == inf.uid
        assert fetched.screen_name == "测试大V"

    def test_get_missing_returns_none(self, tmp_path: Path) -> None:
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        assert store.get("ghost") is None

    def test_delete_removes_member(self, tmp_path: Path) -> None:
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        inf = _make_influencer()
        store.upsert(inf)
        assert store.delete(inf.uid) is True
        assert store.get(inf.uid) is None

    def test_delete_missing_returns_false(self, tmp_path: Path) -> None:
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        assert store.delete("ghost") is False

    def test_idempotent_upsert_replaces_not_duplicates(self, tmp_path: Path) -> None:
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        inf = _make_influencer()
        store.upsert(inf)
        updated = _make_influencer(uid=inf.uid, screen_name="新名字")
        store.upsert(updated)
        members = store.list_active()
        assert len(members) == 1
        assert members[0].screen_name == "新名字"

    def test_list_active_excludes_probation_when_asked(self, tmp_path: Path) -> None:
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        store.upsert(_make_influencer("1", "active", PoolStatus.ACTIVE))
        store.upsert(_make_influencer("2", "prob", PoolStatus.PROBATION))
        assert len(store.list_active()) == 2
        assert len(store.list_active(include_probation=False)) == 1

    def test_save_pool_drops_removed_status(self, tmp_path: Path) -> None:
        """REMOVED members must not be persisted in the active pool segment."""
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        pool = {
            "1": _make_influencer("1", "a", PoolStatus.ACTIVE),
            "2": _make_influencer("2", "b", PoolStatus.REMOVED),
        }
        store.save_pool(pool)
        assert store.get("1") is not None
        assert store.get("2") is None


# ---------------------------------------------------------------------------
# Seeds CRUD
# ---------------------------------------------------------------------------


class TestSeedsCRUD:
    def test_add_then_load_seed(self, tmp_path: Path) -> None:
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        store.add_seed("999", "候选大V", "私募")
        seeds = store.load_seeds()
        assert "999" in seeds
        assert seeds["999"]["screen_name"] == "候选大V"
        assert seeds["999"]["note"] == "私募"

    def test_add_seed_overwrites(self, tmp_path: Path) -> None:
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        store.add_seed("1", "old")
        store.add_seed("1", "new", "updated")
        seeds = store.load_seeds()
        assert seeds["1"]["screen_name"] == "new"
        assert len(seeds) == 1

    def test_remove_seed(self, tmp_path: Path) -> None:
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        store.add_seed("1", "x")
        assert store.remove_seed("1") is True
        assert store.load_seeds() == {}

    def test_remove_seed_missing_returns_false(self, tmp_path: Path) -> None:
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        assert store.remove_seed("ghost") is False

    def test_seeds_preserved_when_pool_saved(self, tmp_path: Path) -> None:
        """save_pool must not clobber the seeds segment."""
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        store.add_seed("1", "seed")
        store.upsert(_make_influencer("100", "member"))
        # After pool write, seeds should still be there.
        assert "1" in store.load_seeds()


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


class TestSnapshots:
    def _snap(self, date: str = "2026-07-16") -> PoolSnapshot:
        return PoolSnapshot(date=date, pool_size_before=1, pool_size_after=2, added=["1"])

    def test_append_then_load(self, tmp_path: Path) -> None:
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        store.append_snapshot(self._snap())
        snaps = store.load_snapshots()
        assert len(snaps) == 1
        assert snaps[0].date == "2026-07-16"
        assert snaps[0].added == ["1"]

    def test_snapshots_trimmed_to_cap(self, tmp_path: Path) -> None:
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        for i in range(30):
            store.append_snapshot(self._snap(date=f"2026-{i:02d}-01"))
        snaps = store.load_snapshots()
        # _MAX_SNAPSHOTS = 24
        assert len(snaps) == 24


# ---------------------------------------------------------------------------
# Atomicity & corruption
# ---------------------------------------------------------------------------


class TestAtomicityAndCorruption:
    def test_atomic_write_cleans_up_temp(self, tmp_path: Path) -> None:
        store = InfluencerPoolStore(path=tmp_path / "pool.json")
        store.upsert(_make_influencer())
        assert list(tmp_path.glob("*.tmp")) == []

    def test_corrupt_store_raises_and_quarantines(self, tmp_path: Path) -> None:
        store_path = tmp_path / "pool.json"
        store_path.write_text("{{not json}}", encoding="utf-8")
        store = InfluencerPoolStore(path=store_path)
        with pytest.raises(CorruptStoreError) as exc_info:
            store.load_pool()
        assert not store_path.exists()
        assert exc_info.value.quarantined.exists()

    def test_round_trip_across_instances(self, tmp_path: Path) -> None:
        """A second store instance reading the same file gets the same data."""
        path = tmp_path / "pool.json"
        store1 = InfluencerPoolStore(path=path)
        store1.upsert(_make_influencer("1", "Alice"))
        store1.add_seed("100", "seed-1")

        store2 = InfluencerPoolStore(path=path)
        assert store2.get("1") is not None
        assert store2.get("1").screen_name == "Alice"
        assert "100" in store2.load_seeds()

    def test_persisted_file_is_valid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "pool.json"
        store = InfluencerPoolStore(path=path)
        store.upsert(_make_influencer())
        # Should parse cleanly.
        envelope = json.loads(path.read_text(encoding="utf-8"))
        assert envelope["schema_version"] == 1
        assert isinstance(envelope["influencers"], list)
        assert isinstance(envelope["seeds"], dict)


# ---------------------------------------------------------------------------
# Model round-trip
# ---------------------------------------------------------------------------


class TestModelRoundTrip:
    def test_influencer_to_from_dict(self) -> None:
        inf = _make_influencer("1", "name")
        inf.raw_metrics = {"followers": 1000}
        inf.score_history = [{"date": "2026-07-16", "breakdown": {"total": 0.5}}]
        rebuilt = Influencer.from_dict(inf.to_dict())
        assert rebuilt.uid == inf.uid
        assert rebuilt.screen_name == inf.screen_name
        assert rebuilt.raw_metrics == inf.raw_metrics
        assert rebuilt.score_history == inf.score_history

    def test_influencer_rejects_bad_status(self) -> None:
        with pytest.raises(ValueError):
            Influencer.from_dict({"uid": "1", "screen_name": "x", "status": "bogus"})

    def test_influencer_rejects_non_string_uid(self) -> None:
        with pytest.raises(TypeError):
            Influencer.from_dict({"uid": 123, "screen_name": "x"})
