"""custom_2026 example: 5 日动量(用于验证 zoo 自动发现机制)。"""
from __future__ import annotations

from src.factors.base import delta, safe_div

__alpha_meta__ = {
    "id": "custom_2026_example_5d_momentum",
    "theme": ["momentum"],
    "formula_latex": r"(close_t - close_{t-5}) / close_{t-5}",
    "columns_required": ["close"],
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 5,
    "min_warmup_bars": 5,
}


def compute(panel):
    """5 日动量。"""
    c = panel["close"]
    return safe_div(delta(c, 5), c.shift(5))
