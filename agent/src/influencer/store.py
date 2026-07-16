"""Crash-safe store for the Xueqiu influencer (大V) pool.

Mirrors the atomic-write pattern of :mod:`src.scheduled_research.store`
(write temp → fsync → ``os.replace`` → fsync parent dir) so the store survives
a SIGKILL at any point without corruption.

The store persists two things in one JSON envelope:

* ``influencers`` — the current pool members (active + probation), keyed by uid.
* ``seeds``       — the candidate seed list (uid → {screen_name, note}).
* ``snapshots``   — recent monthly refresh outcomes (capped, newest last).

Removed influencers are deleted from ``influencers`` (their history is
preserved in the ``removed`` list of past snapshots) rather than kept around
as tombstones, to keep the active pool cheap to iterate.

A missing store file is the only clean empty result. A file that exists but
fails to parse is quarantined and ``load`` raises :class:`CorruptStoreError`
instead of silently returning an empty pool.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from src.config.paths import get_runtime_root
from src.influencer.models import Influencer, PoolSnapshot

logger = logging.getLogger(__name__)

_STORE_FILENAME = "influencer_pool.json"
_SCHEMA_VERSION = 1
# Keep the last N monthly snapshots inline. Older ones age out; the current
# pool + seeds are always retained in full.
_MAX_SNAPSHOTS = 24  # two years of monthly refreshes


def _default_store_path() -> Path:
    """Return the default store path under the user runtime dir."""
    return get_runtime_root() / "influencer" / _STORE_FILENAME


class CorruptStoreError(RuntimeError):
    """Raised when the store exists but cannot be parsed.

    The corrupt file is renamed aside (quarantined) before this is raised.

    Attributes:
        original: Path that failed to parse.
        quarantined: Path the corrupt file was moved to.
        cause: Short description of the parse failure.
    """

    def __init__(self, original: Path, quarantined: Path, cause: str) -> None:
        super().__init__(f"influencer store {original} is corrupt ({cause}); quarantined to {quarantined}")
        self.original = original
        self.quarantined = quarantined
        self.cause = cause


class InfluencerPoolStore:
    """Durable, crash-safe persistence for the influencer pool.

    The store owns only serialization and atomic I/O; scoring and refresh
    decisions live in :mod:`src.influencer.scoring` and
    :mod:`src.influencer.refresh`.

    Attributes:
        path: Absolute path of the backing JSON file.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        """Initialize the store.

        Args:
            path: Explicit path. Defaults to
                ``~/.vibe-trading/influencer/influencer_pool.json``.
        """
        self.path: Path = path if path is not None else _default_store_path()

    # ------------------------------------------------------------------
    # Public API — pool members
    # ------------------------------------------------------------------

    def load_pool(self) -> Dict[str, Influencer]:
        """Load all pool members (active + probation).

        Returns an empty dict when the store has never been written.

        Raises:
            CorruptStoreError: When the file exists but cannot be parsed.
        """
        envelope = self._load_envelope()
        result: Dict[str, Influencer] = {}
        for item in envelope.get("influencers", []):
            inf = Influencer.from_dict(item)
            result[inf.uid] = inf
        return result

    def save_pool(self, pool: Dict[str, Influencer]) -> None:
        """Atomically persist the full pool (replaces the pool segment).

        Seeds and snapshots are preserved unchanged.

        Args:
            pool: Mapping of uid to :class:`Influencer` (the full set).
        """
        envelope = self._load_envelope_or_empty()
        envelope["influencers"] = [inf.to_dict() for inf in pool.values()]
        # Only persist active/probation members — removed ones are already
        # captured in snapshot history.
        envelope["influencers"] = [
            d for d in envelope["influencers"]
            if d.get("status") in ("active", "probation")
        ]
        self._write_envelope(envelope)

    def upsert(self, influencer: Influencer) -> None:
        """Insert or replace an influencer by uid."""
        pool = self.load_pool()
        pool[influencer.uid] = influencer
        self.save_pool(pool)

    def get(self, uid: str) -> Optional[Influencer]:
        """Return an influencer by uid, or ``None`` when absent."""
        return self.load_pool().get(uid)

    def list_active(
        self, include_probation: bool = True, limit: int = 100
    ) -> List[Influencer]:
        """Return pool members sorted by ``added_at`` ascending (oldest first).

        Args:
            include_probation: When False, return only ACTIVE members.
            limit: Maximum number of members to return.
        """
        pool = list(self.load_pool().values())
        if include_probation:
            members = [i for i in pool if i.status.value in ("active", "probation")]
        else:
            members = [i for i in pool if i.status.value == "active"]
        members.sort(key=lambda i: i.added_at)
        return members[:limit]

    def delete(self, uid: str) -> bool:
        """Remove a member by uid.

        Returns:
            ``True`` when the member was found and removed; ``False`` otherwise.
        """
        pool = self.load_pool()
        if uid not in pool:
            return False
        del pool[uid]
        self.save_pool(pool)
        return True

    # ------------------------------------------------------------------
    # Public API — seeds
    # ------------------------------------------------------------------

    def load_seeds(self) -> Dict[str, Dict[str, str]]:
        """Load the seed candidate list as ``{uid: {screen_name, note}}``."""
        envelope = self._load_envelope_or_empty()
        return dict(envelope.get("seeds", {}))

    def save_seeds(self, seeds: Dict[str, Dict[str, str]]) -> None:
        """Atomically persist the full seed list (replaces the seeds segment)."""
        envelope = self._load_envelope_or_empty()
        envelope["seeds"] = dict(seeds)
        self._write_envelope(envelope)

    def add_seed(self, uid: str, screen_name: str, note: str = "") -> None:
        """Add or update a seed candidate."""
        seeds = self.load_seeds()
        seeds[uid] = {"screen_name": screen_name, "note": note}
        self.save_seeds(seeds)

    def remove_seed(self, uid: str) -> bool:
        """Remove a seed candidate. Returns ``True`` if it was present."""
        seeds = self.load_seeds()
        if uid not in seeds:
            return False
        del seeds[uid]
        self.save_seeds(seeds)
        return True

    # ------------------------------------------------------------------
    # Public API — snapshots
    # ------------------------------------------------------------------

    def load_snapshots(self) -> List[PoolSnapshot]:
        """Load retained monthly snapshots, oldest first."""
        envelope = self._load_envelope_or_empty()
        out: List[PoolSnapshot] = []
        for raw in envelope.get("snapshots", []):
            out.append(PoolSnapshot(
                date=raw["date"],
                decided_at=int(raw.get("decided_at", 0)),
                pool_size_before=int(raw.get("pool_size_before", 0)),
                pool_size_after=int(raw.get("pool_size_after", 0)),
                added=list(raw.get("added", [])),
                removed=list(raw.get("removed", [])),
                promoted=list(raw.get("promoted", [])),
                scores=dict(raw.get("scores", {})),
                config=dict(raw.get("config", {})),
            ))
        return out

    def append_snapshot(self, snapshot: PoolSnapshot) -> None:
        """Append a refresh snapshot, trimming to the last ``_MAX_SNAPSHOTS``."""
        envelope = self._load_envelope_or_empty()
        snaps = envelope.get("snapshots", [])
        snaps.append(snapshot.to_dict())
        if len(snaps) > _MAX_SNAPSHOTS:
            snaps = snaps[-_MAX_SNAPSHOTS:]
        envelope["snapshots"] = snaps
        self._write_envelope(envelope)

    # ------------------------------------------------------------------
    # Internal — envelope I/O
    # ------------------------------------------------------------------

    def _load_envelope(self) -> dict:
        """Load and validate the envelope, raising on corruption."""
        if not self.path.exists():
            return {}
        try:
            raw = self.path.read_text(encoding="utf-8")
            envelope = json.loads(raw)
            if not isinstance(envelope, dict):
                raise ValueError("store root is not a JSON object")
            return envelope
        except (OSError, ValueError) as exc:
            quarantined = self._quarantine(str(exc))
            raise CorruptStoreError(self.path, quarantined, str(exc)) from exc

    def _load_envelope_or_empty(self) -> dict:
        """Like :meth:`_load_envelope` but tolerant of a missing file."""
        if not self.path.exists():
            return self._empty_envelope()
        return self._load_envelope()

    @staticmethod
    def _empty_envelope() -> dict:
        return {
            "schema_version": _SCHEMA_VERSION,
            "influencers": [],
            "seeds": {},
            "snapshots": [],
        }

    def _write_envelope(self, envelope: dict) -> None:
        """Atomic write: temp → fsync → replace → fsync parent dir."""
        target = self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        envelope.setdefault("schema_version", _SCHEMA_VERSION)
        payload = json.dumps(envelope, ensure_ascii=False, indent=2)

        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

        os.replace(tmp, target)
        self._fsync_dir(target.parent)

    def _quarantine(self, cause: str) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        quarantined = self.path.with_name(f"{self.path.name}.corrupt-{ts}")
        try:
            os.replace(self.path, quarantined)
            logger.error(
                "influencer store %s corrupt (%s) — quarantined to %s",
                self.path, cause, quarantined,
            )
        except OSError:
            logger.error(
                "influencer store %s corrupt (%s) — quarantine rename failed",
                self.path, cause, exc_info=True,
            )
            return self.path
        return quarantined

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        except OSError:
            logger.debug("parent-dir fsync unsupported on %s", directory, exc_info=True)
        finally:
            os.close(dir_fd)
