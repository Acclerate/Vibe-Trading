"""custom_2026_llm_high_vol_weak_close: 振幅放大且收盘疲软综合异象。"""
from src.factors.base import safe_div, ts_std, ts_mean

__alpha_meta__ = {
    'id': 'custom_2026_llm_hvol_wclose',
    'theme': ['volatility', 'microstructure'],
    'formula_latex': r'\sigma\left(\frac{H_t-L_t}{C_t}, 10\right) \times \mu\left(\frac{L_t-C_t}{H_t-L_t}, 10\right)',
    'columns_required': ['high', 'low', 'close'],
    'universe': ['equity_cn'],
    'frequency': ['1d'],
    'decay_horizon': 10,
    'min_warmup_bars': 15,
}

def compute(panel):
    h = panel['high']
    l = panel['low']
    c = panel['close']
    amp_vol = ts_std(safe_div(h - l, c), 10)
    weak_close = ts_mean(safe_div(l - c, h - l), 10)
    return amp_vol * weak_close