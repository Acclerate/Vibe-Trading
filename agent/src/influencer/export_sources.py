"""Bridge: export the influencer pool into the snowball-follow skill's format.

The snowball-follow skill (agent/src/skills/snowball-follow/) tracks a list of
大V and generates daily digests. Its ``sources`` config is a list of
``{name, slug, tag}`` objects. This script exports the *current active pool*
from :mod:`src.influencer` into that format, so the digest tracks exactly the
big-Vs that the monthly refresh has admitted — one source of truth.

Usage::

    python -m src.influencer.export_sources --output ~/.snowball-follow/sources.json
    # or print to stdout
    python -m src.influencer.export_sources

Only ACTIVE + PROBATION pool members with a non-pending uid are exported;
PENDING_VERIFICATION placeholders are skipped (they can't be fetched). Verified
seeds not yet in the pool are also included as candidates, so the digest can
run even before the first monthly refresh.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

from src.influencer.seeds import SEED_MAP, is_pending
from src.influencer.store import InfluencerPoolStore


def _slug_from_uid(uid: str) -> str:
    """Return the slug to use in a source entry.

    For numeric uids we pass them through (the skill accepts numeric IDs as
    slugs). PENDING uids should never reach here — filtered upstream.
    """
    return uid


def export_sources(
    store: InfluencerPoolStore | None = None,
    include_seeds: bool = True,
) -> List[Dict[str, Any]]:
    """Build the snowball-follow ``sources`` list from the influencer pool.

    Args:
        store: Pool store. Defaults to the standard store path.
        include_seeds: When True (default), also include verified seed entries
            that are not yet in the pool — useful before the first refresh.

    Returns:
        A list of ``{name, slug, tag}`` dicts, de-duplicated by uid.
    """
    store = store or InfluencerPoolStore()
    sources: List[Dict[str, Any]] = []
    seen_uids: set[str] = set()

    # 1. Pool members first (these are the refresh-admitted big-Vs).
    for inf in store.list_active():
        if is_pending(inf.uid):
            continue  # skip unverified placeholders
        sources.append({
            "name": inf.screen_name,
            "slug": _slug_from_uid(inf.uid),
            "tag": inf.note or "",
        })
        seen_uids.add(inf.uid)

    # 2. Verified seeds not yet in the pool (so digest works pre-refresh).
    if include_seeds:
        for uid, meta in SEED_MAP.items():
            if is_pending(uid) or uid in seen_uids:
                continue
            sources.append({
                "name": meta["screen_name"],
                "slug": _slug_from_uid(uid),
                "tag": meta.get("note", ""),
            })
            seen_uids.add(uid)

    return sources


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.influencer.export_sources",
        description="Export the influencer pool as snowball-follow skill sources.",
    )
    parser.add_argument(
        "--output", "-o",
        help="Output JSON file path. Omit to print to stdout.",
    )
    parser.add_argument(
        "--no-seeds",
        action="store_true",
        help="Exclude verified seeds not yet in the pool (pool members only).",
    )
    args = parser.parse_args(argv)

    sources = export_sources(include_seeds=not args.no_seeds)
    payload = {"authors": sources}  # match default-sources.json shape
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {len(sources)} sources to {args.output}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
