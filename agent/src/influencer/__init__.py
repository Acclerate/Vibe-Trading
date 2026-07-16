"""Xueqiu influencer (大V) watchlist pool.

Maintains a dynamic pool of high-value Xueqiu influencers, refreshed monthly
via :func:`src.influencer.refresh.refresh_pool`. The pool is persisted in a
crash-safe JSON store under ``~/.vibe-trading/influencer/``.

Public API:

* :class:`Influencer`, :class:`PoolSnapshot`, :class:`PoolStatus` — data models.
* :class:`InfluencerPoolStore` — durable persistence.
* :func:`score_bigv` — the dynamic scoring function.
* :class:`XueqiuDataSource` — metrics fetcher (hybrid pysnowball + direct).
* :func:`refresh_pool` — monthly refresh entry point.
* CLI: ``python -m src.influencer.refresh``.
"""

from src.influencer.models import Influencer, PoolSnapshot, PoolStatus, ScoreBreakdown
from src.influencer.scoring import DEFAULT_WEIGHTS, score_bigv
from src.influencer.store import CorruptStoreError, InfluencerPoolStore
from src.influencer.datasource import InfluencerMetrics, XueqiuDataSource
from src.influencer.refresh import DEFAULTS, RefreshConfig, refresh_pool

__all__ = [
    "DEFAULTS",
    "DEFAULT_WEIGHTS",
    "CorruptStoreError",
    "Influencer",
    "InfluencerMetrics",
    "InfluencerPoolStore",
    "PoolSnapshot",
    "PoolStatus",
    "RefreshConfig",
    "ScoreBreakdown",
    "XueqiuDataSource",
    "refresh_pool",
    "score_bigv",
]
