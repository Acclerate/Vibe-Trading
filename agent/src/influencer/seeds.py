"""Built-in seed candidate list for the influencer pool.

The seed list is the source of *candidates* — users who are not yet in the
pool but should be evaluated each refresh. Candidates that score above the
entry threshold get promoted into the pool; the seed list itself is
persistent (managed via :class:`~src.influencer.store.InfluencerPoolStore`)
and only seeded from :data:`DEFAULT_SEEDS` on first run.

The uids below are well-known public Xueqiu accounts. **They must be
verified before relying on them** — uids can change as accounts are
renamed/recycled. See ``influencer_RESEARCH.md`` for how to validate and
extend this list. The list is deliberately small and conservative; extend it
via ``python -m src.influencer.refresh --add-seed <uid> --name <name>``.
"""

from __future__ import annotations

from typing import Dict, List

# Each entry: uid (string), screen_name, note (style/circle).
# NOTE: uids are placeholders pending validation — see RESEARCH.md.
DEFAULT_SEEDS: Dict[str, Dict[str, str]] = {
    # --- value investors / well-known public accounts ---
    # Verify each uid by visiting https://xueqiu.com/<uid> in a browser.
}


def default_seed_uids() -> List[str]:
    """Return the uids of :data:`DEFAULT_SEEDS` in stable order."""
    return list(DEFAULT_SEEDS.keys())


def default_seed_entries() -> List[Dict[str, str]]:
    """Return DEFAULT_SEEDS as a list of ``{uid, screen_name, note}`` dicts."""
    return [
        {"uid": uid, "screen_name": meta["screen_name"], "note": meta.get("note", "")}
        for uid, meta in DEFAULT_SEEDS.items()
    ]
