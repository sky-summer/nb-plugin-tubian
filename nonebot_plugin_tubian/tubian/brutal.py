"""残酷+（Brutal+）难度随机开局生成。

每次生成一局完整的残酷+开局配置：因子组合、合作地图与埃蒙部队编制。

残酷+ 在每周突变之上叠加额外因子，突变因子按强度赋 1～10 分，
各难度档位对当期因子的点数总和与因子个数做筛选：

- 残酷+1：点数总和 4～6，因子 2～3 个
- 残酷+2：点数总和 7～8，因子 2～3 个
- 残酷+3：点数总和 9～10，因子 2～3 个
- 残酷+4：点数总和 11～12，因子 2～3 个
- 残酷+5：点数总和 15～16，因子 2～4 个
- 残酷+6：点数总和 19～20，因子 2～4 个

限定因子（节日特供，不参与轮换）与未实装因子不参与随机。
"""

import random
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

_POOL = tuple(sorted(FACTOR_POINTS, key=FACTOR_POINTS.__getitem__))
"""按分数升序排列的因子池，供组合枚举与剪枝使用。"""


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
    """一次随机残酷+的开局配置：难度、地图、埃蒙部队与因子组合。"""

    level: int
    """难度档位（1～6）。"""

    map_name: str
    """合作地图名。"""

    army: AmonArmy
    """本局埃蒙部队编制。"""

    factors: tuple[str, ...]
    """因子名称，按分数从高到低排列。"""

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
def _combo_groups(level: int) -> tuple[tuple[tuple[str, ...], ...], ...]:
    """枚举该难度下全部合法因子组合，按因子个数分组。

    因子池按分数升序，DFS 剪枝：当前分数加上剩余最小可能和超过
    区间上限时提前终止；加上剩余最大可能和仍达不到下限时跳过。
    """
    rule = BRUTAL_RULES[level]
    points = [FACTOR_POINTS[name] for name in _POOL]
    size = len(points)
    found: list[tuple[str, ...]] = []
    partial: list[str] = []

    def search(remain: int, start: int, lo: int, hi: int) -> None:
        if remain == 0:
            if lo <= 0 <= hi:
                found.append(tuple(partial))
            return
        for i in range(start, size - remain + 1):
            point = points[i]
            # 升序池：分数只增不减，最小可能和一旦超过上限即可终止
            if point + sum(points[i + 1 : i + remain]) > hi:
                break
            # 剩余最大可能和（池尾 remain-1 个）仍不足下限，当前分数太低
            if point + sum(points[size - remain + 1 :]) < lo:
                continue
            partial.append(_POOL[i])
            search(remain - 1, i + 1, lo - point, hi - point)
            partial.pop()

    groups = []
    for count in range(rule.min_count, rule.max_count + 1):
        found.clear()
        search(count, 0, rule.min_total, rule.max_total)
        groups.append(tuple(found))
    return tuple(groups)


def random_brutal(level: int, rng: random.Random | None = None) -> BrutalCombo:
    """按难度规律随机生成一局残酷+开局。

    因子个数先在允许范围内均匀取值，再在该个数的全部合法组合中
    均匀抽取，避免个数多的组合数量占优时几乎抽不出个数少的组合
    （如残酷+6 的双 10 分因子）；地图与埃蒙部队在各自池中均匀抽取。
    """
    if level not in BRUTAL_RULES:
        raise ValueError(f"未知的残酷+难度：{level}")
    if rng is None:
        rng = random
    groups = [group for group in _combo_groups(level) if group]
    if not groups:
        raise ValueError(f"残酷+{level}无合法组合")
    factors = rng.choice(rng.choice(groups))
    ordered = tuple(sorted(factors, key=lambda name: -FACTOR_POINTS[name]))
    return BrutalCombo(
        level=level,
        map_name=rng.choice(COOP_MAPS),
        army=rng.choice(AMON_ARMIES),
        factors=ordered,
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
