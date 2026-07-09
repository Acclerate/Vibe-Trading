"""custom_2026_llm_intraday_range_skew: 日内振幅相对其均值的偏度,捕捉尾部风险形态。"""
from __future__ import annotations
from src.factors.base import ts_mean, ts_std, safe_div, decay_linear, signed_power

__alpha_meta__ = {
    'id': 'custom_2026_llm_intraday_c90995',
    'theme': ['volatility', 'microstructure'],
    'formula_latex': r'-\, \mathrm{mean}_{20}\!\left[\left(\frac{(high-low)/close - \mu}{\sigma}\right)^{3}\right]',
    'columns_required': ['high', 'low', 'close'],
    'universe': ['equity_cn'],
    'frequency': ['1d'],
    'decay_horizon': 5,
    'min_warmup_bars': 20,
}

def compute(panel):
    h, l, c = panel['high'], panel['low'], panel['close']
    rng = safe_div(h - l, c)
    mu, sd = ts_mean(rng, 20), ts_std(rng, 20)
    z = safe_div(rng - mu, sd)
    return decay_linear(-ts_mean(signed_power(z, 3), 20), 5)