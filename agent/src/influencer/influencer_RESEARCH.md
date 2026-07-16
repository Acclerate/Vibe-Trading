# 雪球大V动态名单池 — 研究档案

**研究日期**: 2026-07-16
**状态**: ✅ 已实现 — 数据管线 + 评分 + 月度刷新 + CLI

---

## 一、目标

维护一个 **50 人小池子**的雪球高价值大V名单,每月用动态评分函数评估,
**严格进出**(进出阈值分离,防抖动),CLI 手动触发。池子里的大V后续可作为
另类数据源(情绪/调仓信号)接入因子 zoo,但那是独立任务,本期不涉及。

## 二、核心设计决策(及调研依据)

### 2.1 数据层:为什么是"混合"而不是纯 pysnowball

源码核查 `uname-yang/pysnowball`(936★, 2025-09 仍维护)后发现:**它对大V
社交数据几乎零覆盖**。下表是规划阶段实际读源码得到的结论:

| 评分所需数据 | pysnowball 支持? | 备注 |
|---|---|---|
| 用户资料(粉丝/简介) | ❌ | `user.py` 其实是"自选股" |
| 发帖 timeline(点赞/评论) | ❌ | 无 timeline/statuses 模块 |
| 关注/粉丝列表 | ❌ | 无 follow 模块 |
| 用户名下所有组合 | ❌ | 无法按 uid 列组合 |
| 排行榜(发现大V) | ❌ | `cube.top_n` 名字误导,实为查单组合 |
| 单组合详情(年化/回撤/夏普) | ✅ | `cube.detail(ZH...)`,但需先知道 cube ID |

**结论**:pysnowball 5 个评分维度里只覆盖 1 个(且需先有 cube ID)。因此采用
**混合策略** —— 复用 pysnowball 的 token 机制(`set_token` / `XUEQIUTOKEN`),
社交数据直接调 xueqiu.com 网页接口,全部封装在 `XueqiuDataSource` 里,评分层
不感知数据来源。

### 2.2 降级契约(对抗反爬/token 失效)

雪球 token 是浏览器 cookie,易过期;反爬会随机触发。**任何单个维度抓取失败
都不得中断整个 refresh**。设计:

- `XueqiuDataSource._safe()` 把每个 endpoint 的异常吞掉 → 该维度返回 NaN
- `score_bigv()` 对 NaN 维度**排除并重新归一化权重**(不是简单置 0)
- **唯一例外**:`performance` 维度,无组合 = "没真金白银验证",记 0 惩罚,
  且**计入权重**(不是缺失)
- token 完全缺失时 → 所有维度 NaN,refresh 仍跑完(产出空快照),不崩溃

### 2.3 发现机制:种子集 + 手动维护(不抓关注列表)

主动放弃"从种子大V的关注列表社交图扩展"方案,原因:
- 雪球关注列表接口最不稳定,反爬最严
- 社交图扩展会引入大量低质量候选,反而稀释信号
- 手动维护种子集 30 个,月度评估足够

种子集存储在 store 的 `seeds` 段,首次运行从 `seeds.DEFAULT_SEEDS` 引导,
后续用 CLI `--add-seed` / `--remove-seed` 维护。

## 三、评分函数(score_bigv)

### 3.1 五维加权(默认权重)

| 维度 | 权重 | 含义 | 归一化方式 |
|------|------|------|-----------|
| influence | 0.20 | 粉丝量(对数压缩) | `log10(followers) / log10(1M)` |
| engagement | 0.20 | 互动率(剔僵尸粉) | `avg_likes / (followers×0.1) / 10%` |
| originality | 0.15 | 原创+长文 | `0.5×原创率 + 0.5×长文率(饱和0.5)` |
| **performance** | **0.35** | **组合风险调整收益(最重要)** | `均值(年化, 夏普, 卡尔马)` |
| activity | 0.10 | 发帖频率 | `posts_30d / 30` |

**performance 权重最高(0.35)**:因为真金白银的 P&L 是最难造假的信号。
一个 50 万粉但组合常年亏损的大V,价值远低于 5 万粉但年化 20% 的主理人。

### 3.2 归一化锚点(scoring.py `_ANCHORS`)

```
influence_followers_saturation     = 1,000,000   # 100万粉 = 满分
engagement_rate_saturation         = 0.10        # 10% 互动率 = 满分(平台均2.3%)
originality_long_post_ratio_saturation = 0.50    # 一半长文 = 满分
performance_annual_return_saturation   = 0.30    # 30% 年化 = 满分
performance_sharpe_saturation          = 2.0     # 夏普 2.0 = 满分
performance_calmar_saturation          = 3.0     # 卡尔马 3.0 = 满分
activity_posts_per_30d_saturation      = 30      # 日均1帖 = 满分
```

### 3.3 缺失维度处理(关键)

- influence/engagement/originality/activity 缺失 → **排除,权重重分配到其他维度**
  (保证 total 仍在 [0,1])
- performance 缺失(无组合) → **强制 0,且计入权重**(惩罚"嘴盘")
- 这样一个只有组合数据、无社交活跃的大V,仍能拿到 performance 维度的分;
  一个只有粉丝、无组合的大V,performance 拖累总分

## 四、月度进出策略(严格进出 + PROBATION)

### 4.1 阈值(默认,CLI 可覆盖)

```
pool_cap        = 50      # 池子上限
entry_threshold = 0.55    # 候选 ≥ 0.55 才可进入
exit_threshold  = 0.35    # 成员 < 0.35 才被移除
probation_days  = 31      # 新进成员 31 天保护期
```

**进出阈值分离(0.55 vs 0.35)**:避免边界值附近的成员反复进出(抖动)。
一个 0.45 分的大V既进不来也出不去,稳定在候选态。

### 4.2 PROBATION 保护期(反 churn)

新进成员标记 `PROBATION`,**首月不可被移除**(即使分数暴跌)。满 31 天后
自动转 `ACTIVE` 才开放移除资格。这防止:
- 单次抓取失败(NaN)误杀新成员
- 新成员刚好赶上数据低谷被秒踢

### 4.3 NaN 分数保护

任何成员本月分数为 NaN(数据全缺)→ **不移除**。宁可保留也不在数据缺口上
做不可逆决策。

## 五、文件结构

```
agent/src/influencer/
├── __init__.py              # 公开 API 导出
├── models.py                # Influencer / PoolSnapshot / ScoreBreakdown / PoolStatus
├── store.py                 # Crash-safe JSON 持久化(仿 scheduled_research/store.py)
├── seeds.py                 # 内置种子集(首次运行引导;需手动校验 uid)
├── datasource.py            # XueqiuDataSource(混合 pysnowball token + 直接调雪球)
├── scoring.py               # score_bigv() + 五维归一化
├── refresh.py               # refresh_pool() 主流程 + CLI(python -m src.influencer.refresh)
└── influencer_RESEARCH.md   # 本文档

agent/tests/
├── test_influencer_store.py     # 22 个:持久化/损坏隔离/CRUD/快照
├── test_influencer_scoring.py   # 25 个:五维归一化/缺失处理/边界
└── test_influencer_refresh.py   # 22 个:进出决策/PROBATION/池上限/全流程
```

数据落点:`~/.vibe-trading/influencer/influencer_pool.json`(用户态,非仓库内)。

## 六、使用方式

### 6.1 配置 token

```bash
# agent/.env
XUEQIU_TOKEN=你的xq_a_token_cookie值
```

获取方式:浏览器打开 xueqiu.com → DevTools → Application → Cookies →
复制 `xq_a_token` 的值。

### 6.2 维护种子集

```bash
# 添加候选大V
python -m src.influencer.refresh --add-seed 1234567890 --name "某大V" --note "价值投资"

# 移除候选
python -m src.influencer.refresh --remove-seed 1234567890

# 查看当前池子 + 种子
python -m src.influencer.refresh --list
```

### 6.3 月度刷新

```bash
# 先 dry-run 看决策
python -m src.influencer.refresh --dry-run

# 正式执行(写入 store + 快照)
python -m src.influencer.refresh

# JSON 输出(便于脚本消费)
python -m src.influencer.refresh --json

# 自定义阈值
python -m src.influencer.refresh --pool-cap 30 --entry 0.60 --exit 0.30
```

## 七、待办与风险

### 7.1 种子集需人工校验 ⚠️

`seeds.DEFAULT_SEEDS` 当前为**空**(有意为之)。原因:我无法实时验证雪球
uid 的有效性,且 uid 会随账号更名/回收变化。**首次使用前必须手动添加**:

```bash
# 浏览器访问 https://xueqiu.com/<uid> 确认账号正确后:
python -m src.influencer.refresh --add-seed <uid> --name "<昵称>" --note "<风格>"
```

建议初始种子 20-30 个,覆盖:价值投资/趋势/量化/行业研究等风格。

### 7.2 已知限制

- **token 易过期**:雪球 cookie 通常几天到两周失效;失效时 refresh 降级为
  全 NaN,不报错但不产出有效分数 → 需定期更新 `.env`
- **反爬**:timeline 接口最易触发滑块验证;datasource 已加 0.6s 限速 +
  retry_with_budget,但大规模抓取仍可能被限
- **单组合代理**:`performance` 只取用户第一个组合的详情;若用户有多个
  组合,可能不代表其最优策略(后续可改为取收益最高的)
- **无情感分析**:本期只做名单维护,不做帖子内容情感(那是后续 alpha 因子任务)

### 7.3 后续路线(不在本期)

1. 大V情绪因子 → 接入 `factors/zoo/custom_2026/`
2. 大V调仓跟随信号(检测组合 rebalancing_history 变化)
3. 多组合取最优(按夏普/收益排序选代表组合)
4. token 自动刷新(模拟登录流程,合规风险需评估)
