"""custom_2026_llm_volume_price_rank_corr_divergence: 量价排名相关背离（长期）。"""
from __future__ import annotations
from src.factors.base import ts_corr, ts_rank

__alpha_meta__ = {
    'id': 'custom_2026_llm_volume_p_2c742f',
    'theme': ['volume', 'microstructure'],
    'formula_latex': r'-\text{ts\_corr}\!\big(\text{ts\_rank}(\text{close},10),\;\text{ts\_rank}(\text{volume},10),\;20\big)',
    'columns_required': ['close', 'volume'],
    'universe': ['equity_cn'],
    'frequency': ['1d'],
    'decay_horizon': 10,
    'min_warmup_bars': 35,
}

def compute(panel):
    return -ts_corr(ts_rank(panel['close'], 10), ts_rank(panel['volume'], 10), 20)