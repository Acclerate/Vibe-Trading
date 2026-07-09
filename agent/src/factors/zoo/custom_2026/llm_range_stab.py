"""custom_2026_llm_intraday_range_stability: 日内振幅的波动稳定性(A股低波动异象)。"""
from src.factors.base import safe_div, ts_std

__alpha_meta__ = {
    'id': 'custom_2026_llm_range_stab',
    'theme': ['volatility', 'microstructure'],
    'formula_latex': r'-\sigma\left(\frac{H_t-L_t}{C_t}, 20\right)',
    'columns_required': ['high', 'low', 'close'],
    'universe': ['equity_cn'],
    'frequency': ['1d'],
    'decay_horizon': 15,
    'min_warmup_bars': 20,
}

def compute(panel):
    h = panel['high']
    l = panel['low']
    c = panel['close']
    amp = safe_div(h - l, c)
    return -ts_std(amp, 20)