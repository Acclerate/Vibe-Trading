"""custom_2026_llm_close_loc_momentum: 收盘位置的短期均值偏离长期均值,刻画买盘力量的趋势性变化。"""
from __future__ import annotations
from src.factors.base import ts_mean, safe_div, decay_linear

__alpha_meta__ = {
    'id': 'custom_2026_llm_close_lo_7eaebf',
    'theme': ['microstructure', 'momentum'],
    'formula_latex': r'\mathrm{mean}_{5}\!\left(\frac{close-low}{high-low}\right) - \mathrm{mean}_{20}\!\left(\frac{close-low}{high-low}\right)',
    'columns_required': ['high', 'low', 'close'],
    'universe': ['equity_cn'],
    'frequency': ['1d'],
    'decay_horizon': 5,
    'min_warmup_bars': 20,
}

def compute(panel):
    h, l, c = panel['high'], panel['low'], panel['close']
    loc = safe_div(c - l, h - l)
    return decay_linear(ts_mean(loc, 5) - ts_mean(loc, 20), 5)