"""custom_2026_llm_amplitude_zscore_reversal: 短期振幅相对长期均值的Z-Score均值回归。"""
from src.factors.base import safe_div, ts_mean, ts_std

__alpha_meta__ = {
    'id': 'custom_2026_llm_amp_zscore',
    'theme': ['volatility', 'microstructure'],
    'formula_latex': r'-\frac{\mu\left(\frac{H_t-L_t}{C_t}, 5\right) - \mu\left(\frac{H_t-L_t}{C_t}, 20\right)}{\sigma\left(\frac{H_t-L_t}{C_t}, 20\right)}',
    'columns_required': ['high', 'low', 'close'],
    'universe': ['equity_cn'],
    'frequency': ['1d'],
    'decay_horizon': 10,
    'min_warmup_bars': 25,
}

def compute(panel):
    h = panel['high']
    l = panel['low']
    c = panel['close']
    amp = safe_div(h - l, c)
    return -safe_div(ts_mean(amp, 5) - ts_mean(amp, 20), ts_std(amp, 20))