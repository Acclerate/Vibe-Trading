"""custom_2026_llm_volume_price_rank_divergence: 量价动量排名差（缩量抗跌 / 放量滞涨）。"""
from __future__ import annotations
from src.factors.base import delta, safe_div, rank

__alpha_meta__ = {
    'id': 'custom_2026_llm_volume_p_db3630',
    'theme': ['volume', 'reversal'],
    'formula_latex': r'\text{rank}\!\left(\frac{\Delta_5\,\text{close}}{\text{close}_{t-5}}\right) - \text{rank}\!\left(\frac{\Delta_5\,\text{volume}}{\text{volume}_{t-5}}\right)',
    'columns_required': ['close', 'volume'],
    'universe': ['equity_cn'],
    'frequency': ['1d'],
    'decay_horizon': 5,
    'min_warmup_bars': 10,
}

def compute(panel):
    c = panel['close']
    v = panel['volume']
    return rank(safe_div(delta(c, 5), c.shift(5))) - rank(safe_div(delta(v, 5), v.shift(5)))