"""custom_2026_llm_close_loc_stability: 收盘位置稳定性——高且稳定的日内收盘位置代表持续买盘。"""
from __future__ import annotations
from src.factors.base import ts_mean, ts_std, safe_div, decay_linear

__alpha_meta__ = {
    'id': 'custom_2026_llm_close_lo_5d7f01',
    'theme': ['microstructure', 'volatility'],
    'formula_latex': r'\mathrm{mean}_{20}\!\left(\frac{close-low}{high-low}\right) - \mathrm{std}_{20}\!\left(\frac{close-low}{high-low}\right)',
    'columns_required': ['high', 'low', 'close'],
    'universe': ['equity_cn'],
    'frequency': ['1d'],
    'decay_horizon': 10,
    'min_warmup_bars': 20,
}

def compute(panel):
    h, l, c = panel['high'], panel['low'], panel['close']
    loc = safe_div(c - l, h - l)
    return decay_linear(ts_mean(loc, 20) - ts_std(loc, 20), 10)