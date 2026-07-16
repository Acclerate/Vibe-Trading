"""Monthly refresh of the influencer pool — the top-level orchestration.

Entry point: ``python -m src.influencer.refresh``. The flow:

1. Load the current pool + seed candidates from the store.
2. Initialize the seed list from :mod:`seeds` on first run (empty store).
3. For each pool member *and* each seed candidate, fetch fresh metrics and
   score them.
4. Apply strict enter/exit decisions with separated thresholds (anti-churn):
     - enter: candidate score ≥ ENTRY  AND pool not full → add as PROBATION
     - exit:  member score   < EXIT    AND not on PROBATION → remove
     - promote: PROBATION members older than PROBATION_DAYS → ACTIVE
5. Persist the updated pool + append a :class:`PoolSnapshot`.

PROBATION protects newly added members from being ejected on their first
evaluation — a single stale fetch shouldn't bounce a fresh admit. Members
must survive one full cycle before becoming removable.

CLI is deliberately the only trigger (no scheduler wiring); run it on your
own monthly cadence via cron or manually.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Mapping, Optional

from src.influencer.datasource import InfluencerMetrics, XueqiuDataSource
from src.influencer.models import Influencer, PoolSnapshot, PoolStatus, ScoreBreakdown
from src.influencer.scoring import DEFAULT_WEIGHTS, score_bigv
from src.influencer.seeds import default_seed_entries
from src.influencer.store import InfluencerPoolStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Refresh configuration
# ---------------------------------------------------------------------------


class RefreshConfig:
    """Tunable thresholds for a refresh cycle. All times in ms unless noted."""

    def __init__(
        self,
        pool_cap: int = 50,
        entry_threshold: float = 0.55,
        exit_threshold: float = 0.35,
        probation_days: int = 31,
        weights: Optional[Mapping[str, float]] = None,
    ) -> None:
        if not (0.0 <= exit_threshold < entry_threshold <= 1.0):
            raise ValueError(
                f"require 0 <= exit({exit_threshold}) < entry({entry_threshold}) <= 1"
            )
        if pool_cap <= 0:
            raise ValueError("pool_cap must be positive")
        self.pool_cap = pool_cap
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold
        self.probation_ms = probation_days * 24 * 3600 * 1000
        self.weights = dict(weights) if weights is not None else dict(DEFAULT_WEIGHTS)

    def to_dict(self) -> dict:
        return {
            "pool_cap": self.pool_cap,
            "entry_threshold": self.entry_threshold,
            "exit_threshold": self.exit_threshold,
            "probation_days": self.probation_ms // (24 * 3600 * 1000),
            "weights": dict(self.weights),
        }


DEFAULTS = RefreshConfig()


# ---------------------------------------------------------------------------
# Core refresh logic
# ---------------------------------------------------------------------------


def refresh_pool(
    store: Optional[InfluencerPoolStore] = None,
    datasource: Optional[XueqiuDataSource] = None,
    config: Optional[RefreshConfig] = None,
    *,
    dry_run: bool = False,
) -> PoolSnapshot:
    """Run one monthly refresh cycle and (unless ``dry_run``) persist results.

    Args:
        store: Store to read/write. Defaults to the standard store path.
        datasource: Data source for fetching metrics. Defaults to a fresh
            :class:`XueqiuDataSource` using the resolved token.
        config: Thresholds/weights. Defaults to :data:`DEFAULTS`.
        dry_run: When True, compute the decision but do NOT write to the store.

    Returns:
        The :class:`PoolSnapshot` describing this cycle's decisions.
    """
    store = store or InfluencerPoolStore()
    datasource = datasource or XueqiuDataSource()
    config = config or DEFAULTS

    _ensure_seeds_seeded(store)

    pool = store.load_pool()
    seeds = store.load_seeds()
    pool_size_before = len([i for i in pool.values() if i.status.value in ("active", "probation")])

    # Candidates = seed uids not currently in the (active+probation) pool.
    active_uids = {uid for uid, i in pool.items() if i.status.value in ("active", "probation")}
    candidate_uids = [uid for uid in seeds.keys() if uid not in active_uids]

    logger.info(
        "refresh start: %d members, %d candidates, dry_run=%s",
        pool_size_before, len(candidate_uids), dry_run,
    )

    # Fetch + score everyone.
    all_uids = list(active_uids) + candidate_uids
    scores: Dict[str, ScoreBreakdown] = {}
    metrics_by_uid: Dict[str, InfluencerMetrics] = {}
    for uid in all_uids:
        m = datasource.fetch_all(uid)
        metrics_by_uid[uid] = m
        scores[uid] = score_bigv(m.to_metrics_dict(), weights=config.weights)

    # Decisions (seeds passed so newly-added members inherit screen_name).
    decisions = _decide(pool, seeds, scores, config)
    snapshot = PoolSnapshot(
        date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        pool_size_before=pool_size_before,
        pool_size_after=pool_size_before + len(decisions["added"]) - len(decisions["removed"]),
        added=decisions["added"],
        removed=decisions["removed"],
        promoted=decisions["promoted"],
        scores={uid: sb.to_dict() for uid, sb in scores.items()},
        config=config.to_dict(),
    )

    if dry_run:
        logger.info("dry run — not persisting. decisions: %s", _summary(snapshot))
        return snapshot

    # Apply decisions to the pool and persist.
    _apply_decisions(pool, decisions, metrics_by_uid, scores, seeds, config)
    store.save_pool(pool)
    store.append_snapshot(snapshot)
    logger.info("refresh complete: %s", _summary(snapshot))
    return snapshot


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------


def _decide(
    pool: Dict[str, Influencer],
    seeds: Dict[str, Dict[str, str]],
    scores: Dict[str, ScoreBreakdown],
    config: RefreshConfig,
) -> Dict[str, List[str]]:
    """Pure decision function — no I/O. Returns {added, removed, promoted}."""
    now_ms = int(time.time() * 1000)
    promoted: List[str] = []
    removed: List[str] = []
    added: List[str] = []

    # 1. Promote probation members whose observation window has elapsed.
    for uid, inf in pool.items():
        if inf.status == PoolStatus.PROBATION and (now_ms - inf.added_at) >= config.probation_ms:
            promoted.append(uid)

    # 2. Exit: active members (now including just-promoted) below exit threshold.
    promoted_set = set(promoted)
    for uid, inf in pool.items():
        if inf.status != PoolStatus.ACTIVE and uid not in promoted_set:
            continue
        sb = scores.get(uid)
        if sb is None or math.isnan(sb.total):
            continue  # can't score → don't remove (avoid ejecting on data gaps)
        if sb.total < config.exit_threshold:
            removed.append(uid)

    # 3. Enter: seed candidates above entry threshold, up to pool cap.
    active_count = sum(
        1 for i in pool.values()
        if i.status.value in ("active", "probation") and i.uid not in removed
    )
    # Rank candidates by score desc.
    ranked_candidates = sorted(
        ((uid, scores[uid]) for uid in seeds.keys() if uid not in pool),
        key=lambda kv: kv[1].total,
        reverse=True,
    )
    for uid, sb in ranked_candidates:
        if active_count >= config.pool_cap:
            break
        if math.isnan(sb.total) or sb.total < config.entry_threshold:
            continue
        added.append(uid)
        active_count += 1

    return {"added": added, "removed": removed, "promoted": promoted}


def _apply_decisions(
    pool: Dict[str, Influencer],
    decisions: Dict[str, List[str]],
    metrics_by_uid: Dict[str, InfluencerMetrics],
    scores: Dict[str, ScoreBreakdown],
    seeds: Dict[str, Dict[str, str]],
    config: RefreshConfig,
) -> None:
    """Mutate `pool` in place to apply the decisions, attaching fresh metrics."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_ms = int(time.time() * 1000)

    # Remove.
    for uid in decisions["removed"]:
        pool.pop(uid, None)

    # Promote.
    for uid in decisions["promoted"]:
        if uid in pool:
            pool[uid].status = PoolStatus.ACTIVE

    # Add — inherit screen_name from metrics or seed metadata.
    for uid in decisions["added"]:
        m = metrics_by_uid.get(uid)
        seed_meta = seeds.get(uid, {})
        name = (
            (m.screen_name if m and m.screen_name else "")
            or seed_meta.get("screen_name", "")
            or uid
        )
        inf = Influencer(
            uid=uid,
            screen_name=name,
            added_at=now_ms,
            status=PoolStatus.PROBATION,
            note=seed_meta.get("note", ""),
        )
        pool[uid] = inf

    # Refresh metrics + score history for every remaining member.
    for uid, inf in pool.items():
        m = metrics_by_uid.get(uid)
        if m:
            inf.raw_metrics = m.to_metrics_dict()
            inf.last_refreshed_at = now_ms
            if m.screen_name:
                inf.screen_name = m.screen_name
        sb = scores.get(uid)
        if sb is not None:
            inf.score_history.append({"date": date_str, "breakdown": sb.to_dict()})
            # Cap history to last 12 entries (one year).
            if len(inf.score_history) > 12:
                inf.score_history = inf.score_history[-12:]


# ---------------------------------------------------------------------------
# Seed bootstrap
# ---------------------------------------------------------------------------


def _ensure_seeds_seeded(store: InfluencerPoolStore) -> None:
    """On first run (empty seeds), populate from the built-in defaults."""
    existing = store.load_seeds()
    if existing:
        return
    for entry in default_seed_entries():
        existing[entry["uid"]] = {
            "screen_name": entry["screen_name"],
            "note": entry.get("note", ""),
        }
    store.save_seeds(existing)
    logger.info("seeded %d default candidates", len(existing))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.influencer.refresh",
        description="Monthly refresh of the Xueqiu influencer pool.",
    )
    p.add_argument("--dry-run", action="store_true", help="Compute decisions without persisting.")
    p.add_argument("--pool-cap", type=int, default=DEFAULTS.pool_cap)
    p.add_argument("--entry", type=float, default=DEFAULTS.entry_threshold, help="entry score threshold")
    p.add_argument("--exit", type=float, default=DEFAULTS.exit_threshold, help="exit score threshold")
    p.add_argument("--probation-days", type=int, default=31)
    p.add_argument("--add-seed", metavar="UID", help="add a seed candidate (use with --name)")
    p.add_argument("--name", help="screen name for --add-seed")
    p.add_argument("--note", default="", help="note for --add-seed")
    p.add_argument("--remove-seed", metavar="UID", help="remove a seed candidate")
    p.add_argument("--list", action="store_true", help="list current pool + seeds and exit")
    p.add_argument("--json", action="store_true", help="emit snapshot as JSON to stdout")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = _build_parser().parse_args(argv)
    store = InfluencerPoolStore()

    # Seed management sub-commands (no refresh).
    if args.add_seed:
        store.add_seed(args.add_seed, args.name or args.add_seed, args.note)
        print(f"added seed {args.add_seed}")
        return 0
    if args.remove_seed:
        removed = store.remove_seed(args.remove_seed)
        print(f"removed seed {args.remove_seed}: {removed}")
        return 0
    if args.list:
        _print_pool(store)
        return 0

    config = RefreshConfig(
        pool_cap=args.pool_cap,
        entry_threshold=args.entry,
        exit_threshold=args.exit,
        probation_days=args.probation_days,
    )
    snapshot = refresh_pool(store=store, config=config, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_snapshot(snapshot)
    return 0


def _print_pool(store: InfluencerPoolStore) -> None:
    pool = store.list_active()
    seeds = store.load_seeds()
    print(f"== Pool ({len(pool)} members) ==")
    for inf in pool:
        last_score = inf.score_history[-1]["breakdown"]["total"] if inf.score_history else "n/a"
        print(f"  [{inf.status.value:9}] {inf.uid:>12}  {inf.screen_name:<20}  score={last_score}")
    print(f"\n== Seeds ({len(seeds)} candidates) ==")
    for uid, meta in seeds.items():
        print(f"  {uid:>12}  {meta.get('screen_name', ''):<20}  {meta.get('note', '')}")


def _print_snapshot(s: PoolSnapshot) -> None:
    print(_summary(s))


def _summary(s: PoolSnapshot) -> str:
    return (
        f"snapshot {s.date}: pool {s.pool_size_before}→{s.pool_size_after} "
        f"(+{len(s.added)} -{len(s.removed)} ~{len(s.promoted)} promoted) "
        f"entry={s.config.get('entry_threshold')} exit={s.config.get('exit_threshold')}"
    )


if __name__ == "__main__":
    sys.exit(main())
