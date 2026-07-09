"""custom_2026_llm_volume_price_corr_10d: 10 日量价相关性。"""
from __future__ import annotations
from src.factors.base import ts_corr

__alpha_meta__ = {
    'id': 'custom_2026_llm_volume_p_a7bc4c',
    'theme': ['volume', 'microstructure'],
    'formula_latex': r'\text{ts\_corr}(\text{close}, \text{volume}, 10)',
    'columns_required': ['close', 'volume'],
    'universe': ['equity_cn'],
    'frequency': ['1d'],
    'decay_horizon': 5,
    'min_warmup_bars': 15,
}

def compute(panel):
    return ts_corr(panel['close'], panel['volume'], 10)