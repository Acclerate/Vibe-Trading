"""Data models for the Xueqiu influencer (大V) watchlist pool.

Mirrors the dataclass + ``to_dict``/``from_dict`` pattern of
:mod:`src.scheduled_research.models`. An :class:`Influencer` is a single
tracked Xueqiu user; a :class:`PoolSnapshot` is one monthly refresh result.

Unlike alpha factors (one score per stock), an influencer is an entity in its
own right with a lifecycle and a score history, so it lives in its own module
rather than under ``factors/zoo``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Lifecycle status
# ---------------------------------------------------------------------------


class PoolStatus(str, Enum):
    """Lifecycle status of a tracked influencer within the pool.

    PROBATION gives a freshly added influencer one observation window before it
    becomes eligible for removal — this prevents a single bad month from
    ejecting a newly admitted member (anti-churn guard).
    """

    ACTIVE = "active"        # full member, eligible for removal on low score
    PROBATION = "probation"  # newly added; protected from removal this cycle
    REMOVED = "removed"      # ejected; retained for history


# ---------------------------------------------------------------------------
# Score breakdown
# ---------------------------------------------------------------------------


@dataclass
class ScoreBreakdown:
    """Per-dimension decomposition of a :func:`~src.influencer.scoring.score_bigv` result.

    Attributes:
        total: Weighted total score in ``[0, 1]``.
        components: Per-dimension normalized score in ``[0, 1]``. Missing
            dimensions (fetch failed) are omitted from this dict.
        weights_used: The effective weights, re-normalized over available
            dimensions so the total still lands in ``[0, 1]``.
        missing_dims: Dimension names that were unavailable (NaN) and excluded
            from the total. Transparent for observability — a consistently
            missing ``performance`` is a red flag.
    """

    total: float
    components: Dict[str, float] = field(default_factory=dict)
    weights_used: Dict[str, float] = field(default_factory=dict)
    missing_dims: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "components": dict(self.components),
            "weights_used": dict(self.weights_used),
            "missing_dims": list(self.missing_dims),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScoreBreakdown":
        return cls(
            total=float(data["total"]),
            components=dict(data.get("components", {})),
            weights_used=dict(data.get("weights_used", {})),
            missing_dims=list(data.get("missing_dims", [])),
        )


# ---------------------------------------------------------------------------
# Influencer entity
# ---------------------------------------------------------------------------


@dataclass
class Influencer:
    """A tracked Xueqiu influencer.

    Attributes:
        uid: Xueqiu numeric user id (as a string, since it can exceed int32).
        screen_name: Display name at last refresh.
        added_at: Epoch-millisecond timestamp when the user entered the pool.
        status: Current lifecycle status.
        note: Free-form annotation (e.g. "价值投资", "私募").
        raw_metrics: Most recent raw metrics snapshot from the data source.
            Kept verbatim for auditability — the score is derived, not stored
            as the source of truth.
        score_history: Append-only list of ``{date, breakdown}`` dicts,
            newest last. Capped by the store to avoid unbounded growth.
        last_refreshed_at: Epoch-ms of the last successful metric fetch, or
            ``None`` if never refreshed.
    """

    uid: str
    screen_name: str
    added_at: int = field(default_factory=lambda: int(time.time() * 1000))
    status: PoolStatus = PoolStatus.ACTIVE
    note: str = ""
    raw_metrics: Dict[str, Any] = field(default_factory=dict)
    score_history: List[Dict[str, Any]] = field(default_factory=list)
    last_refreshed_at: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain JSON-serializable dict."""
        return {
            "uid": self.uid,
            "screen_name": self.screen_name,
            "added_at": self.added_at,
            "status": self.status.value,
            "note": self.note,
            "raw_metrics": dict(self.raw_metrics),
            "score_history": list(self.score_history),
            "last_refreshed_at": self.last_refreshed_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Influencer":
        """Reconstruct from a plain dict.

        Raises:
            KeyError: If a required field (``uid``, ``screen_name``) is missing.
            TypeError: If ``uid``/``screen_name`` are not strings.
            ValueError: If ``status`` is not a recognized :class:`PoolStatus`.
        """
        uid = data["uid"]
        screen_name = data["screen_name"]
        if not isinstance(uid, str) or not isinstance(screen_name, str):
            raise TypeError("'uid' and 'screen_name' must be strings")
        status = PoolStatus(data.get("status", PoolStatus.ACTIVE.value))
        raw_metrics = data.get("raw_metrics")
        score_history = data.get("score_history")
        return cls(
            uid=uid,
            screen_name=screen_name,
            added_at=int(data.get("added_at", int(time.time() * 1000))),
            status=status,
            note=str(data.get("note", "")),
            raw_metrics=dict(raw_metrics) if isinstance(raw_metrics, dict) else {},
            score_history=list(score_history) if isinstance(score_history, list) else [],
            last_refreshed_at=(
                int(data["last_refreshed_at"]) if data.get("last_refreshed_at") is not None else None
            ),
        )


# ---------------------------------------------------------------------------
# Refresh snapshot
# ---------------------------------------------------------------------------


@dataclass
class PoolSnapshot:
    """One monthly refresh outcome — what changed this cycle.

    Attributes:
        date: ``YYYY-MM-DD`` (UTC) of the refresh.
        decided_at: Epoch-ms when the decision was computed.
        pool_size_before / pool_size_after: Pool cardinality pre/post refresh.
        added: Uids that entered the pool this cycle.
        removed: Uids that were ejected this cycle.
        promoted: Uids that graduated from PROBATION to ACTIVE.
        scores: ``{uid: ScoreBreakdown.to_dict()}`` for every evaluated user
            (pool members + candidates), for audit/debugging.
        config: The thresholds and weights used, for reproducibility.
    """

    date: str
    decided_at: int = field(default_factory=lambda: int(time.time() * 1000))
    pool_size_before: int = 0
    pool_size_after: int = 0
    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    promoted: List[str] = field(default_factory=list)
    scores: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    config: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "decided_at": self.decided_at,
            "pool_size_before": self.pool_size_before,
            "pool_size_after": self.pool_size_after,
            "added": list(self.added),
            "removed": list(self.removed),
            "promoted": list(self.promoted),
            "scores": dict(self.scores),
            "config": dict(self.config),
        }
