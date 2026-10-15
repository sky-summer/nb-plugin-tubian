"""nonebot-plugin-tubian 测试。

在项目根目录下运行：

    python -m unittest tests.test_tubian -v
"""

import base64
import sys
import unittest
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import nonebot
from nonebot.adapters.onebot.v11 import Message as OB11Message

if TYPE_CHECKING:
    from nonebot.adapters import Bot, Event

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
    """伪造 Bot，仅支持 adapter.get_name。"""

    def __init__(self, name: str) -> None:
        self.adapter = _FakeAdapter(name)


class _FakeEvent:
    """伪造 Event，get_message 返回空的 OneBot V11 消息。"""

    def get_message(self) -> OB11Message:
        return OB11Message()


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

    def test_icon_path(self) -> None:
        icon = self.book.icon_path("异形寄生")
        assert icon is not None
        self.assertTrue(icon.is_file())
        self.assertIsNone(self.book.icon_path("不存在的因子"))

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

    def _card(self, adapter_name: str, mutation: Any, label: str) -> Any:
        bot = cast("Bot", _FakeBot(adapter_name))
        event = cast("Event", _FakeEvent())
        return self.plugin_mod._build_card(bot, event, self.book, mutation, label)

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
        self.assertIn("右侧数字：7", text)

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
        self.assertEqual(len(images), 3)
        for seg in images:
            file = str(seg.data["file"])
            self.assertTrue(file.startswith("base64://"), file[:32])
            self.assertTrue(
                base64.b64decode(file.removeprefix("base64://")).startswith(b"\x89PNG")
            )

    def test_card_images_qq(self) -> None:
        """QQ 侧传 bytes 而非路径，由适配器自行上传。"""
        mutation = self.book.at(date(2026, 10, 15))
        card = self._card("QQ", mutation, "本周突变")
        images = [seg for seg in card if seg.type == "file_image"]
        self.assertEqual(len(images), 3)
        for seg in images:
            self.assertIsInstance(seg.data["content"], bytes)
            self.assertTrue(bytes(seg.data["content"]).startswith(b"\x89PNG"))
            self.assertTrue(str(seg.data["file_name"]).endswith(".png"))

    def test_card_fallback_text_only(self) -> None:
        mutation = self.book.at(date(2026, 10, 15))
        card = self._card("Other", mutation, "本周突变")
        self.assertTrue(all(seg.type == "text" for seg in card))

    def test_search_summary(self) -> None:
        found = self.book.search("灾难之轮")
        summary = self.plugin_mod._search_summary(found, "灾难之轮")
        self.assertIn("找到 6 个", summary)
        self.assertIn("#11", summary)
        self.assertIn("突变 <序号>", summary)


class TestPluginSmoke(unittest.TestCase):
    """插件注册冒烟测试。"""

    def test_plugin_loaded(self) -> None:
        plugin = nonebot.get_plugin("tubian")
        assert plugin is not None
        self.assertEqual(plugin.name, "tubian")
        self.assertEqual(len(plugin.matcher), 2)

    def test_metadata(self) -> None:
        plugin_mod, _ = _load()
        meta = plugin_mod.__plugin_meta__
        self.assertEqual(meta.name, "星际争霸每周突变")
        self.assertIn("突变", meta.usage)


if __name__ == "__main__":
    unittest.main()
