"""custom_2026_llm_true_range_premium: 真实波动幅度相对日内振幅的溢价,刻画跳空与延续性。"""
from __future__ import annotations
from src.factors.base import ts_mean, safe_div, delta, decay_linear

__alpha_meta__ = {
    'id': 'custom_2026_llm_true_ran_18b197',
    'theme': ['volatility', 'microstructure'],
    'formula_latex': r'-\, \mathrm{mean}_{20}\!\left(\frac{\max(high,close_{t-1}) - \min(low,close_{t-1})}{high-low}\right)',
    'columns_required': ['high', 'low', 'close'],
    'universe': ['equity_cn'],
    'frequency': ['1d'],
    'decay_horizon': 10,
    'min_warmup_bars': 21,
}

def compute(panel):
    import numpy as np
    h, l, c = panel['high'], panel['low'], panel['close']
    pc = c.shift(1)
    true_range = np.maximum(h, pc) - np.minimum(l, pc)
    intraday = h - l
    premium = safe_div(true_range, intraday)
    return decay_linear(-ts_mean(premium, 20), 10)