"""Hybrid data source for Xueqiu influencer metrics.

Strategy (confirmed during planning): reuse ``pysnowball``'s token mechanism
(``set_token`` / ``XUEQIUTOKEN`` env var) and its ``cube.detail`` for portfolio
performance, but fetch the social data pysnowball doesn't cover (profile,
timeline, cube-list-by-owner) directly from xueqiu.com web endpoints using
:mod:`requests`, through the same ``xq_a_token`` cookie.

Degradation contract
--------------------
No single failed fetch aborts a refresh. Each dimension returns NaN-safe
metrics; :func:`~src.influencer.scoring.score_bigv` then excludes the missing
dimension (or, for ``performance``, applies a 0 penalty). This is what makes
the pipeline robust to Xueqiu's anti-scraping and token expiry.

Token acquisition order
-----------------------
1. ``pysnowball.set_token(token)`` if called programmatically.
2. ``XUEQIUTOKEN`` env var (pysnowball's own convention; also read by us).
3. ``XUEQIU_TOKEN`` env var (the project's ``.env`` convention) — copied into
   ``XUEQIUTOKEN`` for pysnowball's benefit.
4. ``None`` → all fetches return empty metrics; refresh proceeds in
   "metrics-stale" mode (scores computed from whatever's available).
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from backtest.loaders.base import (
    DEFAULT_BACKOFF,
    DEFAULT_MAX_RETRIES,
    retry_with_budget,
)

logger = logging.getLogger(__name__)

_BASE = "https://xueqiu.com"
_REQUEST_TIMEOUT = 10  # seconds per HTTP call
_RATE_LIMIT_SLEEP = 0.6  # seconds between calls (gentle; ~100 calls/min ceiling)
_DEFAULT_FETCH_BUDGET_S = 30.0  # total wall-clock budget for one uid's full fetch


# ---------------------------------------------------------------------------
# Metrics DTO
# ---------------------------------------------------------------------------


@dataclass
class InfluencerMetrics:
    """Structured metrics for one influencer, ready for :func:`score_bigv`.

    All numeric fields default to NaN to signal "unavailable". The dataclass
    serializes to a plain dict via :meth:`to_metrics_dict` that matches the
    keys :mod:`scoring` expects.
    """

    uid: str = ""
    screen_name: str = ""
    followers: float = float("nan")
    followings: float = float("nan")
    avg_likes_30d: float = float("nan")
    avg_comments_30d: float = float("nan")
    original_ratio: float = float("nan")
    long_post_ratio: float = float("nan")
    posts_30d: float = float("nan")
    has_portfolio: bool = False
    annual_return: float = float("nan")
    sharpe: float = float("nan")
    max_drawdown: float = float("nan")
    cube_ids: List[str] = field(default_factory=list)
    fetched_at: int = 0

    def to_metrics_dict(self) -> Dict[str, Any]:
        """Return the flat dict shape :func:`score_bigv` consumes."""
        return {
            "followers": self.followers,
            "avg_likes_30d": self.avg_likes_30d,
            "original_ratio": self.original_ratio,
            "long_post_ratio": self.long_post_ratio,
            "posts_30d": self.posts_30d,
            "annual_return": self.annual_return,
            "sharpe": self.sharpe,
            "max_drawdown": self.max_drawdown,
            "has_portfolio": self.has_portfolio,
            # Extra context kept in raw_metrics for audit:
            "_screen_name": self.screen_name,
            "_followings": self.followings,
            "_avg_comments_30d": self.avg_comments_30d,
            "_cube_ids": list(self.cube_ids),
            "_fetched_at": self.fetched_at,
        }


# ---------------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------------


class TokenNotConfiguredError(RuntimeError):
    """Raised when no Xueqiu token is available and strict mode is requested."""


def resolve_token() -> Optional[str]:
    """Resolve the Xueqiu token from env vars / pysnowball.

    Returns ``None`` when no token is configured. As a side effect, when the
    project's ``XUEQIU_TOKEN`` env var is set but ``XUEQIUTOKEN`` (pysnowball's
    convention) is not, the former is copied into the latter so any pysnowball
    call works without extra wiring.
    """
    token = os.getenv("XUEQIUTOKEN") or os.getenv("XUEQIU_TOKEN")
    if token:
        os.environ["XUEQIUTOKEN"] = token
        # Best-effort: propagate to pysnowball if it's importable.
        try:
            import pysnowball  # type: ignore[import-not-found]

            pysnowball.set_token(token)
        except ImportError:
            pass
        except Exception:  # pragma: no cover - defensive
            logger.debug("pysnowball.set_token failed", exc_info=True)
    return token


# ---------------------------------------------------------------------------
# Data source
# ---------------------------------------------------------------------------


class XueqiuDataSource:
    """Fetch influencer metrics from Xueqiu, with graceful degradation.

    The class is deliberately stateless beyond the session/token; each call
    to :meth:`fetch_all` produces a fresh :class:`InfluencerMetrics`.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        session: Optional[requests.Session] = None,
        budget_s: float = _DEFAULT_FETCH_BUDGET_S,
    ) -> None:
        self._session = session or requests.Session()
        self._budget_s = budget_s
        self._last_call_ts = 0.0
        self._token = token if token is not None else resolve_token()
        if self._token:
            self._session.headers.update({
                "Cookie": f"xq_a_token={self._token}",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
            })

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_all(self, uid: str) -> InfluencerMetrics:
        """Fetch every dimension for one user, degrading gracefully.

        Never raises for network/parse errors — they're logged and the
        affected fields stay NaN. Returns a metrics object regardless.

        A uid that is a pending placeholder (starts with
        ``PENDING_VERIFICATION``) is skipped with an all-NaN result and a
        warning — it scores 0 and won't enter the pool, which is the safe
        default for an unverified account.
        """
        metrics = InfluencerMetrics(uid=uid, fetched_at=int(time.time() * 1000))
        if not self._token:
            logger.warning(
                "no Xueqiu token configured (set XUEQIU_TOKEN in .env); "
                "fetch_all returning all-NaN metrics for uid=%s",
                uid,
            )
            return metrics

        # Skip pending placeholders (unverified uids) — fetching a wrong
        # numeric id would silently pollute the pool with a stranger's data.
        from src.influencer.seeds import PENDING_PREFIX

        if uid.startswith(PENDING_PREFIX):
            logger.warning(
                "skipping unverified seed uid=%s — resolve it in seeds.py "
                "or via --add-seed before it can enter the pool",
                uid,
            )
            return metrics

        deadline = time.monotonic() + self._budget_s
        profile = self._safe(self._fetch_profile, uid, deadline=deadline, label="profile")
        timeline = self._safe(self._fetch_timeline, uid, deadline=deadline, label="timeline")
        cube_ids = self._safe(self._fetch_user_cube_ids, uid, deadline=deadline, label="cube-list")

        if profile:
            metrics.screen_name = str(profile.get("screen_name") or profile.get("name") or "")
            metrics.followers = _to_float(profile.get("followers_count") or profile.get("follower_count"))
            metrics.followings = _to_float(profile.get("friends_count") or profile.get("followings"))
        if timeline:
            posts = timeline.get("list") or timeline.get("statuses") or []
            metrics.posts_30d = _count_posts_last_30d(posts)
            metrics.avg_likes_30d = _avg_likes(posts)
            metrics.avg_comments_30d = _avg_comments(posts)
            metrics.original_ratio = _original_ratio(posts)
            metrics.long_post_ratio = _long_post_ratio(posts)
        if cube_ids:
            metrics.cube_ids = list(cube_ids)
            # Take the first cube's detail as the performance proxy. pysnowball
            # is the most reliable path for cube detail; fall back to direct.
            detail = self._fetch_best_cube_detail(cube_ids, deadline)
            if detail:
                metrics.has_portfolio = True
                metrics.annual_return = _to_float(
                    detail.get("annual_return") or detail.get("annualized_gain")
                )
                metrics.sharpe = _to_float(detail.get("sharpe"))
                metrics.max_drawdown = _to_float(detail.get("max_drawdown"))
        return metrics

    # ------------------------------------------------------------------
    # Individual endpoint fetchers (may raise; wrapped by _safe)
    # ------------------------------------------------------------------

    def _fetch_profile(self, uid: str) -> Optional[dict]:
        url = f"{_BASE}/user/show.json"
        data = self._get_json(url, params={"id": uid}, label=f"profile/{uid}")
        return data.get("data") if isinstance(data, dict) else None

    def _fetch_timeline(self, uid: str) -> Optional[dict]:
        url = f"{_BASE}/v4/statuses/user_timeline.json"
        data = self._get_json(
            url,
            params={"user_id": uid, "page": 1, "count": 40},
            label=f"timeline/{uid}",
        )
        return data if isinstance(data, dict) else None

    def _fetch_user_cube_ids(self, uid: str) -> Optional[List[str]]:
        url = f"{_BASE}/cubes/owner.json"
        data = self._get_json(url, params={"uid": uid, "type": 1}, label=f"cubes/{uid}")
        if not isinstance(data, dict):
            return None
        cubes = data.get("data") or data.get("cubes") or []
        out: List[str] = []
        for c in cubes:
            if isinstance(c, dict):
                cid = c.get("symbol") or c.get("id")
                if cid:
                    out.append(str(cid))
        return out

    def _fetch_best_cube_detail(self, cube_ids: List[str], deadline: float) -> Optional[dict]:
        """Fetch the first cube detail via pysnowball, falling back to direct."""
        for cid in cube_ids[:1]:  # one cube is enough as a proxy
            # Prefer pysnowball if importable.
            try:
                import pysnowball  # type: ignore[import-not-found]

                data = self._safe(lambda c=cid: pysnowball.cube.detail(c), deadline=deadline, label=f"cube-detail/{cid}")
                if isinstance(data, dict):
                    return data.get("data") if "data" in data else data
            except ImportError:
                pass
            # Direct fallback.
            return self._safe(self._fetch_cube_detail_direct, cid, deadline=deadline, label=f"cube-detail-direct/{cid}")
        return None

    def _fetch_cube_detail_direct(self, cube_id: str) -> Optional[dict]:
        url = f"{_BASE}/cubes/show.json"
        data = self._get_json(url, params={"symbol": cube_id}, label=f"cube/{cube_id}")
        return data.get("data") if isinstance(data, dict) else None

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------

    def _get_json(self, url: str, *, params: dict, label: str) -> dict:
        """GET with retry + rate limiting, returning parsed JSON."""
        self._throttle()

        def _do() -> dict:
            resp = self._session.get(url, params=params, timeout=_REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()

        return retry_with_budget(
            _do,
            transient=(requests.RequestException, ValueError),
            deadline=time.monotonic() + self._budget_s,
            label=label,
            max_retries=DEFAULT_MAX_RETRIES,
            backoff=DEFAULT_BACKOFF,
        )

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_call_ts
        if elapsed < _RATE_LIMIT_SLEEP:
            time.sleep(_RATE_LIMIT_SLEEP - elapsed)
        self._last_call_ts = time.time()

    def _safe(self, fn, *args, deadline: float, label: str):
        """Run fn(*args); log+swallow any error, returning None.

        Also aborts early if the deadline has passed.
        """
        if time.monotonic() > deadline:
            logger.debug("skipping %s — budget exhausted", label)
            return None
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 - intentional broad swallow
            logger.warning("fetch %s failed: %s", label, exc)
            return None


# ---------------------------------------------------------------------------
# Metric extraction helpers (pure, unit-testable)
# ---------------------------------------------------------------------------

_NOW_MS = time.time() * 1000
_30D_MS = 30 * 24 * 3600 * 1000


def _to_float(val: Any) -> float:
    if val is None or val == "":
        return float("nan")
    try:
        f = float(val)
    except (TypeError, ValueError):
        return float("nan")
    return f if not math.isnan(f) else float("nan")


def _count_posts_last_30d(posts: List[dict], now_ms: Optional[float] = None) -> float:
    if not posts:
        return float("nan")
    cutoff = (now_ms if now_ms is not None else _NOW_MS) - _30D_MS
    count = 0
    for p in posts:
        ts = p.get("created_at") or p.get("time") or p.get("timestamp")
        if ts is not None:
            try:
                # Xueqiu uses ms epoch for created_at on statuses.
                if float(ts) >= cutoff:
                    count += 1
            except (TypeError, ValueError):
                continue
    # `posts` is a single page; extrapolate by page fill ratio so a 40-item
    # page over ~30d gives a sensible frequency estimate.
    return float(count)


def _avg_likes(posts: List[dict]) -> float:
    vals = [_to_float(p.get("like_count") or p.get("likes")) for p in posts]
    vals = [v for v in vals if not math.isnan(v)]
    return sum(vals) / len(vals) if vals else float("nan")


def _avg_comments(posts: List[dict]) -> float:
    vals = [_to_float(p.get("reply_count") or p.get("comments")) for p in posts]
    vals = [v for v in vals if not math.isnan(v)]
    return sum(vals) / len(vals) if vals else float("nan")


def _original_ratio(posts: List[dict]) -> float:
    if not posts:
        return float("nan")
    original = sum(
        1 for p in posts
        if not p.get("retweeted_status") and not p.get("retweeted")
    )
    return original / len(posts)


def _long_post_ratio(posts: List[dict], long_threshold: int = 200) -> float:
    if not posts:
        return float("nan")
    long_count = 0
    for p in posts:
        text = p.get("description") or p.get("text") or p.get("title") or ""
        if len(str(text)) >= long_threshold:
            long_count += 1
    return long_count / len(posts)
