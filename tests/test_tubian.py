"""nonebot-plugin-tubian 测试。

在项目根目录下运行：

    python -m unittest tests.test_tubian -v
"""

import asyncio
import base64
import random
import sys
import tempfile
import unittest
from collections import Counter
from datetime import date, datetime, timedelta
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import nonebot
from nonebot.adapters.onebot.v11 import Message as OB11Message
from nonebot.adapters.onebot.v11 import MessageSegment as OB11MessageSegment
from nonebot.rule import TrieRule
from PIL import Image

if TYPE_CHECKING:
    from nonebot.adapters import Bot

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PLUGIN_NAME = "nonebot_plugin_tubian.tubian"
DATA_DIR = ROOT / "nonebot_plugin_tubian" / "tubian" / "突变列表数据"


@lru_cache(maxsize=1)
def _load() -> tuple[Any, Any]:
    """初始化 NoneBot 并加载插件，返回 (插件模块, data 模块)。"""
    nonebot.init(log_level="ERROR")
    plugin = nonebot.load_plugin(PLUGIN_NAME)
    if plugin is None:
        plugin = nonebot.get_plugin("tubian")
    if plugin is None:
        message = "插件 tubian 加载失败"
        raise RuntimeError(message)
    return plugin.module, plugin.module.data


class _FakeAdapter:
    """伪造适配器，仅提供 get_name。"""

    def __init__(self, name: str) -> None:
        self._name = name

    def get_name(self) -> str:
        return self._name


class _FakeBot:
    """伪造 Bot，仅支持 adapter.get_name 与 self_id。"""

    def __init__(self, name: str, self_id: str = "fake-bot") -> None:
        self.adapter = _FakeAdapter(name)
        self.self_id = self_id


class TestMutationBook(unittest.TestCase):
    """突变数据加载与按时间选中。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.plugin_mod, cls.data_mod = _load()
        cls.data_dir = cls.plugin_mod.config.tubian_data_dir
        cls.book = cls.data_mod.MutationBook.load(cls.data_dir)

    def test_data_dir_is_module_relative(self) -> None:
        self.assertEqual(Path(self.data_dir).resolve(), DATA_DIR.resolve())
        self.assertTrue((Path(self.data_dir) / "突变列表数据.xlsx").is_file())
        self.assertTrue((Path(self.data_dir) / "因子图标").is_dir())
        self.assertTrue((Path(self.data_dir) / "地图图标").is_dir())
        self.assertTrue((Path(self.data_dir) / "指挥官图标").is_dir())

    def test_load_count(self) -> None:
        self.assertEqual(self.book.count, 184)

    def test_ids_cover_cycle(self) -> None:
        for mid in (1, 127, 128, 129, 184):
            self.assertIsNotNone(self.book.get(mid), f"缺少序号 {mid}")
        self.assertIsNone(self.book.get(0))
        self.assertIsNone(self.book.get(185))

    def test_select_by_current_time(self) -> None:
        """2026-10-15 应选中 #128 恐惧神庙，与「本周突变」表一致。"""
        selected = self.book.at(date(2026, 10, 15))
        self.assertEqual(selected.mid, 128)
        self.assertEqual(selected.name, "恐惧神庙")
        self.assertEqual(selected.map_name, "往日神庙")
        self.assertEqual(selected.start_date, date(2026, 10, 12))
        self.assertEqual(selected.factors, ("异形寄生", "惧怕黑暗", "无边恐惧"))
        self.assertIsNone(selected.number)
        self.assertEqual(selected.remaining_days(date(2026, 10, 15)), 4)

    def test_week_boundaries(self) -> None:
        self.assertEqual(self.book.at(date(2026, 10, 12)).mid, 128)  # 开放首日
        self.assertEqual(self.book.at(date(2026, 10, 18)).mid, 128)  # 期满前一天
        self.assertEqual(self.book.at(date(2026, 10, 19)).mid, 129)  # 下一期首日
        self.assertEqual(self.book.at(date(2026, 10, 19)).name, "光炫神迷")

    def test_wrap_around(self) -> None:
        self.assertEqual(self.book.at(date(2026, 10, 11)).mid, 127)  # 锚点前一周
        self.assertEqual(self.book.at(date(2030, 4, 22)).mid, 128)  # 末期后一周

    def test_full_cycle_coverage(self) -> None:
        """整轮 184 期、每期 7 天，每天都应选中对应期次。"""
        for mid in range(1, 185):
            mutation = self.book.get(mid)
            assert mutation is not None
            for offset in range(7):
                day = mutation.start_date + timedelta(days=offset)
                self.assertEqual(self.book.at(day).mid, mid, f"{day} 应选中 #{mid}")

    def test_search_by_name_and_map(self) -> None:
        self.assertEqual(len(self.book.search("灾难之轮")), 6)
        self.assertTrue(self.book.search("往日神庙"))
        self.assertEqual(self.book.search("不存在的突变"), [])

    def test_number_field(self) -> None:
        self.assertEqual(self.book.get(3).number, "7")
        self.assertIsNone(self.book.get(128).number)  # 原表为「/」

    def test_recommendation_fields(self) -> None:
        """推荐指挥官（含威望）、注意事项、保底位与推荐原因均被解析。"""
        mutation = self.book.get(128)
        assert mutation is not None
        self.assertEqual(
            [p.name for p in mutation.commanders], ["斯台特曼", "泽拉图", "凯拉克斯"]
        )
        self.assertEqual(
            [p.prestige for p in mutation.commanders],
            ["P2 最佳伙伴", "P2 知识探求者", "P1 战争建筑师"],
        )
        self.assertTrue(mutation.notes.startswith("①长图守三路"))
        assert mutation.bonus is not None
        self.assertEqual(mutation.bonus.name, "阿塔尼斯")
        self.assertIn("我大主教还需要用哪个威望", mutation.bonus.prestige)
        self.assertTrue(mutation.reason.startswith("往日神庙守三路"))

    def test_commander_pick_display(self) -> None:
        """带威望时展示为「名称（威望）」，无威望时仅名称。"""
        CommanderPick = self.data_mod.CommanderPick
        self.assertEqual(CommanderPick("凯瑞甘", "P0").display, "凯瑞甘（P0）")
        self.assertEqual(CommanderPick("雷诺").display, "雷诺")

    def test_all_mutations_have_recommendations(self) -> None:
        """每期都有 3 位推荐指挥官、保底位、注意事项与推荐原因。"""
        for m in self.book._by_id.values():
            self.assertEqual(len(m.commanders), 3, f"#{m.mid} 推荐指挥官不足 3 位")
            self.assertIsNotNone(m.bonus, f"#{m.mid} 缺少保底位")
            self.assertTrue(m.notes, f"#{m.mid} 缺少注意事项")
            self.assertTrue(m.reason, f"#{m.mid} 缺少推荐原因")

    def test_icon_path(self) -> None:
        icon = self.book.icon_path("异形寄生")
        assert icon is not None
        self.assertTrue(icon.is_file())
        self.assertIsNone(self.book.icon_path("不存在的因子"))

    def test_map_icon_path(self) -> None:
        icon = self.book.map_icon_path("往日神庙")
        assert icon is not None
        self.assertTrue(icon.is_file())
        self.assertIsNone(self.book.map_icon_path("不存在的地图"))

    def test_all_maps_have_icons(self) -> None:
        """全部期次的地图图片均可命中（数据表地图名与图标库一致）。"""
        for m in self.book._by_id.values():
            self.assertIsNotNone(
                self.book.map_icon_path(m.map_name),
                f"#{m.mid} 缺少地图图片：{m.map_name}",
            )

    def test_all_recommended_commanders_have_icons(self) -> None:
        """数据表中的推荐指挥官（含保底位）都能取到头像。"""
        names = {p.name for m in self.book._by_id.values() for p in m.commanders}
        for m in self.book._by_id.values():
            if m.bonus is not None:
                names.add(m.bonus.name)
        for name in names:
            self.assertIsNotNone(
                self.book.commander_icon_path(name), f"缺少指挥官头像：{name}"
            )

    def test_commander_icon_aliases(self) -> None:
        """译名别名均可命中头像库文件。"""
        for name in (
            "斯图科夫",
            "斯托科夫",
            "霍纳与汉",
            "霍纳",
            "卡拉克斯",
            "凯拉克斯",
            "米拉·汉",
        ):
            self.assertIsNotNone(
                self.book.commander_icon_path(name), f"缺少指挥官头像：{name}"
            )
        self.assertIsNone(self.book.commander_icon_path("不存在的指挥官"))

    def test_icons_deduped_by_factor_name(self) -> None:
        """图标目录只保留 因子_<名称>.png，全部因子可按名称通用获取。"""
        icon_dir = Path(self.data_dir) / "因子图标"
        files = sorted(icon_dir.glob("*.png"))
        self.assertEqual(len(files), 62)
        for path in files:
            self.assertTrue(path.name.startswith("因子_"), path.name)
        factors = {f for m in self.book._by_id.values() for f in m.factors}
        self.assertEqual(len(factors), 62)
        for factor in factors:
            icon = self.book.icon_path(factor)
            self.assertIsNotNone(icon, f"缺少因子图标：{factor}")

    def test_missing_data_dir(self) -> None:
        with self.assertRaises(self.data_mod.MutationDataError):
            self.data_mod.MutationBook.load(Path("Z:/不存在的目录"))

    def test_plugin_book_cache(self) -> None:
        self.assertIs(self.plugin_mod._book(), self.plugin_mod._book())
        self.assertEqual(self.plugin_mod._book().count, 184)


class TestMessageCard(unittest.TestCase):
    """消息卡片构造（文字 + 因子图标）。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.plugin_mod, cls.data_mod = _load()
        cls.book = cls.plugin_mod._book()

    def _card(
        self, adapter_name: str, mutation: Any, label: str, note: str | None = None
    ) -> Any:
        bot = cast("Bot", _FakeBot(adapter_name))
        return self.plugin_mod._build_card(bot, mutation, label, note=note)

    def test_card_text(self) -> None:
        mutation = self.book.at(date(2026, 10, 15))
        text = self._card("OneBot V11", mutation, "本周突变").extract_plain_text()
        self.assertIn("【本周突变】恐惧神庙", text)
        self.assertIn("地图：往日神庙 · 序号 128", text)
        self.assertIn("日期：2026/10/12（周一） ～ 2026/10/19", text)
        self.assertIn("异形寄生", text)
        self.assertIn("惧怕黑暗", text)
        self.assertIn("无边恐惧", text)

    def test_card_number_field(self) -> None:
        mutation = self.book.get(3)
        text = self._card("OneBot V11", mutation, "突变 #3").extract_plain_text()
        self.assertIn("因子分数：7", text)

    def test_card_remaining_time(self) -> None:
        today = self.data_mod.local_today()
        mutation = self.book.at(today)
        text = self._card("OneBot V11", mutation, "本周突变").extract_plain_text()
        self.assertIn(f"剩余时间：{mutation.remaining_days(today)} 天", text)

    def test_card_omits_remaining_for_other_weeks(self) -> None:
        today = self.data_mod.local_today()
        mutation = self.book.at(today + timedelta(days=7))
        text = self._card("OneBot V11", mutation, "下周突变").extract_plain_text()
        self.assertNotIn("剩余时间", text)

    def test_card_images_onebot(self) -> None:
        """图片必须以 base64 内嵌发送，传路径会在 NapCat 跨容器场景 ENOENT。"""
        mutation = self.book.at(date(2026, 10, 15))
        card = self._card("OneBot V11", mutation, "本周突变")
        images = [seg for seg in card if seg.type == "image"]
        bonus = () if mutation.bonus is None else (mutation.bonus,)
        picks = (*mutation.commanders, *bonus)
        # 地图 + 推荐指挥官（含保底位）+ 因子
        self.assertEqual(len(images), 1 + len(picks) + len(mutation.factors))
        for seg in images:
            file = str(seg.data["file"])
            self.assertTrue(file.startswith("base64://"), file[:32])
            self.assertTrue(
                base64.b64decode(file.removeprefix("base64://")).startswith(b"\x89PNG")
            )

    def test_card_images_qq(self) -> None:
        """QQ 每条消息只接受一个媒体：因子图标拼为一张长图，与文字卡同条发送。"""
        mutation = self.book.at(date(2026, 10, 15))
        card = self._card("QQ", mutation, "本周突变")
        images = [seg for seg in card if seg.type == "file_image"]
        self.assertEqual(len(images), 1)
        seg = images[0]
        data = bytes(seg.data["content"])
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertTrue(str(seg.data["file_name"]).endswith(".png"))
        text = card.extract_plain_text()
        self.assertIn("【本周突变】恐惧神庙", text)
        for factor in ("异形寄生", "惧怕黑暗", "无边恐惧"):
            self.assertIn(factor, text)

    def test_merged_image_dimensions(self) -> None:
        """拼接图：地图置顶，因子图标在其下横排一行。"""
        mutation = self.book.at(date(2026, 10, 15))
        data = self.plugin_mod._merged_card_image(mutation.map_name, mutation.factors)
        assert data is not None
        map_path = self.book.map_icon_path(mutation.map_name)
        assert map_path is not None
        with Image.open(map_path) as map_img:
            map_width, map_height = map_img.width, map_img.height
        widths = []
        heights = []
        for factor in mutation.factors:
            path = self.book.icon_path(factor)
            assert path is not None
            with Image.open(path) as icon:
                widths.append(icon.width)
                heights.append(icon.height)
        gap = self.plugin_mod._ICON_GAP
        factors_width = sum(widths) + gap * (len(widths) - 1)
        with Image.open(BytesIO(data)) as merged:
            self.assertEqual(merged.width, max(map_width, factors_width))
            self.assertEqual(merged.height, map_height + gap + max(heights))

    def test_merged_image_commander_row(self) -> None:
        """指挥官头像行夹在地图与因子行之间。"""
        mutation = self.book.at(date(2026, 10, 15))
        commanders = ("雷诺", "凯瑞甘")
        data = self.plugin_mod._merged_card_image(
            mutation.map_name, mutation.factors, commanders
        )
        assert data is not None
        map_path = self.book.map_icon_path(mutation.map_name)
        assert map_path is not None
        with Image.open(map_path) as map_img:
            map_width, map_height = map_img.width, map_img.height
        gap = self.plugin_mod._ICON_GAP

        def row_size(names: tuple[str, ...], lookup: Any) -> tuple[int, int]:
            widths = []
            heights = []
            for name in names:
                path = lookup(name)
                assert path is not None
                with Image.open(path) as icon:
                    widths.append(icon.width)
                    heights.append(icon.height)
            return sum(widths) + gap * (len(widths) - 1), max(heights)

        cmdr_width, cmdr_height = row_size(commanders, self.book.commander_icon_path)
        factor_width, factor_height = row_size(mutation.factors, self.book.icon_path)
        with Image.open(BytesIO(data)) as merged:
            self.assertEqual(
                merged.width, max(map_width, cmdr_width, factor_width)
            )
            self.assertEqual(
                merged.height,
                map_height + gap + cmdr_height + gap + factor_height,
            )

    def test_card_note(self) -> None:
        """重名优选时的提示附在文字卡尾部。"""
        mutation = self.book.at(date(2026, 10, 15))
        card = self._card("OneBot V11", mutation, "本周突变", note="同名突变共 6 期")
        self.assertIn("同名突变共 6 期", card.extract_plain_text())

    def test_card_recommendation_text(self) -> None:
        """详情卡片包含推荐指挥官（含威望）、注意事项、保底位与推荐原因。"""
        mutation = self.book.at(date(2026, 10, 15))
        text = self._card("OneBot V11", mutation, "本周突变").extract_plain_text()
        self.assertIn("推荐指挥官：", text)
        self.assertIn("斯台特曼（P2 最佳伙伴）", text)
        self.assertIn("泽拉图（P2 知识探求者）", text)
        self.assertIn("凯拉克斯（P1 战争建筑师）", text)
        self.assertIn("保底位：阿塔尼斯", text)
        self.assertIn("注意事项：", text)
        self.assertIn(mutation.notes, text)
        self.assertIn(f"推荐原因：{mutation.reason}", text)

    def test_card_recommendation_qq(self) -> None:
        """QQ 侧详情文字齐全，推荐指挥官头像拼入唯一媒体图。"""
        mutation = self.book.at(date(2026, 10, 15))
        card = self._card("QQ", mutation, "本周突变")
        text = card.extract_plain_text()
        self.assertIn("推荐指挥官：", text)
        self.assertIn("斯台特曼（P2 最佳伙伴）", text)
        self.assertIn("保底位：阿塔尼斯", text)
        self.assertIn("注意事项：", text)
        images = [seg for seg in card if seg.type == "file_image"]
        self.assertEqual(len(images), 1)
        data = bytes(images[0].data["content"])
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertEqual(str(images[0].data["file_name"]), f"突变{mutation.mid}_详情.png")

    def test_card_fallback_text_only(self) -> None:
        mutation = self.book.at(date(2026, 10, 15))
        card = self._card("Other", mutation, "本周突变")
        self.assertTrue(all(seg.type == "text" for seg in card))

    def test_card_fallback_without_icons(self) -> None:
        """图标全部缺失时，QQ 卡片退化为纯文字，因子名称仍保留。"""
        mutation = self.data_mod.Mutation(
            mid=1,
            name="测试突变",
            map_name="测试地图",
            start_date=date(2026, 10, 12),
            factors=("不存在的因子",),
        )
        self.assertIsNone(self.plugin_mod._merged_card_image(mutation.map_name, mutation.factors))
        card = self._card("QQ", mutation, "本周突变")
        self.assertTrue(all(seg.type == "text" for seg in card))
        self.assertIn("不存在的因子", card.extract_plain_text())

    def test_search_summary(self) -> None:
        found = self.book.search("灾难之轮")
        summary = self.plugin_mod._search_summary(found, "灾难之轮")
        self.assertIn("找到 6 个", summary)
        self.assertIn("#11", summary)
        self.assertIn("突变 <序号>", summary)


class TestQueryRobustness(unittest.TestCase):
    """名称查询健壮性：标点宽度、重名优选、@ 前缀、指令前缀兜底。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.plugin_mod, cls.data_mod = _load()
        cls.book = cls.plugin_mod._book()

    def test_search_normalizes_punctuation(self) -> None:
        """全/半角标点混用的名称按任一宽度均可命中。"""
        self.assertEqual(self.book.search("高举盾牌！")[0].name, "高举盾牌!")
        self.assertEqual(self.book.search("高举盾牌!")[0].name, "高举盾牌!")
        self.assertEqual(self.book.search("我的机器人!")[0].name, "我的机器人！")
        self.assertEqual(self.book.search("我的机器人！")[0].name, "我的机器人！")

    def test_exact_matches_and_prefer(self) -> None:
        """重名时按名称精确命中，挑出距今最近的一期（2026-10-15 → #11）。"""
        found = self.book.search("灾难之轮")
        self.assertEqual(len(found), 6)
        exact = self.plugin_mod._exact_matches(found, "灾难之轮")
        self.assertEqual(len(exact), 6)
        pick = self.plugin_mod._prefer_mutation(exact, date(2026, 10, 15))
        self.assertEqual(pick.mid, 11)
        # 本期命中的优先于「距今最近」
        current = self.book.at(date(2026, 10, 15))
        pick2 = self.plugin_mod._prefer_mutation([current, pick], date(2026, 10, 15))
        self.assertEqual(pick2.mid, current.mid)

    def test_exact_matches_rejects_partial(self) -> None:
        """模糊命中（如按地图名）不做重名优选。"""
        found = self.book.search("神庙")
        self.assertTrue(found)
        self.assertEqual(self.plugin_mod._exact_matches(found, "神庙"), [])

    def test_strip_command_prefix(self) -> None:
        """指令前的 at/reply 段被剥离；非本插件消息保持原样。"""
        at = OB11MessageSegment.at(123)
        msg = OB11Message(at) + OB11Message("突变 恐惧神庙")
        self.plugin_mod._strip_command_prefix(msg)
        self.assertEqual(msg[0].type, "text")
        self.assertEqual(msg.extract_plain_text(), "突变 恐惧神庙")

        reply = OB11MessageSegment.reply(1)
        msg2 = OB11Message(reply) + OB11Message(at) + OB11Message("本周突变")
        self.plugin_mod._strip_command_prefix(msg2)
        self.assertEqual(msg2.extract_plain_text(), "本周突变")

        msg3 = OB11Message(at) + OB11Message("突变查询 恐惧神庙")
        self.plugin_mod._strip_command_prefix(msg3)
        self.assertEqual(msg3.extract_plain_text(), "突变查询 恐惧神庙")

        other = OB11Message(at) + OB11Message("今天吃什么")
        self.plugin_mod._strip_command_prefix(other)
        self.assertEqual(other[0].type, "at")

    def test_strip_command_prefix_qq_mention(self) -> None:
        """QQ 官方群 @ 机器人时开头的 mention_user 段同样被剥离。"""
        from nonebot.adapters.qq import Message as QQMessage
        from nonebot.adapters.qq import MessageSegment as QQMessageSegment

        mention = QQMessageSegment.mention_user("102308151")
        msg = QQMessage(mention) + QQMessage(" 随机残酷 3")
        self.plugin_mod._strip_command_prefix(msg)
        self.assertEqual(msg[0].type, "text")
        self.assertTrue(msg.extract_plain_text().lstrip().startswith("随机残酷"))

        other = QQMessage(mention) + QQMessage(" 今天吃什么")
        self.plugin_mod._strip_command_prefix(other)
        self.assertEqual(other[0].type, "mention_user")

    def test_command_prefix_fallback(self) -> None:
        """带/不带「/」的指令前缀都已注册，不依赖部署环境 COMMAND_START。"""
        for word in (
            "突变",
            "突变查询",
            "本周突变",
            "tb",
            "下周突变",
            "tbx",
            "随机残酷",
            "残酷随机",
            "残酷+",
            "sjck",
            "订阅突变",
            "退订突变",
            "测试推送",
        ):
            self.assertIn(word, TrieRule.prefix, word)
            self.assertIn(f"/{word}", TrieRule.prefix, f"/{word}")

    def test_command_trie_longest_prefix(self) -> None:
        """「突变查询」不被「突变」抢占，按最长前缀解析为独立命令。"""
        pf = TrieRule.prefix.longest_prefix("突变查询 恐惧神庙")
        self.assertEqual(pf.key, "突变查询")
        self.assertEqual(pf.value.command, ("突变查询",))

    def test_resolve_query_rejects_name(self) -> None:
        """「突变 <名称>」不再触发名称查询，回复改用「突变查询」的提示。"""
        query = self.plugin_mod._resolve_query(
            self.book, "恐惧神庙", date(2026, 10, 15)
        )
        self.assertIsNone(query.mutation)
        assert query.reply is not None
        self.assertIn("突变查询", query.reply)

    def test_resolve_search_query(self) -> None:
        """「突变查询 <名称>」按名称查询；序号同样可用。"""
        query = self.plugin_mod._resolve_search_query(
            self.book, "恐惧神庙", date(2026, 10, 15)
        )
        assert query.mutation is not None
        self.assertEqual(query.mutation.mid, 128)
        by_number = self.plugin_mod._resolve_search_query(
            self.book, "128", date(2026, 10, 15)
        )
        assert by_number.mutation is not None
        self.assertEqual(by_number.mutation.mid, 128)

    def test_resolve_search_query_empty(self) -> None:
        """「突变查询」缺参数时回复用法提示。"""
        query = self.plugin_mod._resolve_search_query(self.book, "", date(2026, 10, 15))
        self.assertIsNone(query.mutation)
        assert query.reply is not None
        self.assertIn("突变查询", query.reply)


class TestBrutalRandom(unittest.TestCase):
    """随机残酷+因子组合生成。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.plugin_mod, cls.data_mod = _load()
        cls.brutal_mod = cls.plugin_mod.brutal
        cls.book = cls.plugin_mod._book()

    def test_factor_points_counts(self) -> None:
        """各分数的因子数量与整理表一致。"""
        points = self.brutal_mod.FACTOR_POINTS
        self.assertEqual(len(points), 52)
        by_score: dict[int, int] = {}
        for score in points.values():
            by_score[score] = by_score.get(score, 0) + 1
        self.assertEqual(
            by_score,
            {1: 9, 2: 14, 3: 11, 4: 2, 5: 6, 6: 2, 7: 4, 8: 1, 10: 3},
        )

    def test_factor_pool_matches_icons(self) -> None:
        """计分因子都有图标；52 个计分因子 + 10 个限定因子 = Excel 全部因子。"""
        for factor in self.brutal_mod.FACTOR_POINTS:
            self.assertIsNotNone(self.book.icon_path(factor), f"缺少图标：{factor}")
        limited = self.brutal_mod.LIMITED_FACTORS
        self.assertEqual(len(limited), 10)
        self.assertFalse(limited & set(self.brutal_mod.FACTOR_POINTS))
        excel_factors = {f for m in self.book._by_id.values() for f in m.factors}
        self.assertEqual(excel_factors, set(self.brutal_mod.FACTOR_POINTS) | limited)

    def test_rules(self) -> None:
        """六档规律与整理表一致。"""
        rules = self.brutal_mod.BRUTAL_RULES
        self.assertEqual(
            [
                (r.min_total, r.max_total, r.min_count, r.max_count)
                for r in rules.values()
            ],
            [
                (4, 6, 2, 3),
                (7, 8, 2, 3),
                (9, 10, 2, 3),
                (11, 12, 2, 3),
                (15, 16, 2, 4),
                (19, 20, 2, 4),
            ],
        )

    def test_score_profiles_valid(self) -> None:
        """分数构成枚举：无重复、满足档位规律、分值降序、可实例化。"""
        for level, rule in self.brutal_mod.BRUTAL_RULES.items():
            profiles = self.brutal_mod._score_profiles(level)
            self.assertTrue(profiles, f"残酷+{level} 无合法构成")
            seen: set[tuple[int, ...]] = set()
            for profile in profiles:
                self.assertNotIn(profile, seen, f"残酷+{level} 构成重复：{profile}")
                seen.add(profile)
                self.assertEqual(
                    list(profile), sorted(profile, reverse=True), (level, profile)
                )
                self.assertTrue(
                    rule.allows(sum(profile), len(profile)),
                    f"残酷+{level} 构成 {profile} 不满足规律",
                )
                for score, need in Counter(profile).items():
                    self.assertLessEqual(
                        need,
                        len(self.brutal_mod._BY_SCORE[score]),
                        f"残酷+{level} 构成 {profile} 超出 {score} 分因子数",
                    )

    def test_score_profiles_counts_covered(self) -> None:
        """每一档、每个允许的因子个数都有合法分数构成。"""
        for level, rule in self.brutal_mod.BRUTAL_RULES.items():
            lengths = {len(p) for p in self.brutal_mod._score_profiles(level)}
            self.assertEqual(
                lengths,
                set(range(rule.min_count, rule.max_count + 1)),
                f"残酷+{level} 缺少某个因子个数的构成",
            )

    def test_score_profiles_expectation(self) -> None:
        """手工核对残酷+6 构成数与含 10 分构成数。"""
        profiles = self.brutal_mod._score_profiles(6)
        self.assertEqual(len(profiles), 70)
        self.assertEqual(sum(1 for p in profiles if 10 in p), 24)
        self.assertEqual(sum(1 for p in profiles if p.count(10) >= 2), 1)  # (10,10)

    def test_random_brutal_10_factor_frequency(self) -> None:
        """按分数构成均匀抽取：残酷+6 双 10 罕见、10 分因子期望受控。

        旧实现（按组合均匀）双 10 约 33%、每局 10 分期望约 1.14。
        """
        rng = random.Random(20261020)
        trials = 3000
        double10 = 0
        any10 = 0
        total10 = 0
        for _ in range(trials):
            combo = self.brutal_mod.random_brutal(6, rng)
            n10 = sum(
                1 for f in combo.factors if self.brutal_mod.FACTOR_POINTS[f] == 10
            )
            double10 += n10 >= 2
            any10 += n10 >= 1
            total10 += n10
        self.assertLess(double10, trials * 0.05)
        self.assertLess(any10, trials * 0.60)
        self.assertLess(total10, trials * 0.8)

    def test_random_brutal_low_level_no_10_factor(self) -> None:
        """低档（残酷+1～3）总分不超过 10，任何开局都不含 10 分因子。"""
        rng = random.Random(20261019)
        for _ in range(300):
            combo = self.brutal_mod.random_brutal(rng.randint(1, 3), rng)
            for factor in combo.factors:
                self.assertLess(self.brutal_mod.FACTOR_POINTS[factor], 10)

    def test_random_brutal_profile_coverage(self) -> None:
        """多次抽取应覆盖该档绝大部分分数构成（每种分数体验都能出现）。"""
        rng = random.Random(20261021)
        drawn = set()
        for _ in range(800):
            combo = self.brutal_mod.random_brutal(6, rng)
            drawn.add(combo.points)
        all_profiles = set(self.brutal_mod._score_profiles(6))
        self.assertGreaterEqual(len(drawn), len(all_profiles) * 0.9)
        self.assertLessEqual(drawn, all_profiles)

    def test_random_brutal_satisfies_rules(self) -> None:
        """随机结果始终满足所在档位的点数与个数规律。"""
        rng = random.Random(20261019)
        for _ in range(200):
            level = rng.randint(1, 6)
            combo = self.brutal_mod.random_brutal(level, rng)
            self.assertTrue(combo.rule.allows(combo.total, len(combo.factors)))
            self.assertEqual(len(set(combo.factors)), len(combo.factors))
            # 展示顺序按分数从高到低
            self.assertEqual(tuple(sorted(combo.points, reverse=True)), combo.points)

    def test_random_brutal_count_variety(self) -> None:
        """同档多次抽取覆盖不同因子个数（先均匀选个数再选组合）。"""
        rng = random.Random(42)
        counts = {
            len(self.brutal_mod.random_brutal(5, rng).factors) for _ in range(120)
        }
        self.assertEqual(counts, {2, 3, 4})

    def test_random_brutal_invalid_level(self) -> None:
        for level in (0, 7, -1, 99):
            with self.assertRaises(ValueError):
                self.brutal_mod.random_brutal(level)

    def test_parse_brutal_level(self) -> None:
        """「3」「+3」「残酷+3」「残酷+」等写法均可解析。"""
        parse = self.brutal_mod.parse_brutal_level
        self.assertIsNone(parse(""))
        self.assertIsNone(parse("  "))
        self.assertEqual(parse("3"), 3)
        self.assertEqual(parse("+3"), 3)
        self.assertEqual(parse("残酷+3"), 3)
        self.assertEqual(parse("残酷+"), 1)  # 游戏内「残酷+」即残酷+1
        self.assertEqual(parse("０３"), 3)  # 全角数字
        self.assertEqual(parse("＋３"), 3)  # 全角加号与数字
        self.assertEqual(parse("3级"), 3)
        self.assertIsNone(parse("残酷"))  # 裸「残酷」是基础残酷难度
        self.assertIsNone(parse("0"))
        self.assertIsNone(parse("7"))
        self.assertIsNone(parse("10"))
        self.assertIsNone(parse("abc"))

    def test_commanders_pool(self) -> None:
        """指挥官共 18 位（「汉和霍纳」分列），无重复，且都有头像。"""
        commanders = self.brutal_mod.COMMANDERS
        self.assertEqual(len(commanders), 18)
        self.assertEqual(len(set(commanders)), 18)
        for name in commanders:
            self.assertIsNotNone(
                self.book.commander_icon_path(name), f"缺少指挥官头像：{name}"
            )

    def test_random_brutal_with_commanders(self) -> None:
        """开启指挥官随机时抽取两位不重复的池内指挥官，关闭时为空。"""
        rng = random.Random(20261020)
        for _ in range(30):
            combo = self.brutal_mod.random_brutal(
                rng.randint(1, 6), rng, with_commanders=True
            )
            self.assertEqual(len(combo.commanders), 2)
            self.assertEqual(len(set(combo.commanders)), 2)
            for name in combo.commanders:
                self.assertIn(name, self.brutal_mod.COMMANDERS)
            self.assertEqual(
                self.brutal_mod.random_brutal(rng.randint(1, 6), rng).commanders, ()
            )

    def test_parse_brutal_args(self) -> None:
        """「指挥官」开关与难度可一起解析；无法识别时返回 None。"""
        parse = self.brutal_mod.parse_brutal_args
        self.assertEqual(parse(""), (None, False))
        self.assertEqual(parse("3"), (3, False))
        self.assertEqual(parse("指挥官"), (None, True))
        self.assertEqual(parse("随机指挥官"), (None, True))
        self.assertEqual(parse("3指挥官"), (3, True))
        self.assertEqual(parse("指挥官3"), (3, True))
        self.assertEqual(parse("随机残酷 3 指挥官"), (3, True))
        self.assertEqual(parse("残酷+3 指挥官"), (3, True))
        self.assertEqual(parse("３指挥官"), (3, True))  # 全角数字
        self.assertIsNone(parse("abc"))
        self.assertIsNone(parse("7指挥官"))
        self.assertIn("指挥官", self.plugin_mod.BRUTAL_USAGE_HINT)

    def test_coop_maps(self) -> None:
        """合作地图共 15 张，无重复。"""
        maps = self.brutal_mod.COOP_MAPS
        self.assertEqual(len(maps), 15)
        self.assertEqual(len(set(maps)), 15)

    def test_coop_map_icons(self) -> None:
        """全部合作地图都有图片。"""
        for name in self.brutal_mod.COOP_MAPS:
            self.assertIsNotNone(self.book.map_icon_path(name), f"缺少地图图片：{name}")

    def test_amon_armies(self) -> None:
        """埃蒙部队共 19 支：星灵 8、人类 6、异虫 5，番号不重复。"""
        armies = self.brutal_mod.AMON_ARMIES
        self.assertEqual(len(armies), 19)
        by_race: dict[str, int] = {}
        for army in armies:
            by_race[army.race] = by_race.get(army.race, 0) + 1
        self.assertEqual(by_race, {"星灵": 8, "人类": 6, "异虫": 5})
        names = [army.name for army in armies]
        self.assertEqual(len(set(names)), 19)

    def test_random_brutal_includes_map_and_army(self) -> None:
        """每次生成都附带池内的地图与埃蒙部队编制。"""
        rng = random.Random(20261019)
        for _ in range(50):
            combo = self.brutal_mod.random_brutal(rng.randint(1, 6), rng)
            self.assertIn(combo.map_name, self.brutal_mod.COOP_MAPS)
            self.assertIn(combo.army, self.brutal_mod.AMON_ARMIES)

    def test_brutal_card_text(self) -> None:
        combo = self.brutal_mod.random_brutal(3, random.Random(7))
        bot = cast("Bot", _FakeBot("OneBot V11"))
        text = self.plugin_mod._brutal_card(bot, combo).extract_plain_text()
        self.assertIn("【随机残酷+3】", text)
        self.assertIn(f"地图：{combo.map_name}", text)
        self.assertIn(f"埃蒙部队：{combo.army.race} · {combo.army.name}", text)
        self.assertIn(f"点数总和：{combo.total}", text)
        self.assertIn("（残酷+3 规则：点数 9～10 · 因子 2～3 个）", text)
        for name, points in zip(combo.factors, combo.points):
            self.assertIn(f"{name}（{points}分）", text)

    def test_brutal_card_images(self) -> None:
        """OneBot 侧地图与每个因子各一个内联图标；QQ 侧拼为一张图。"""
        combo = self.brutal_mod.random_brutal(6, random.Random(11))
        card = self.plugin_mod._brutal_card(cast("Bot", _FakeBot("OneBot V11")), combo)
        images = [seg for seg in card if seg.type == "image"]
        self.assertEqual(len(images), 1 + len(combo.factors))  # 地图 + 因子

        qq_card = self.plugin_mod._brutal_card(cast("Bot", _FakeBot("QQ")), combo)
        qq_images = [seg for seg in qq_card if seg.type == "file_image"]
        self.assertEqual(len(qq_images), 1)
        self.assertTrue(bytes(qq_images[0].data["content"]).startswith(b"\x89PNG"))
        self.assertEqual(
            str(qq_images[0].data["file_name"]), f"随机残酷{combo.level}_因子.png"
        )

    def test_brutal_card_with_commanders(self) -> None:
        """开启指挥官随机时文字带指挥官行，图标数随之增加。"""
        combo = self.brutal_mod.random_brutal(3, random.Random(3), with_commanders=True)
        bot = cast("Bot", _FakeBot("OneBot V11"))
        card = self.plugin_mod._brutal_card(bot, combo)
        text = card.extract_plain_text()
        self.assertIn(f"指挥官：{'、'.join(combo.commanders)}", text)
        images = [seg for seg in card if seg.type == "image"]
        self.assertEqual(len(images), 1 + len(combo.commanders) + len(combo.factors))

        qq_card = self.plugin_mod._brutal_card(cast("Bot", _FakeBot("QQ")), combo)
        qq_images = [seg for seg in qq_card if seg.type == "file_image"]
        self.assertEqual(len(qq_images), 1)
        qq_text = qq_card.extract_plain_text()
        self.assertIn(f"指挥官：{'、'.join(combo.commanders)}", qq_text)

    def test_brutal_command_registered(self) -> None:
        """指令已注册；「残酷+3」按最长前缀解析为「残酷+」命令。"""
        plugin = nonebot.get_plugin("tubian")
        assert plugin is not None
        self.assertEqual(len(plugin.matcher), 10)  # 9 指令 + 1 补发兜底
        pf = TrieRule.prefix.longest_prefix("残酷+3")
        self.assertEqual(pf.key, "残酷+")
        self.assertEqual(pf.value.command, ("残酷+",))


class TestFactorBook(unittest.TestCase):
    """因子效果数据加载与查询。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.plugin_mod, _ = _load()
        cls.factors_mod = cls.plugin_mod.factors
        cls.data_dir = cls.plugin_mod.config.tubian_data_dir
        cls.book = cls.factors_mod.FactorBook.load(cls.data_dir)

    def test_load_counts(self) -> None:
        self.assertEqual(self.book.count, 68)
        by_cat: dict[str, int] = {}
        for factor in self.book.factors:
            by_cat[factor.category] = by_cat.get(factor.category, 0) + 1
        self.assertEqual(by_cat, {"常规": 52, "限定": 10, "未实装": 6})

    def test_names_match_brutal_pools(self) -> None:
        brutal_mod = self.plugin_mod.brutal
        names = {f.name for f in self.book.factors}
        expected = (
            set(brutal_mod.FACTOR_POINTS)
            | set(brutal_mod.LIMITED_FACTORS)
            | set(brutal_mod.UNRELEASED_FACTORS)
        )
        self.assertEqual(names, expected)
        for factor in self.book.factors:
            if factor.name in brutal_mod.FACTOR_POINTS:
                self.assertEqual(factor.category, "常规")
                self.assertEqual(factor.points, brutal_mod.FACTOR_POINTS[factor.name])
            else:
                self.assertIsNone(factor.points)

    def test_effect_fields(self) -> None:
        factor = self.book.get("短视症")
        assert factor is not None
        self.assertEqual(factor.en_name, "Shortsighted")
        self.assertEqual(factor.points, 1)
        self.assertTrue(factor.summary)
        self.assertTrue(factor.details)
        self.assertEqual(factor.points_text, "1分")
        nested = self.book.get("强行征用")
        assert nested is not None
        self.assertIn("\n·", nested.details)

    def test_unreleased_placeholder(self) -> None:
        factor = self.book.get("不死邪魔")
        assert factor is not None
        self.assertEqual(factor.category, "未实装")
        self.assertIsNone(factor.points)
        self.assertEqual(factor.points_text, "")
        self.assertEqual(factor.en_name, "")

    def test_get_normalizes_punctuation(self) -> None:
        self.assertIsNotNone(self.book.get("给我死吧！"))
        self.assertIsNotNone(self.book.get("给我死吧!"))
        self.assertIsNotNone(self.book.get("  短视症  "))
        self.assertIsNone(self.book.get("不存在的因子"))

    def test_search_by_name_and_english(self) -> None:
        self.assertEqual([f.name for f in self.book.search("机器人")], ["杀戮机器人", "炸弹机器人"])
        self.assertEqual([f.name for f in self.book.search("void")], ["虚空重生者", "虚空裂隙"])
        self.assertEqual(self.book.search("不存在的因子"), [])

    def test_missing_file(self) -> None:
        with self.assertRaises(self.factors_mod.FactorDataError):
            self.factors_mod.FactorBook.load(ROOT / "不存在的目录")

    def test_resolve_exact(self) -> None:
        factor, reply = self.plugin_mod._resolve_factor_query(self.book, "给我死吧!")
        self.assertIsNone(reply)
        assert factor is not None
        self.assertEqual(factor.name, "给我死吧！")

    def test_resolve_fuzzy_and_miss(self) -> None:
        factor, reply = self.plugin_mod._resolve_factor_query(self.book, "短视")
        self.assertIsNone(reply)
        assert factor is not None
        self.assertEqual(factor.name, "短视症")

        factor, reply = self.plugin_mod._resolve_factor_query(self.book, "机器人")
        self.assertIsNone(factor)
        assert reply is not None
        self.assertIn("找到 2 个与「机器人」相关的因子", reply)
        self.assertIn("杀戮机器人", reply)

        factor, reply = self.plugin_mod._resolve_factor_query(self.book, "不存在的因子")
        self.assertIsNone(factor)
        self.assertEqual(reply, "未找到名称包含「不存在的因子」的因子")

        factor, reply = self.plugin_mod._resolve_factor_query(self.book, "")
        self.assertIsNone(factor)
        assert reply is not None
        self.assertIn("因子查询用法", reply)

    def test_effect_text(self) -> None:
        factor = self.book.get("虚空重生者")
        assert factor is not None
        text = self.plugin_mod._effect_text(factor)
        self.assertIn("【因子查询】虚空重生者（Void Reanimators）", text)
        self.assertIn("类别：常规 · 分数：5分", text)
        self.assertIn(f"效果：{factor.summary}", text)
        self.assertIn("详细说明：", text)
        self.assertIn("补给0-2需1.5秒", text)

    def test_effect_card_images(self) -> None:
        """OneBot 侧内联图标在文本之后；QQ 侧图标在文本之前。"""
        factor = self.book.get("短视症")
        assert factor is not None
        onebot = self.plugin_mod._effect_card(
            cast("Bot", _FakeBot("OneBot V11")), factor
        )
        types = [seg.type for seg in onebot]
        self.assertIn("image", types)
        self.assertGreater(types.index("image"), types.index("text"))

        qq = self.plugin_mod._effect_card(cast("Bot", _FakeBot("QQ")), factor)
        qq_types = [seg.type for seg in qq]
        self.assertIn("file_image", qq_types)
        self.assertLess(qq_types.index("file_image"), qq_types.index("text"))

    def test_effect_card_without_icon(self) -> None:
        """图标缺失时退化为纯文本卡片。"""
        factor = self.book.get("不死邪魔")
        assert factor is not None
        card = self.plugin_mod._effect_card(
            cast("Bot", _FakeBot("OneBot V11")), factor
        )
        self.assertEqual([seg.type for seg in card], ["text"])

    def test_factor_command_registered(self) -> None:
        """「因子查询」按最长前缀解析，不被「因子」截胡。"""
        plugin = nonebot.get_plugin("tubian")
        assert plugin is not None
        pf = TrieRule.prefix.longest_prefix("因子查询 短视症")
        self.assertEqual(pf.key, "因子查询")
        pf2 = TrieRule.prefix.longest_prefix("因子 短视症")
        self.assertEqual(pf2.key, "因子")


class TestPluginSmoke(unittest.TestCase):
    """插件注册冒烟测试。"""

    def test_plugin_loaded(self) -> None:
        plugin = nonebot.get_plugin("tubian")
        assert plugin is not None
        self.assertEqual(plugin.name, "tubian")
        self.assertEqual(len(plugin.matcher), 10)  # 9 指令 + 1 补发兜底

    def test_metadata(self) -> None:
        plugin_mod, _ = _load()
        meta = plugin_mod.__plugin_meta__
        self.assertEqual(meta.name, "星际争霸每周突变")
        self.assertIn("突变查询 <名称>", meta.usage)
        self.assertIn("因子查询 <名称>", meta.usage)
        self.assertIn("随机残酷 [1~6]", meta.usage)
        self.assertIn("帮助 / help", meta.usage)

    def test_help_command_registered(self) -> None:
        """「帮助」指令已注册，别名 help 同样按前缀命中。"""
        plugin = nonebot.get_plugin("tubian")
        assert plugin is not None
        pf = TrieRule.prefix.longest_prefix("帮助")
        self.assertEqual(pf.key, "帮助")
        self.assertEqual(pf.value.command, ("帮助",))
        pf2 = TrieRule.prefix.longest_prefix("help me")
        self.assertEqual(pf2.key, "help")
        self.assertEqual(pf2.value.command, ("help",))

    def test_help_text_lists_all_commands(self) -> None:
        """帮助文本覆盖插件全部指令（含别名；推送指令已下线，不列）。"""
        plugin_mod, _ = _load()
        for phrase in (
            "突变 / 本周突变 / tb",
            "下周突变 / tbx",
            "突变 <序号>",
            "突变查询 <名称>",
            "因子查询 <名称> / 因子 <名称>",
            "随机残酷 [1~6] / 残酷随机 / 残酷+<N> / sjck",
            "帮助 / help",
        ):
            self.assertIn(phrase, plugin_mod.HELP_TEXT)


class TestPush(unittest.TestCase):
    """定时推送：订阅存储、目标提取、推送时机与发送。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.plugin_mod, _ = _load()
        cls.push_mod = cls.plugin_mod.push

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = self.push_mod.PushStore(
            Path(self._tmp.name) / "push_targets.json"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _target(self, **kwargs: str) -> Any:
        data: dict[str, str] = {
            "adapter": "QQ",
            "bot_id": "102308151",
            "scene": "group",
            "target_id": "g1",
        }
        data.update(kwargs)
        return self.push_mod.PushTarget(**data)

    def test_store_add_remove(self) -> None:
        target = self._target()
        self.assertTrue(self.store.add(target))
        self.assertFalse(self.store.add(target))  # 同一目标去重
        self.assertEqual(self.store.targets(), (target,))
        self.assertTrue(self.store.remove(target))
        self.assertFalse(self.store.remove(target))
        self.assertEqual(self.store.targets(), ())

    def test_store_persists(self) -> None:
        self.store.add(self._target())
        self.store.add(self._target(adapter="OneBot V11", bot_id="2", target_id="9"))
        again = self.push_mod.PushStore(self.store.path)
        self.assertEqual(len(again.targets()), 2)
        self.assertIn(self._target(), again.targets())

    def test_store_missing_and_corrupt_file(self) -> None:
        self.assertEqual(self.store.targets(), ())
        self.store.path.write_text("not json at all", encoding="utf-8")
        self.assertEqual(self.store.targets(), ())
        self.assertTrue(self.store.add(self._target()))
        self.assertEqual(self.store.targets(), (self._target(),))

    def test_extract_target_onebot(self) -> None:
        from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent

        bot = cast("Bot", _FakeBot("OneBot V11", self_id="ob-bot"))
        group = GroupMessageEvent.model_construct(group_id=123)
        self.assertEqual(
            self.push_mod.extract_target(bot, group),
            self._target(
                adapter="OneBot V11", bot_id="ob-bot", scene="group", target_id="123"
            ),
        )
        private = PrivateMessageEvent.model_construct(user_id=456)
        self.assertEqual(
            self.push_mod.extract_target(bot, private),
            self._target(
                adapter="OneBot V11",
                bot_id="ob-bot",
                scene="private",
                target_id="456",
            ),
        )

    def test_extract_target_qq(self) -> None:
        from nonebot.adapters.qq import C2CMessageCreateEvent, GroupMessageCreateEvent
        from nonebot.adapters.qq.models import FriendAuthor, GroupMemberAuthor

        bot = cast("Bot", _FakeBot("QQ", self_id="102308151"))
        author = GroupMemberAuthor(
            id="a1", bot=False, member_openid="m1", member_role="member"
        )
        group = GroupMessageCreateEvent.model_construct(
            group_openid="go1", author=author
        )
        self.assertEqual(
            self.push_mod.extract_target(bot, group),
            self._target(scene="group", target_id="go1"),
        )
        c2c = C2CMessageCreateEvent.model_construct(
            author=FriendAuthor(id="f1", user_openid="u1")
        )
        self.assertEqual(
            self.push_mod.extract_target(bot, c2c),
            self._target(scene="c2c", target_id="f1"),
        )

    def test_extract_target_unsupported(self) -> None:
        bot = cast("Bot", _FakeBot("QQ", self_id="102308151"))
        other = OB11Message("hi")
        self.assertIsNone(self.push_mod.extract_target(bot, other))
        self.assertIsNone(
            self.push_mod.extract_target(cast("Bot", _FakeBot("Other")), other)
        )

    def test_push_due(self) -> None:
        """推送日到点后当周推一次；当天启动补推，其余时间不推。"""
        due = self.push_mod.push_due

        def at_8(now: datetime, last: date | None) -> bool:
            return due(now, last, 0, 8, 0)  # 默认：周一 08:00

        monday8 = datetime(2026, 10, 19, 8, 0)
        self.assertTrue(at_8(monday8, None))
        self.assertFalse(at_8(datetime(2026, 10, 19, 7, 59), None))
        self.assertFalse(at_8(datetime(2026, 10, 18, 8, 0), None))  # 周日
        self.assertFalse(at_8(datetime(2026, 10, 20, 8, 0), None))  # 周二
        # 同一周内（以周一计）不重复推送
        self.assertFalse(at_8(datetime(2026, 10, 19, 10, 0), date(2026, 10, 19)))
        # 上周推送过则本周要推
        self.assertTrue(at_8(monday8, date(2026, 10, 12)))
        # 周一白天/晚间启动均补推
        self.assertTrue(at_8(datetime(2026, 10, 19, 21, 0), None))
        self.assertEqual(
            self.push_mod.week_monday(datetime(2026, 10, 20, 12, 0)), date(2026, 10, 19)
        )

    def test_push_due_custom_schedule(self) -> None:
        """线上定时测试：把推送日/时刻临时改为当天的几分钟后即可观察触发。"""
        due = self.push_mod.push_due
        # 周二（weekday=1）14:30 推送
        self.assertFalse(due(datetime(2026, 10, 20, 14, 29), None, 1, 14, 30))
        self.assertTrue(due(datetime(2026, 10, 20, 14, 30), None, 1, 14, 30))
        self.assertFalse(due(datetime(2026, 10, 20, 15, 0), None, 2, 14, 30))
        # 时刻未到不推（周三 21:00 的任务在 20:00 不触发）
        self.assertFalse(due(datetime(2026, 10, 21, 20, 0), None, 2, 21, 0))
        self.assertTrue(due(datetime(2026, 10, 21, 21, 0), None, 2, 21, 0))
        self.assertEqual(self.push_mod.schedule_text(0, 8, 0), "每周一 08:00")
        self.assertEqual(self.push_mod.schedule_text(1, 14, 30), "每周二 14:30")

    def test_push_config_defaults(self) -> None:
        """推送时刻默认周一 08:00，可用 TUBIAN_PUSH_* 覆盖。"""
        cfg = self.plugin_mod.config
        self.assertEqual(cfg.tubian_push_weekday, 0)
        self.assertEqual(cfg.tubian_push_hour, 8)
        self.assertEqual(cfg.tubian_push_minute, 0)
        self.assertEqual(
            self.plugin_mod._schedule_text(),
            self.push_mod.schedule_text(0, 8, 0),
        )

    def test_push_all_and_offline_skip(self) -> None:
        """按适配器分发发送；不在线的机器人跳过且不影响其余目标。"""
        sent: list[tuple[str, str, Any]] = []

        class _SendBot(_FakeBot):
            async def send_group_msg(self, group_id: int, message: Any) -> None:
                sent.append(("ob_group", str(group_id), message))

            async def send_to_group(self, group_openid: str, message: Any) -> None:
                sent.append(("qq_group", group_openid, message))

        ob_bot = cast("Bot", _SendBot("OneBot V11", self_id="ob-bot"))
        qq_bot = cast("Bot", _SendBot("QQ", self_id="102308151"))
        bots = {"ob-bot": ob_bot, "102308151": qq_bot}
        targets = (
            self._target(),
            self._target(adapter="OneBot V11", bot_id="ob-bot", target_id="9"),
            self._target(bot_id="offline-bot"),
        )
        original = self.push_mod.get_bot
        self.push_mod.get_bot = lambda self_id: bots[self_id]  # type: ignore[assignment]
        try:
            summary = asyncio.run(
                self.push_mod.push_all(targets, lambda bot: f"card-{bot.self_id}")
            )
        finally:
            self.push_mod.get_bot = original  # type: ignore[assignment]
        self.assertEqual(summary.sent, 2)
        self.assertEqual(summary.attempted, 3)
        self.assertEqual(len(summary.errors), 1)
        self.assertIn("不在线", summary.errors[0])
        self.assertEqual(len(summary.succeeded), 2)
        self.assertEqual(
            sorted(sent),
            sorted(
                [
                    ("qq_group", "g1", "card-102308151"),
                    ("ob_group", "9", "card-ob-bot"),
                ]
            ),
        )

    def test_describe_send_error_permission(self) -> None:
        """QQ 主动消息无权限（40034105）时给出开通指引。"""

        class _ActionFailed(Exception):
            code = 40034105

            def __str__(self) -> str:
                return "主动消息失败, 无权限"

        text = self.push_mod.describe_send_error(_ActionFailed())
        self.assertIn("40034105", text)
        self.assertIn("主动消息", text)
        self.assertIn("补发", text)
        self.assertEqual(
            self.push_mod.describe_send_error(ValueError("boom")), "boom"
        )

    def test_store_last_pushed_and_pending(self) -> None:
        """登记送达时间后，本周不再列为待发目标；旧格式无该字段仍可读。"""
        week = date(2026, 10, 19)
        target = self._target()
        self.store.add(target)
        self.assertIsNone(self.store.last_pushed(target))
        self.assertEqual(self.store.pending_targets(week), (target,))
        self.store.mark_pushed(target, week)
        self.assertEqual(self.store.last_pushed(target), week)
        self.assertEqual(self.store.pending_targets(week), ())
        # 持久化后新实例可见
        again = self.push_mod.PushStore(self.store.path)
        self.assertEqual(again.last_pushed(target), week)
        self.assertEqual(again.pending_targets(week), ())
        self.assertEqual(again.pending_targets(date(2026, 10, 26)), (target,))
        # 旧格式（无 last_pushed 字段）视作未送达
        self.store.path.write_text(
            '[{"adapter": "QQ", "bot_id": "102308151", "scene": "group",'
            ' "target_id": "g1"}]',
            encoding="utf-8",
        )
        legacy = self.push_mod.PushStore(self.store.path)
        self.assertEqual(legacy.targets(), (target,))
        self.assertIsNone(legacy.last_pushed(target))

    def test_lazy_due(self) -> None:
        """补发条件：本周推送时刻已过且本会话本周尚未送达。"""
        due = self.push_mod.lazy_due
        week = date(2026, 10, 19)  # 周一
        # 推送时刻（周一 08:00）之前不补发
        self.assertFalse(due(datetime(2026, 10, 19, 7, 59), None, 0, 8, 0))
        self.assertTrue(due(datetime(2026, 10, 19, 8, 0), None, 0, 8, 0))
        # 本周已送达则不补发；上周送达仍要补
        self.assertFalse(due(datetime(2026, 10, 21, 12, 0), week, 0, 8, 0))
        self.assertTrue(due(datetime(2026, 10, 21, 12, 0), date(2026, 10, 12), 0, 8, 0))
        # 推送日设为周三：周二不补发，周三到点后补发
        self.assertFalse(due(datetime(2026, 10, 20, 12, 0), None, 2, 14, 30))
        self.assertFalse(due(datetime(2026, 10, 21, 14, 29), None, 2, 14, 30))
        self.assertTrue(due(datetime(2026, 10, 21, 14, 30), None, 2, 14, 30))

    def _send_bot(self, sent: list[Any]) -> "Bot":
        class _SendBot(_FakeBot):
            async def send_to_group(self, group_openid: str, message: Any) -> None:
                sent.append(message)

        return cast("Bot", _SendBot("QQ", self_id="102308151"))

    def test_weekly_push_builds_card(self) -> None:
        """定时任务本体：向订阅会话发出「本周突变」卡片并登记送达。"""
        sent: list[Any] = []
        bot = self._send_bot(sent)
        target = self._target()
        self.assertTrue(self.store.add(target))
        original_store = self.plugin_mod._store
        original_get_bot = self.push_mod.get_bot
        self.plugin_mod._store = self.store
        self.push_mod.get_bot = lambda self_id: bot  # type: ignore[assignment]
        try:
            summary = asyncio.run(self.plugin_mod._push_weekly())
            # 本周已送达：定时路径不再重发
            again = asyncio.run(self.plugin_mod._push_weekly())
        finally:
            self.push_mod.get_bot = original_get_bot  # type: ignore[assignment]
            self.plugin_mod._store = original_store
        self.assertEqual((summary.sent, summary.attempted), (1, 1))
        self.assertEqual((again.sent, again.attempted), (0, 0))
        self.assertEqual(len(sent), 1)
        text = sent[0].extract_plain_text()
        self.assertIn("【本周突变】", text)
        self.assertIn("因子", text)
        self.assertEqual(
            self.store.last_pushed(target),
            self.push_mod.week_monday(datetime.now()),
        )

    def test_weekly_push_force_repeats_without_marking(self) -> None:
        """「测试推送」全量重发且不改动送达记录，可反复测试。"""
        sent: list[Any] = []
        bot = self._send_bot(sent)
        target = self._target()
        self.assertTrue(self.store.add(target))
        original_store = self.plugin_mod._store
        original_get_bot = self.push_mod.get_bot
        self.plugin_mod._store = self.store
        self.push_mod.get_bot = lambda self_id: bot  # type: ignore[assignment]
        try:
            first = asyncio.run(self.plugin_mod._push_weekly(force=True))
            second = asyncio.run(self.plugin_mod._push_weekly(force=True))
        finally:
            self.push_mod.get_bot = original_get_bot  # type: ignore[assignment]
            self.plugin_mod._store = original_store
        self.assertEqual((first.sent, second.sent), (1, 1))
        self.assertEqual(len(sent), 2)
        self.assertIsNone(self.store.last_pushed(target))

    def test_lazy_push_sends_and_marks(self) -> None:
        """补发兜底：被动回复本周突变卡片并登记送达，之后不再重复。"""
        from nonebot.adapters.qq.models import GroupMemberAuthor

        sent: list[Any] = []
        target = self._target()

        class _LazyBot(_FakeBot):
            async def send(self, event: Any, message: Any) -> Any:
                sent.append(message)
                return message

        bot = cast("Bot", _LazyBot("QQ", self_id="102308151"))
        author = GroupMemberAuthor(
            id="a1", bot=False, member_openid="m1", member_role="member"
        )
        from nonebot.adapters.qq import GroupMessageCreateEvent

        event = GroupMessageCreateEvent.model_construct(
            group_openid="g1", author=author
        )
        original_store = self.plugin_mod._store
        self.plugin_mod._store = self.store
        try:
            # 未订阅的会话不补发
            self.assertFalse(
                asyncio.run(
                    self.plugin_mod._maybe_lazy_push(
                        bot, event, datetime(2026, 10, 19, 8, 5)
                    )
                )
            )
            self.assertTrue(self.store.add(target))
            # 推送时刻之前：不补发
            self.assertFalse(
                asyncio.run(
                    self.plugin_mod._maybe_lazy_push(
                        bot, event, datetime(2026, 10, 19, 7, 59)
                    )
                )
            )
            self.assertTrue(
                asyncio.run(
                    self.plugin_mod._maybe_lazy_push(
                        bot, event, datetime(2026, 10, 19, 8, 5)
                    )
                )
            )
            # 同一周内不重复补发
            self.assertFalse(
                asyncio.run(
                    self.plugin_mod._maybe_lazy_push(
                        bot, event, datetime(2026, 10, 19, 9, 0)
                    )
                )
            )
        finally:
            self.plugin_mod._store = original_store
        self.assertEqual(len(sent), 1)
        text = sent[0].extract_plain_text()
        self.assertIn("【本周突变】", text)
        self.assertIn("定时推送补发", text)
        self.assertEqual(self.store.last_pushed(target), date(2026, 10, 19))

    def test_subscribe_commands_registered(self) -> None:
        """「订阅突变 / 退订突变」已注册，带/不带「/」前缀均可命中。"""
        plugin = nonebot.get_plugin("tubian")
        assert plugin is not None
        for word in ("订阅突变", "退订突变"):
            pf = TrieRule.prefix.longest_prefix(word)
            self.assertEqual(pf.key, word)
            self.assertEqual(pf.value.command, (word,))
            pf2 = TrieRule.prefix.longest_prefix(f"/{word} now")
            self.assertEqual(pf2.key, f"/{word}")
            self.assertEqual(pf2.value.command, (word,))


if __name__ == "__main__":
    unittest.main()
