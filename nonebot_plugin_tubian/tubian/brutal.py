"""残酷+（Brutal+）难度随机开局生成。

每次生成一局完整的残酷+开局配置：因子组合、合作地图与埃蒙部队编制，
可选附带两位随机指挥官。

残酷+ 在每周突变之上叠加额外因子，突变因子按强度赋 1～10 分，
各难度档位对当期因子的点数总和与因子个数做筛选：

- 残酷+1：点数总和 4～6，因子 2～3 个
- 残酷+2：点数总和 7～8，因子 2～3 个
- 残酷+3：点数总和 9～10，因子 2～3 个
- 残酷+4：点数总和 11～12，因子 2～3 个
- 残酷+5：点数总和 15～16，因子 2～4 个
- 残酷+6：点数总和 19～20，因子 2～4 个

因子选取分两层：先在本档全部合法「分数构成」（如残酷+6 的
(10,10)、(10,7,3)、(8,7,5)、(7,6,6,1) 等）中均匀抽取一种，
再在该构成下从各分值池随机抽取具体因子。按构成均匀而非按
组合均匀，可避免组合数量多的分数形态（如含 10 分因子者）
占据过高频率——残酷+6 的双 10 分开局因此从约三分之一概率
降至约百分之二，各档分数体验更为均衡。

限定因子（节日特供，不参与轮换）与未实装因子不参与随机。
"""

import random
from collections import Counter
from dataclasses import dataclass
from functools import cache
from typing import NamedTuple

from .data import normalize_query

FACTOR_POINTS: dict[str, int] = {
    # 1 分
    "震荡攻击": 1,
    "强行征用": 1,
    "闪避机动": 1,
    "生命吸取": 1,
    "轨道轰炸": 1,
    "光子过载": 1,
    "短视症": 1,
    "时空力场": 1,
    "时间扭曲": 1,
    # 2 分
    "异形寄生": 2,
    "减伤屏障": 2,
    "暗无天日": 2,
    "坚强意志": 2,
    "鼓舞人心": 2,
    "激光钻机": 2,
    "超远视距": 2,
    "晶矿护盾": 2,
    "默哀": 2,
    "净化光束": 2,
    "焦土政策": 2,
    "速度狂魔": 2,
    "龙卷风暴": 2,
    "行尸走肉": 2,
    # 3 分
    "进攻部署": 3,
    "伤害散射": 3,
    "双重压力": 3,
    "致命勾引": 3,
    "无边恐惧": 3,
    "核弹打击": 3,
    "岩浆爆发": 3,
    "飞弹大战": 3,
    "丧尸大战": 3,
    "自毁程序": 3,
    "来去无踪": 3,
    # 4 分
    "暴风雪": 4,
    "强磁雷场": 4,
    # 5 分
    "复仇战士": 5,
    "拿钱说话": 5,
    "相互摧毁": 5,
    "灵能爆表": 5,
    "小捞油水": 5,
    "虚空重生者": 5,
    # 6 分
    "杀戮机器人": 6,
    "扫雷专家": 6,
    # 7 分
    "黑死病": 7,
    "给我死吧！": 7,
    "极性不定": 7,
    "力量蜕变": 7,
    # 8 分
    "同化体": 8,
    # 10 分
    "炸弹机器人": 10,
    "风暴英雄": 10,
    "虚空裂隙": 10,
}
"""参与随机的因子及其分数（共 52 个）。"""

LIMITED_FACTORS = frozenset(
    {
        "惧怕黑暗",
        "混乱工作室",
        "焰火秀",
        "礼尚往来",
        "幸运红包",
        "杀生业报",
        "补给共享",
        "不给糖果就捣蛋",
        "捕杀火鸡",
        "迷失方向",
    }
)
"""节日限定因子，不参与残酷+随机。"""

UNRELEASED_FACTORS = frozenset(
    {
        "极度谨慎",
        "刺头兵",
        "上班偷睡",
        "消极战斗",
        "石像狂热者",
        "不死邪魔",
    }
)
"""未实装因子，不参与残酷+随机。"""


class BrutalRule(NamedTuple):
    """一档残酷+难度的筛选规律（均为闭区间）。"""

    level: int
    """难度档位（1～6）。"""

    min_total: int
    max_total: int
    """因子点数总和的允许范围。"""

    min_count: int
    max_count: int
    """因子个数的允许范围。"""

    def allows(self, total: int, count: int) -> bool:
        """判断一组因子是否满足本档规律。"""
        return (
            self.min_total <= total <= self.max_total
            and self.min_count <= count <= self.max_count
        )


BRUTAL_RULES: dict[int, BrutalRule] = {
    rule.level: rule
    for rule in (
        BrutalRule(1, 4, 6, 2, 3),
        BrutalRule(2, 7, 8, 2, 3),
        BrutalRule(3, 9, 10, 2, 3),
        BrutalRule(4, 11, 12, 2, 3),
        BrutalRule(5, 15, 16, 2, 4),
        BrutalRule(6, 19, 20, 2, 4),
    )
}
"""各档难度的筛选规律。"""

MAX_LEVEL = max(BRUTAL_RULES)
"""最高难度档位。"""

_score_groups: dict[int, list[str]] = {}
for _name, _score in FACTOR_POINTS.items():
    _score_groups.setdefault(_score, []).append(_name)
_BY_SCORE: dict[int, tuple[str, ...]] = {
    score: tuple(names) for score, names in _score_groups.items()
}
"""各分值的因子池，供按分数构成抽取具体因子。"""

_SCORE_POOL: tuple[tuple[int, int], ...] = tuple(
    (score, len(_BY_SCORE[score])) for score in sorted(_BY_SCORE, reverse=True)
)
"""(分值, 该分值因子数)，按分值降序；分数构成枚举的搜索空间。"""


class AmonArmy(NamedTuple):
    """一支埃蒙部队编制：种族与番号。"""

    race: str
    """种族名（星灵 / 人类 / 异虫）。"""

    name: str
    """部队番号。"""


COOP_MAPS: tuple[str, ...] = (
    "湮灭快车",
    "虚空撕裂",
    "虚空降临",
    "克哈裂痕",
    "往日神庙",
    "天界封锁",
    "升格之链",
    "熔火危机",
    "机会渺茫",
    "营救矿工",
    "亡者之夜",
    "黑暗杀星",
    "净网行动",
    "聚铁成兵",
    "死亡摇篮",
)
"""合作任务地图（共 15 张）。"""

COMMANDERS: tuple[str, ...] = (
    "雷诺",
    "凯瑞甘",
    "阿塔尼斯",
    "斯旺",
    "阿拉纳克",
    "沃拉尊",
    "阿巴瑟",
    "德哈卡",
    "菲尼克斯",
    "诺娃",
    "斯托科夫",
    "凯拉克斯",
    "扎加拉",
    "米拉·汉",
    "泰凯斯",
    "泽拉图",
    "斯台特曼",
    "蒙斯克",
)
"""合作任务指挥官（共 18 位；「汉和霍纳」按米拉·汉与霍纳分列）。"""

AMON_ARMIES: tuple[AmonArmy, ...] = (
    # 星灵
    AmonArmy("星灵", "大师机械"),
    AmonArmy("星灵", "步战机甲"),
    AmonArmy("星灵", "袭扰炮击"),
    AmonArmy("星灵", "卡莱的希望"),
    AmonArmy("星灵", "风暴迫临"),
    AmonArmy("星灵", "暗影袭扰"),
    AmonArmy("星灵", "艾尔先锋"),
    AmonArmy("星灵", "族长之军"),
    # 人类
    AmonArmy("人类", "旧世步兵团"),
    AmonArmy("人类", "突击团"),
    AmonArmy("人类", "旧世机械团"),
    AmonArmy("人类", "战争机械团"),
    AmonArmy("人类", "暗影科技团"),
    AmonArmy("人类", "帝国战斗群"),
    # 异虫
    AmonArmy("异虫", "肆虐扩散"),
    AmonArmy("异虫", "滋生腐化"),
    AmonArmy("异虫", "爆炸威胁"),
    AmonArmy("异虫", "侵略虫群"),
    AmonArmy("异虫", "遮天蔽日"),
)
"""埃蒙部队编制（共 19 支：星灵 8、人类 6、异虫 5）。"""


@dataclass(frozen=True)
class BrutalCombo:
    """一次随机残酷+的开局配置：难度、地图、埃蒙部队、因子组合与指挥官。"""

    level: int
    """难度档位（1～6）。"""

    map_name: str
    """合作地图名。"""

    army: AmonArmy
    """本局埃蒙部队编制。"""

    factors: tuple[str, ...]
    """因子名称，按分数从高到低排列。"""

    commanders: tuple[str, ...] = ()
    """随机到的指挥官（两位）；空元组表示未开启指挥官随机。"""

    @property
    def rule(self) -> BrutalRule:
        """本档的筛选规律。"""
        return BRUTAL_RULES[self.level]

    @property
    def points(self) -> tuple[int, ...]:
        """各因子分数，与 factors 一一对应。"""
        return tuple(FACTOR_POINTS[name] for name in self.factors)

    @property
    def total(self) -> int:
        """因子点数总和。"""
        return sum(self.points)


@cache
def _score_profiles(level: int) -> tuple[tuple[int, ...], ...]:
    """枚举该难度下全部合法分数构成。

    一个分数构成是分值降序的多元组（如 (10, 7, 3)），满足：
    长度在本档因子个数区间内、总和落在本档点数区间内、
    每个分值的出现次数不超过该分值的因子数（保证可实例化）。

    DFS 按分值降序逐档决定用量；当前和加上剩余位置全取
    本档分值（后续档只会更小）仍达不到下限时提前终止。
    """
    rule = BRUTAL_RULES[level]
    profiles: list[tuple[int, ...]] = []
    partial: list[int] = []
    size = len(_SCORE_POOL)

    def search(idx: int, count: int, total: int) -> None:
        # 终态（档位决策完毕或因子位已满）判定一次，避免同一构成
        # 沿跳档分支在每个后续档位入口被重复记录
        if idx >= size or count >= rule.max_count:
            if (
                rule.min_count <= count <= rule.max_count
                and rule.min_total <= total <= rule.max_total
            ):
                profiles.append(tuple(partial))
            return
        score, avail = _SCORE_POOL[idx]
        # 剩余位置全取本档分值（后续档只会更小）也够不到下限，剪枝
        if total + score * (rule.max_count - count) < rule.min_total:
            return
        for use in range(1, avail + 1):
            if count + use > rule.max_count or total + score * use > rule.max_total:
                break
            partial.extend((score,) * use)
            search(idx + 1, count + use, total + score * use)
            del partial[-use:]
        search(idx + 1, count, total)  # 跳过本档

    search(0, 0, 0)
    return tuple(profiles)


def random_brutal(
    level: int,
    rng: random.Random | None = None,
    *,
    with_commanders: bool = False,
) -> BrutalCombo:
    """按难度规律随机生成一局残酷+开局。

    先在本档全部合法分数构成中均匀抽取一种（每种分数体验
    等概率，避免组合数量多的形态——尤其是含 10 分因子者——
    占据过高频率），再在该构成下从各分值池均匀抽取具体因子；
    地图与埃蒙部队在各自池中均匀抽取。with_commanders 为
    True 时再从指挥官池均匀抽取两位不重复的指挥官。
    """
    if level not in BRUTAL_RULES:
        raise ValueError(f"未知的残酷+难度：{level}")
    if rng is None:
        rng = random
    profiles = _score_profiles(level)
    if not profiles:
        raise ValueError(f"残酷+{level}无合法组合")
    profile = rng.choice(profiles)
    needs: Counter[int] = Counter(profile)
    factors: list[str] = []
    for score, need in needs.items():
        factors.extend(rng.sample(_BY_SCORE[score], need))
    ordered = tuple(sorted(factors, key=lambda name: -FACTOR_POINTS[name]))
    commanders = tuple(rng.sample(COMMANDERS, 2)) if with_commanders else ()
    return BrutalCombo(
        level=level,
        map_name=rng.choice(COOP_MAPS),
        army=rng.choice(AMON_ARMIES),
        factors=ordered,
        commanders=commanders,
    )


def parse_brutal_level(text: str) -> int | None:
    """把「随机残酷」的参数解析为 1～6 的难度档位。

    支持「3」「+3」「3级」「残酷+3」等写法；「残酷+」即残酷+1。
    空文本、裸「残酷」（基础残酷难度）、无法识别或超出范围时返回 None。
    """
    key = normalize_query(text)
    for prefix in ("随机残酷", "残酷随机", "残酷"):
        if key.startswith(prefix):
            key = key.removeprefix(prefix)
            if key == "+":
                return 1
            break
    key = key.lstrip("+").rstrip("级").strip()
    if not key.isdigit():
        return None
    level = int(key)
    return level if level in BRUTAL_RULES else None


COMMANDER_KEYWORDS = ("随机指挥官", "指挥官")
"""「随机残酷」附带指挥官随机的关键词，长词在前以免被短词截断。"""


class BrutalArgs(NamedTuple):
    """「随机残酷」的参数解析结果。"""

    level: int | None
    """难度档位（1～6）；None 表示未指定，调用方随机抽取。"""

    with_commanders: bool
    """是否额外随机两位指挥官。"""


def parse_brutal_args(text: str) -> BrutalArgs | None:
    """把「随机残酷」的参数解析为难度档位与指挥官开关。

    支持「3」「3指挥官」「指挥官」「随机残酷 3 指挥官」等写法；
    空文本即随机难度。除去指挥官关键词后剩余文本无法识别时返回 None。
    """
    key = normalize_query(text)
    with_commanders = False
    for keyword in COMMANDER_KEYWORDS:
        if keyword in key:
            key = key.replace(keyword, "")
            with_commanders = True
    key = key.strip()
    if not key:
        return BrutalArgs(None, with_commanders)
    level = parse_brutal_level(key)
    return None if level is None else BrutalArgs(level, with_commanders)
