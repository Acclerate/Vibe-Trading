"""Built-in seed candidate list for the influencer pool.

The seed list is the source of *candidates* — users who are not yet in the
pool but should be evaluated each refresh. Candidates that score above the
entry threshold get promoted into the pool; the seed list itself is
persistent (managed via :class:`~src.influencer.store.InfluencerPoolStore`)
and only seeded from :data:`SEED_MAP` on first run.

UID verification status
-----------------------
Only UIDs that have been verified by loading ``https://xueqiu.com/<uid>`` in
a browser and confirming the screen_name + bio match are filled in. Entries
without a verified UID use a ``PENDING_VERIFICATION_<n>`` placeholder key plus
the known screen_name in the ``note`` — the fetcher skips these and logs a
warning until you replace the placeholder with the real numeric UID.

How to resolve a pending entry
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
1. Open ``https://xueqiu.com`` and search the screen_name.
2. Open the matching profile (check the bio matches the note).
3. Copy the numeric UID from the profile URL (``xueqiu.com/<UID>``).
4. ``python -m src.influencer.refresh --remove-seed PENDING_VERIFICATION_3``
   then ``--add-seed <UID> --name <昵称> --note <风格>``
   (or edit the lists below and delete the store file to reseed).
"""

from __future__ import annotations

from typing import Dict, List

# Sentinel prefix for entries whose numeric UID is not yet verified. The
# fetcher treats any uid starting with this as "skip" rather than fetching a
# wrong account. Suffix (_1, _2, ...) disambiguates multiple pending entries.
PENDING_PREFIX = "PENDING_VERIFICATION"


# ---------------------------------------------------------------------------
# Verified seeds (numeric UID confirmed against the live profile page).
# ---------------------------------------------------------------------------

# Each: uid -> {screen_name, note}. Source-order matters for stable display.
VERIFIED_SEEDS: Dict[str, Dict[str, str]] = {
    # --- 价值投资 / 底层逻辑 ---
    "1247347556": {
        "screen_name": "大道无形我有型",
        "note": "段永平；顶级企业家价投，重仓苹果/茅台/腾讯，商业模式与护城河，买股票=买公司",
    },
    # --- 消费 / 白酒 ---
    "3491303582": {
        "screen_name": "闲来一坐s话投资",
        "note": "张居营；《慢慢变富》作者，20+年经验，白酒/家电/医药，长期持有与消费赛道",
    },
    # --- 指数 / 定投 ---
    "3079173340": {
        "screen_name": "银行螺丝钉",
        "note": "《指数基金投资指南》作者；低估值宽基/行业指数，新手入门与定投体系",
    },
}


# ---------------------------------------------------------------------------
# Pending seeds (screen_name known, numeric UID not yet browser-verified).
# Fetched only as placeholders; never enter the pool until resolved.
# ---------------------------------------------------------------------------

PENDING_SEEDS: List[Dict[str, str]] = [
    # --- 原名单 6 位(第一/二轮选定;唐朝因 P2P 暴雷争议+停更已移除) ---
    {"screen_name": "ice_招行谷子地", "note": "银行股专家；深耕银行基本面，数据拆解与低估值策略 [uid待校验]"},
    {"screen_name": "乐趣", "note": "白酒集中投资；超长期持有白酒龙头，绝对集中+高确定性 [uid待校验]"},
    {"screen_name": "超级鹿鼎公", "note": "宏观大局+红利赛道；自上而下看周期，重仓陕煤/神华等周期蓝筹，高股息长线 [uid待校验]"},
    {"screen_name": "DAVID自由之路", "note": "徐大为；《低风险投资之路》作者，套利/高分红/可转债；用户评「雪球做低风险投资套利最出色」 [uid待校验]"},
    {"screen_name": "黄建平", "note": "医药价值派；工程师转职业投资人，深耕创新药，数据与产业视角估值 [uid待校验]"},
    {"screen_name": "张小丰", "note": "医药数据派；雪球「医药三杰」之一，强统计与数据分析，创新药/器械 [uid待校验]"},
    # --- 第二轮新增 7 位(来自知乎盘点文章) ---
    {"screen_name": "林园", "note": "消费医药传奇；只投行业垄断/刚需/永续经营龙头，垄断成长投资 [uid待校验]"},
    {"screen_name": "无思无虑无忧", "note": "逆向价值高手；逆向投资+安全边际，消费龙头/低风险套利/现金管理，散户适配度高 [uid待校验]"},
    {"screen_name": "水晶苍蝇拍", "note": "李杰；成长价值双轮驱动，体系化投资，大局观强，景气周期+成长红利共振 [uid待校验]"},
    {"screen_name": "岁寒知松柏", "note": "财务排雷专家；从财报细节挖掘造假/风险信号，筛现金流优质/估值合理的稳健标的 [uid待校验]"},
    {"screen_name": "管我财", "note": "深度估值套利；全市场(含港股)性价比选股，低估值修复+稳健增长，逆向布局冷门优质股 [uid待校验]"},
    {"screen_name": "杨爽", "note": "趋势成长派；赛道优先+择时为辅+业绩落地，捕捉行业拐点与板块主升浪，波段滚动持仓 [uid待校验]"},
    {"screen_name": "北京城西封老师", "note": "超短情绪周期；情绪/资金/周期三维一体，题材梯队与周期高低点识别，短线节奏实战 [uid待校验]"},
    # --- 第三轮新增 9 位(来自分类参考名单;已去停更/营销号) ---
    # 用户明确 endorsed:
    {"screen_name": "微光破晓", "note": "保守派投资；低估/分散/平均盈，用户评「这几年愈发感觉值得学习」 [uid待校验]"},
    # 基金研究簇(名单重点分类):
    {"screen_name": "基民柠檬", "note": "基金研究透彻；公募基金深度分析 [uid待校验]"},
    {"screen_name": "似曾相识81", "note": "老基金投资者；基金研究经验丰富 [uid待校验]"},
    {"screen_name": "张翼珍", "note": "CFA；擅长海龟交易、二八轮动，专业度高 [uid待校验]"},
    {"screen_name": "紫葳侍郎", "note": "分级基金大神；集思录分级基金顾问 [uid待校验]"},
    {"screen_name": "老牛先生", "note": "债券专业投资人；固收/债券研究 [uid待校验]"},
    # 价值/成长:
    {"screen_name": "股海十三年", "note": "深度价值投资者；老股民，深度价值 [uid待校验]"},
    {"screen_name": "价值at风险", "note": "价值投资+基本面分析；估值保守 [uid待校验]"},
    # 行业专精:
    {"screen_name": "西峯", "note": "地产行业研究透彻 [uid待校验]"},
]


def _build_seed_map() -> Dict[str, Dict[str, str]]:
    """Merge verified + pending into one uid-keyed dict.

    Pending entries get distinct keys ``PENDING_VERIFICATION_<n>`` so they
    survive as separate entries (a plain dict can't hold duplicate keys).
    """
    merged: Dict[str, Dict[str, str]] = {}
    # Verified first, in source order.
    for uid, meta in VERIFIED_SEEDS.items():
        merged[uid] = dict(meta)
    # Pending after, numbered for uniqueness.
    for i, entry in enumerate(PENDING_SEEDS, start=1):
        merged[f"{PENDING_PREFIX}_{i}"] = dict(entry)
    return merged


# The canonical, de-duplicated seed map (verified uids + disambiguated pending).
SEED_MAP: Dict[str, Dict[str, str]] = _build_seed_map()


def default_seed_uids() -> List[str]:
    """Return the uids (including pending placeholders) in stable order."""
    return list(SEED_MAP.keys())


def default_seed_entries() -> List[Dict[str, str]]:
    """Return SEED_MAP as a list of ``{uid, screen_name, note}`` dicts."""
    return [
        {"uid": uid, "screen_name": meta["screen_name"], "note": meta.get("note", "")}
        for uid, meta in SEED_MAP.items()
    ]


def is_pending(uid: str) -> bool:
    """True when *uid* is an unverified placeholder that the fetcher must skip."""
    return uid.startswith(PENDING_PREFIX)
