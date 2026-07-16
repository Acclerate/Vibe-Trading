"""custom_2026_style_rotation: 风格轮动动量因子。

灵感来源:掘金量化风格轮动策略(上证50/沪深300/中证500 三选一动量轮动)。

核心逻辑:
  1. 对三个风格指数计算 20 日动量。
  2. 计算每只个股与三个指数的 60 日收益率相关性作为风格权重。
  3. 因子得分 = 个股对各风格的相关性权重 × 对应风格 20 日动量之和。
     → 本质是「个股的风格动量加权暴露」。
     属于当前最强风格(且相关性高)的个股获得高分。

这样因子输出在横截面上有个体差异(不同风格归属的股票得分不同),
经 z-score 后能有效区分。

输入:
  panel 需包含 extras: index_hs50, index_hs300, index_zz500
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__alpha_meta__ = {
    "id": "custom_2026_style_rotation",
    "theme": ["momentum", "sentiment"],
    "formula_latex": r"\text{score}_i = \sum_{k \in \{50,300,500\}} \rho_{60}(r_i, r_{I^k}) \cdot \text{mom}_{20}(I^k)",
    "columns_required": ["close"],
    "extras_required": ["index_hs50", "index_hs300", "index_zz500"],
    "universe": ["equity_cn"],
    "frequency": ["1d"],
    "decay_horizon": 20,
    "min_warmup_bars": 65,
    "nickname": "风格轮动",
    "notes": "个股与三指数的60日相关性加权 × 对应风格20日动量。需load_panel注入index_*数据。",
}


def compute(panel):
    """风格轮动动量因子(相关性加权风格动量)。

    每只股票得分 = Σ(与风格k的60日相关性 × 风格k的20日动量)。
    """
    close = panel["close"]

    # 取三指数(每列是指数收盘,取均值得到标量序列)
    idx_50 = panel["index_hs50"].mean(axis=1)
    idx_300 = panel["index_hs300"].mean(axis=1)
    idx_500 = panel["index_zz500"].mean(axis=1)

    # 三指数 20 日动量(每日标量 Series)
    lookback = 20
    mom_50 = idx_50 / idx_50.shift(lookback) - 1.0
    mom_300 = idx_300 / idx_300.shift(lookback) - 1.0
    mom_500 = idx_500 / idx_500.shift(lookback) - 1.0

    # 个股与三指数的 60 日收益率相关性(wide DataFrame,与 close 同 shape)
    corr_window = 60
    stock_ret = close.pct_change()
    idx50_ret = idx_50.pct_change()
    idx300_ret = idx_300.pct_change()
    idx500_ret = idx_500.pct_change()

    # 向量化:每只股票对每个指数算滚动相关性
    corr_50 = stock_ret.rolling(corr_window).corr(idx50_ret)
    corr_300 = stock_ret.rolling(corr_window).corr(idx300_ret)
    corr_500 = stock_ret.rolling(corr_window).corr(idx500_ret)

    # 广播:将每日标量动量乘到 wide 相关性矩阵上
    # mom_* 是 Series(index=date), corr_* 是 DataFrame(date×code)
    result = (corr_50.mul(mom_50, axis=0) +
              corr_300.mul(mom_300, axis=0) +
              corr_500.mul(mom_500, axis=0))

    return result
