"""nonebot-plugin-tubian —— 星际争霸每周突变查询（QQ 官方机器人版）。

根据「突变列表数据.xlsx」与当前时间自动选中本周突变并返回，
支持查询下周突变、按序号查询、「突变查询 <名称>」按名称搜索，
「随机残酷 [1~6]」随机生成残酷+开局（因子组合、地图与埃蒙部队），
以及「订阅突变」定时推送：每周一 08:00 向订阅会话发送本周突变，
主动消息无权限时随会话的下一条消息补发（被动回复，无需该权限）。

QQ 官方接口每条消息只接受一个媒体文件，本版将全部因子图标
拼接为一张长图，作为单条消息的唯一媒体浮在文字卡上方。
"""

import asyncio
import random
from datetime import date, datetime, timedelta
from functools import lru_cache
from io import BytesIO
from typing import NamedTuple

from nonebot import get_driver, logger, on_command, on_message
from nonebot.adapters import Bot, Event, Message
from nonebot.adapters import MessageSegment as BaseMessageSegment
from nonebot.matcher import Matcher
from nonebot.message import event_preprocessor
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata
from nonebot.rule import TRIE_VALUE, TrieRule

from .brutal import MAX_LEVEL, BrutalCombo, parse_brutal_level, random_brutal
from .config import Config, config
from .data import (
    Mutation,
    MutationBook,
    MutationDataError,
    fmt_date,
    local_today,
    normalize_query,
)
from .factors import Factor, FactorBook, FactorDataError
from .push import (
    PushStore,
    PushSummary,
    default_store_path,
    extract_target,
    lazy_due,
    push_all,
    push_due,
    schedule_text,
    week_monday,
)

SEARCH_LIMIT = 10

__plugin_meta__ = PluginMetadata(
    name="星际争霸每周突变",
    description="根据突变列表 Excel 与当前时间，自动选中本周突变并返回",
    usage=(
        "突变 / 本周突变 / tb — 查询本周突变（按当前时间自动选中）\n"
        "下周突变 / tbx — 查询下周突变\n"
        "突变 <序号> — 按轮换序号查询，如：突变 128\n"
        "突变查询 <名称> — 按突变名称搜索，如：突变查询 恐惧神庙\n"
        "因子查询 <名称> / 因子 <名称> — 查询单个因子效果，如：因子查询 短视症\n"
        "随机残酷 [1~6] / 残酷随机 / 残酷+<N> / sjck — 随机残酷+开局：因子组合、地图与埃蒙部队，如：随机残酷 3\n"
        "订阅突变 / 退订突变 — 订阅/取消每周一 08:00 的本周突变推送\n"
        # "测试推送 — 立即执行一次推送任务（测试用）\n"
        "帮助 / help — 查看全部功能"
    ),
    type="application",
    config=Config,
)

HELP_TEXT = (
    "星际争霸每周突变查询 全部功能：\n"
    "突变 / 本周突变 / tb — 查询本周突变（按当前时间自动选中）\n"
    "下周突变 / tbx — 查询下周突变\n"
    "突变 <序号> — 按轮换序号查询，如：突变 128\n"
    "突变查询 <名称> — 按突变名称搜索，如：突变查询 恐惧神庙\n"
    "因子查询 <名称> / 因子 <名称> — 查询单个因子效果，如：因子查询 短视症\n"
    "随机残酷 [1~6] / 残酷随机 / 残酷+<N> / sjck — 随机残酷+开局：因子组合、地图与埃蒙部队，如：随机残酷 3\n"
    "订阅突变 / 退订突变 — 订阅/取消每周一 08:00 的本周突变推送\n"
    # "测试推送 — 立即执行一次推送任务（测试用）\n"
    "帮助 / help — 查看全部功能\n"
)

SEARCH_HINT = "名称查询请使用「突变查询 <名称>」，如：突变查询 恐惧神庙"

FACTOR_USAGE_HINT = (
    "因子查询用法：\n"
    "因子查询 <名称> — 查询单个因子的效果，如：因子查询 短视症\n"
    "也可写作「因子 <名称>」"
)

BRUTAL_USAGE_HINT = (
    "随机残酷用法：\n"
    "随机残酷 — 随机 1～6 难度并生成开局（因子组合、地图与埃蒙部队）\n"
    "随机残酷 <1~6> — 生成指定难度的开局，如：随机残酷 3"
)

# 与下方 on_command 注册保持一致
_COMMAND_WORDS = (
    "突变",
    "突变查询",
    "因子查询",
    "因子",
    "下周突变",
    "本周突变",
    "tbx",
    "tb",
    "随机残酷",
    "残酷随机",
    "残酷+",
    "sjck",
    "订阅突变",
    "退订突变",
    "测试推送（测试使用勿用！！！！！）",
    "帮助",
    "help",
    "usage",
)

_CURRENT_WORDS = {"本周", "这周", "当前", "now"}
_NEXT_WORDS = {"下周", "下一周", "next"}
_HELP_WORDS = {"帮助", "help", "usage"}


class _Query(NamedTuple):
    """一次指令查询的结果：卡片参数，或直接回复的文本。"""

    mutation: Mutation | None
    label: str = ""
    note: str | None = None
    reply: str | None = None


@lru_cache(maxsize=1)
def _book() -> MutationBook:
    return MutationBook.load(config.tubian_data_dir)


@lru_cache(maxsize=1)
def _factor_book() -> FactorBook:
    return FactorBook.load(config.tubian_data_dir)


def _message_class(bot: Bot) -> type[Message]:
    """按适配器取消息类。

    卡片构造只需要消息类；定时推送没有事件对象，不能从 event 推断。
    """
    if "QQ" in bot.adapter.get_name():
        from nonebot.adapters.qq import Message as AdapterMessage

        return AdapterMessage
    from nonebot.adapters.onebot.v11 import Message as AdapterMessage

    return AdapterMessage


def _image_segment(bot: Bot, data: bytes, file_name: str) -> BaseMessageSegment | None:
    """按适配器构造图片消息段，不支持图片的适配器返回 None。

    图片内容以 bytes 传入：OneBot 侧会编码为 base64 发送。
    NapCat 等实现常与机器人不在同一文件系统（如各自 Docker 部署），
    若传本地路径 / file://，对方读不到文件会在 copyfile 时报 ENOENT。
    """
    adapter_name = bot.adapter.get_name()
    if "OneBot V11" not in adapter_name and "QQ" not in adapter_name:
        return None
    if "OneBot V11" in adapter_name:
        from nonebot.adapters.onebot.v11 import MessageSegment

        return MessageSegment.image(data)
    from nonebot.adapters.qq import MessageSegment

    return MessageSegment.file_image(data, file_name=file_name)


def _icon_segment(bot: Bot, factor: str) -> BaseMessageSegment | None:
    """构造单个因子图标消息段，图标缺失或读取失败时返回 None。"""
    path = _book().icon_path(factor)
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return _image_segment(bot, data, path.name)


def _map_icon_segment(bot: Bot, map_name: str) -> BaseMessageSegment | None:
    """构造单张地图图片消息段，图片缺失或读取失败时返回 None。"""
    path = _book().map_icon_path(map_name)
    if path is None:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return _image_segment(bot, data, path.name)


_ICON_GAP = 12
"""拼接因子图标时相邻图标间的像素间距。"""


def _merged_card_image(map_name: str | None, factors: tuple[str, ...]) -> bytes | None:
    """把地图图片与一组因子图标拼为一张 PNG，无可用图片时返回 None。

    地图图片置顶，因子图标在其下横排一行；图标缺失或损坏的因子被跳过；
    Pillow 未安装时同样返回 None，调用方据此退化为纯文字卡片。
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    map_icon = None
    if map_name:
        path = _book().map_icon_path(map_name)
        if path is not None:
            try:
                with Image.open(path) as im:
                    map_icon = im.convert("RGBA")
            except OSError:
                map_icon = None
    icons = []
    for factor in factors:
        path = _book().icon_path(factor)
        if path is None:
            continue
        try:
            with Image.open(path) as icon:
                icons.append(icon.convert("RGBA"))
        except OSError:
            continue
    if map_icon is None and not icons:
        return None
    factors_width = (
        sum(icon.width for icon in icons) + _ICON_GAP * (len(icons) - 1) if icons else 0
    )
    factors_height = max((icon.height for icon in icons), default=0)
    width = max(map_icon.width if map_icon is not None else 0, factors_width)
    height = factors_height
    if map_icon is not None:
        height += map_icon.height + (_ICON_GAP if icons else 0)
    merged = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    y = 0
    if map_icon is not None:
        merged.paste(map_icon, ((width - map_icon.width) // 2, y))
        y += map_icon.height + (_ICON_GAP if icons else 0)
    offset = (width - factors_width) // 2
    for icon in icons:
        merged.paste(icon, (offset, y + (factors_height - icon.height) // 2))
        offset += icon.width + _ICON_GAP
    buffer = BytesIO()
    merged.save(buffer, format="PNG")
    return buffer.getvalue()


def _card_text(mutation: Mutation, label: str, note: str | None) -> tuple[str, str]:
    """构造卡片文字，返回 (头, 尾)。"""
    header = (
        f"【{label}】{mutation.name}\n"
        f"地图：{mutation.map_name} · 序号 {mutation.mid}\n"
        f"日期：{fmt_date(mutation.start_date)} ～ {mutation.end_date:%Y/%m/%d}\n"
    )
    today = local_today()
    if mutation.contains(today):
        header += f"剩余时间：{mutation.remaining_days(today)} 天\n"
    footer = f"因子分数：{mutation.number}" if mutation.number else ""
    if note:
        footer = f"{footer}\n{note}" if footer else note
    return header, footer


class _CardParts(NamedTuple):
    """一张卡片的内容：头尾文字、地图与因子及其展示行、拼图文件名。"""

    header: str
    footer: str
    factors: tuple[str, ...]
    """因子名称，用于取图标。"""

    factor_lines: tuple[str, ...]
    """因子展示行（如附带分数），与 factors 一一对应。"""

    image_name: str
    """QQ 侧拼接图的文件名。"""

    map_name: str | None = None
    """合作地图名，用于取地图图片；None 表示不配地图图。"""


def _qq_card(bot: Bot, parts: _CardParts) -> Message:
    """QQ：地图与全部因子图标拼为一张长图，作为单条消息的唯一媒体浮在文字上方。"""
    msg_cls = _message_class(bot)
    text = parts.header
    if parts.factor_lines:
        text += "因子：\n"
        text += "".join(f"{line}\n" for line in parts.factor_lines)
    message = msg_cls()
    image = _merged_card_image(parts.map_name, parts.factors)
    if image is not None:
        segment = _image_segment(bot, image, parts.image_name)
        if segment is not None:
            message += segment
    message += msg_cls(text + parts.footer)
    return message


def _inline_card(bot: Bot, parts: _CardParts) -> Message:
    """单条消息内联地图与全部因子图标（OneBot 等侧）。"""
    msg_cls = _message_class(bot)
    message = msg_cls()
    if parts.map_name:
        segment = _map_icon_segment(bot, parts.map_name)
        if segment is not None:
            message += segment
    message += msg_cls(parts.header)
    if parts.factor_lines:
        message += msg_cls("因子：\n")
        for factor, line in zip(parts.factors, parts.factor_lines):
            segment = _icon_segment(bot, factor)
            if segment is not None:
                message += segment
            message += msg_cls(f"{line}\n")
    if parts.footer:
        message += msg_cls(parts.footer)
    return message


def _factor_card(bot: Bot, parts: _CardParts) -> Message:
    """按适配器选择卡片形态构造消息。

    QQ 官方接口每条消息只接受一个媒体文件（多余的 file_image 段会被静默丢弃），
    故 QQ 侧把地图与全部因子图标拼接为一张图发送；OneBot 等侧保持一条消息内联图文。
    """
    if "QQ" in bot.adapter.get_name():
        return _qq_card(bot, parts)
    return _inline_card(bot, parts)


def _build_card(
    bot: Bot, mutation: Mutation, label: str, note: str | None = None
) -> Message:
    """构造突变信息卡片消息。"""
    header, footer = _card_text(mutation, label, note)
    parts = _CardParts(
        header=header,
        footer=footer,
        factors=mutation.factors,
        factor_lines=mutation.factors,
        image_name=f"突变{mutation.mid}_因子.png",
        map_name=mutation.map_name,
    )
    return _factor_card(bot, parts)


def _brutal_card(bot: Bot, combo: BrutalCombo) -> Message:
    """构造随机残酷+结果卡片：地图、埃蒙部队、因子带分数，尾部附点数小结。"""
    rule = combo.rule
    parts = _CardParts(
        header=(
            f"【随机残酷+{combo.level}】\n"
            f"地图：{combo.map_name}\n"
            f"埃蒙部队：{combo.army.race} · {combo.army.name}\n"
        ),
        footer=(
            f"点数总和：{combo.total} · 因子数：{len(combo.factors)}\n"
            f"（残酷+{combo.level} 规则：点数 {rule.min_total}～{rule.max_total}"
            f" · 因子 {rule.min_count}～{rule.max_count} 个）"
        ),
        factors=combo.factors,
        factor_lines=tuple(
            f"{name}（{points}分）" for name, points in zip(combo.factors, combo.points)
        ),
        image_name=f"随机残酷{combo.level}_因子.png",
        map_name=combo.map_name,
    )
    return _factor_card(bot, parts)


def _effect_text(factor: Factor) -> str:
    """构造单因子效果文本。"""
    head = f"【因子查询】{factor.name}"
    if factor.en_name:
        head += f"（{factor.en_name}）"
    meta = f"类别：{factor.category}"
    if factor.points_text:
        meta += f" · 分数：{factor.points_text}"
    lines = [head, meta]
    if factor.summary:
        lines.append(f"效果：{factor.summary}")
    if factor.details:
        lines.append("详细说明：")
        lines.append(factor.details)
    return "\n".join(lines)


def _effect_card(bot: Bot, factor: Factor) -> Message:
    """构造单因子效果消息：图标在前、文本在后（QQ 单媒体；其余内联）。"""
    msg_cls = _message_class(bot)
    message = msg_cls()
    segment = _icon_segment(bot, factor.name)
    if segment is not None and "QQ" in bot.adapter.get_name():
        message += segment
    message += msg_cls(_effect_text(factor))
    if segment is not None and "QQ" not in bot.adapter.get_name():
        message += segment
    return message


def _factor_search_summary(found: list[Factor], keyword: str) -> str:
    lines = [f"找到 {len(found)} 个与「{keyword}」相关的因子："]
    for factor in found[:SEARCH_LIMIT]:
        meta = f"{factor.category}"
        if factor.points_text:
            meta += f" · {factor.points_text}"
        lines.append(f"{factor.name} · {meta}")
    if len(found) > SEARCH_LIMIT:
        lines.append("……")
    lines.append("可发送「因子查询 <名称>」查看详情")
    return "\n".join(lines)


def _resolve_factor_query(book: FactorBook, text: str) -> tuple[Factor | None, str | None]:
    """把「因子查询」参数解析为 (因子, 回复文本)。

    命中单个因子时返回因子本体（走图标卡片）；否则返回直接回复的文本。
    """
    if not text or text in _HELP_WORDS:
        return None, FACTOR_USAGE_HINT
    factor = book.get(text)
    if factor is not None:
        return factor, None
    found = book.search(text)
    if not found:
        return None, f"未找到名称包含「{text}」的因子"
    if len(found) == 1:
        return found[0], None
    return None, _factor_search_summary(found, text)


def _search_summary(found: list[Mutation], keyword: str) -> str:
    lines = [f"找到 {len(found)} 个与「{keyword}」相关的突变："]
    lines += [
        f"#{m.mid} {m.name} · {m.map_name} · {m.start_date:%Y/%m/%d}"
        for m in found[:SEARCH_LIMIT]
    ]
    if len(found) > SEARCH_LIMIT:
        lines.append("……")
    lines.append("可发送「突变 <序号>」查看详情")
    return "\n".join(lines)


def _exact_matches(found: list[Mutation], keyword: str) -> list[Mutation]:
    """返回名称与关键字完全一致（忽略大小写与全/半角标点）的候选。"""
    key = normalize_query(keyword)
    return [m for m in found if normalize_query(m.name) == key]


def _prefer_mutation(found: list[Mutation], today: date) -> Mutation:
    """从同名候选中挑出最值得展示的一条：本周优先，其次距今最近。"""
    return min(
        found,
        key=lambda m: (
            0 if m.contains(today) else 1,
            abs((m.start_date - today).days),
            m.mid,
        ),
    )


def _resolve_number(book: MutationBook, text: str) -> _Query:
    mutation = book.get(int(text))
    if mutation is None:
        return _Query(None, reply=f"未找到序号为 {text} 的突变（范围 1～{book.count}）")
    return _Query(mutation, f"突变 #{mutation.mid}")


def _resolve_search(book: MutationBook, text: str, today: date) -> _Query:
    found = book.search(text)
    if not found:
        return _Query(None, reply=f"未找到名称包含「{text}」的突变")
    if len(found) == 1:
        return _Query(found[0], f"突变 #{found[0].mid}")
    exact = _exact_matches(found, text)
    if not exact:
        return _Query(None, reply=_search_summary(found, text))
    mutation = _prefer_mutation(exact, today)
    note = (
        f"同名突变共 {len(exact)} 期，当前显示 #{mutation.mid}；"
        f"可发送「突变 <序号>」查看其他"
    )
    return _Query(mutation, f"突变 #{mutation.mid}", note=note)


def _resolve_query(book: MutationBook, text: str, today: date) -> _Query:
    """把「突变」指令参数解析为卡片参数或直接回复文本。

    名称查询已移至「突变查询」指令，非序号文本回复改用该指令的提示。
    """
    if not text or text in _CURRENT_WORDS:
        return _Query(book.at(today), "本周突变")
    if text in _NEXT_WORDS:
        return _Query(book.at(today + timedelta(days=7)), "下周突变")
    if text in _HELP_WORDS:
        return _Query(None, reply=HELP_TEXT)
    if text.isdigit():
        return _resolve_number(book, text)
    return _Query(None, reply=SEARCH_HINT)


def _resolve_search_query(book: MutationBook, text: str, today: date) -> _Query:
    """把「突变查询」指令参数解析为按名称（或序号）的查询。"""
    if not text:
        return _Query(None, reply=SEARCH_HINT)
    if text in _HELP_WORDS:
        return _Query(None, reply=HELP_TEXT)
    if text.isdigit():
        return _resolve_number(book, text)
    return _resolve_search(book, text, today)


_ADDRESS_SEGMENTS = frozenset({"at", "reply", "mention_user", "mention_everyone"})
"""指令前可能混入的称呼类消息段：OneBot 的 at/reply、QQ 官方的 mention_*。

QQ 群 @ 机器人再接指令时，适配器会把 mention_user 插到消息开头；
命令解析只看首个消息段，非文本段开头会整体失配。
"""


def _starts_with_command(message: Message) -> bool:
    """跳过开头的称呼段后，判断剩余文本是否以本插件指令开头。"""
    for seg in message:
        if seg.type in _ADDRESS_SEGMENTS:
            continue
        if seg.type != "text":
            return False
        text = str(seg.data.get("text", "")).lstrip()
        if text.startswith("/"):
            text = text[1:].lstrip()
        return text.startswith(_COMMAND_WORDS)
    return False


def _strip_command_prefix(message: Message) -> None:
    """剥掉指令前的称呼段，使 NoneBot 命令解析能看到指令文本。

    OneBot 群聊 @ 机器人再接指令时消息以 at 段开头，QQ 官方群则是
    mention_user 段；命令解析只看首个消息段，会整体失配。仅当剩余文本
    是本插件指令时才剥离，不影响其他消息与其他插件。
    """
    if not message or message[0].type == "text":
        return
    if not _starts_with_command(message):
        return
    while message and message[0].type in _ADDRESS_SEGMENTS:
        message.pop(0)


@event_preprocessor
async def _pre_handle_message(event: Event) -> None:
    if event.get_type() != "message":
        return
    _strip_command_prefix(event.get_message())


def _ensure_command_prefixes() -> None:
    """兜底注册带/不带 "/" 的指令前缀，不依赖部署环境的 COMMAND_START。

    命令解析的前缀表由全局 COMMAND_START 配置生成，线上漏配 "/"（或反之）
    会导致「/本周突变」整条消息无响应；这里为本插件的指令补齐两种写法。
    """
    for word in _COMMAND_WORDS:
        for start in ("", "/"):
            prefix = f"{start}{word}"
            if prefix not in TrieRule.prefix:
                TrieRule.add_prefix(prefix, TRIE_VALUE(start, (word,)))


mutation_cmd = on_command("突变", aliases={"本周突变", "tb"}, priority=10, block=True)
next_cmd = on_command("下周突变", aliases={"tbx"}, priority=10, block=True)
search_cmd = on_command("突变查询", priority=10, block=True)
factor_cmd = on_command("因子查询", aliases={"因子"}, priority=10, block=True)
brutal_cmd = on_command(
    "随机残酷", aliases={"残酷随机", "残酷+", "sjck"}, priority=10, block=True
)
sub_cmd = on_command("订阅突变", priority=10, block=True)
unsub_cmd = on_command("退订突变", priority=10, block=True)
test_push_cmd = on_command("测试推送", priority=10, block=True)
help_cmd = on_command("帮助", aliases={"help", "usage"}, priority=10, block=True)
_ensure_command_prefixes()

_PUSH_POLL_SECONDS = 10
"""定时推送的检查间隔（秒），也是推送时刻的触发精度。"""

_store: PushStore | None = None


def _push_store() -> PushStore:
    """订阅存储（懒加载，避免插件加载期创建数据目录）。"""
    global _store
    if _store is None:
        _store = PushStore(default_store_path())
    return _store


def _schedule_text() -> str:
    """当前配置的推送时刻展示文本。"""
    return schedule_text(
        config.tubian_push_weekday, config.tubian_push_hour, config.tubian_push_minute
    )


async def _push_weekly(*, force: bool = False) -> PushSummary:
    """向订阅目标推送本周突变。

    force=False（定时触发）：只发本周尚未送达的目标，成功后登记送达时间，
    供主动消息无权限/错过推送时刻后的补发兜底使用。
    force=True（「测试推送」）：全量发送且不改动送达记录，可反复测试。
    """
    store = _push_store()
    week = week_monday(datetime.now())
    targets = store.targets() if force else store.pending_targets(week)
    if not targets:
        return PushSummary(sent=0, attempted=0, errors=[], succeeded=())
    try:
        mutation = _book().at(local_today())
    except MutationDataError:
        logger.exception("推送本周突变失败：突变数据加载失败")
        return PushSummary(
            sent=0, attempted=len(targets), errors=["突变数据加载失败"], succeeded=()
        )
    summary = await push_all(
        targets, lambda bot: _build_card(bot, mutation, "本周突变")
    )
    if not force:
        for target in summary.succeeded:
            store.mark_pushed(target, week)
    for error in summary.errors:
        logger.warning(f"推送失败：{error}")
    logger.info(f"本周突变推送完成：成功 {summary.sent}/{summary.attempted} 条")
    return summary


async def _weekly_push_loop() -> None:
    """定时推送主循环：到点推送本周突变，进程内每周至多一次。"""
    last: date | None = None
    while True:
        now = datetime.now()
        if push_due(
            now,
            last,
            config.tubian_push_weekday,
            config.tubian_push_hour,
            config.tubian_push_minute,
        ):
            last = week_monday(now)
            await _push_weekly()
        await asyncio.sleep(_PUSH_POLL_SECONDS)


@get_driver().on_startup
async def _start_weekly_push() -> None:
    asyncio.create_task(_weekly_push_loop())


lazy_push = on_message(priority=99, block=False)
"""补发兜底：priority 高于全部指令，指令消息被 block 后才轮到它。"""


async def _maybe_lazy_push(bot: Bot, event: Event, now: datetime) -> bool:
    """按需向已订阅会话补发本周突变，返回是否已补发。

    主动消息无权限或错过推送时刻时，随会话的下一条消息补发：被动回复
    不需要「主动消息」权限；内容按补发时的本周突变现算。本插件指令在
    priority 10 已 block，不会把卡片夹带进指令回复。
    """
    if event.get_type() != "message":
        return False
    target = extract_target(bot, event)
    if target is None:
        return False
    store = _push_store()
    if not store.subscribed(target):
        return False
    if not lazy_due(
        now,
        store.last_pushed(target),
        config.tubian_push_weekday,
        config.tubian_push_hour,
        config.tubian_push_minute,
    ):
        return False
    try:
        mutation = _book().at(local_today())
    except MutationDataError:
        return False
    card = _build_card(bot, mutation, "本周突变", note="（定时推送补发）")
    try:
        await bot.send(event, card)
    except Exception as e:
        logger.warning(f"补发本周突变失败（{target.scene} {target.target_id}）：{e}")
        return False
    store.mark_pushed(target, week_monday(now))
    logger.info(f"已向 {target.scene} {target.target_id} 补发本周突变")
    return True


@lazy_push.handle()
async def handle_lazy_push(bot: Bot, event: Event) -> None:
    await _maybe_lazy_push(bot, event, datetime.now())


async def _finish_query(
    matcher: type[Matcher], bot: Bot, query: _Query
) -> None:
    """回复查询结果：提示文本，或突变卡片。"""
    if query.reply is not None:
        await matcher.finish(query.reply)
    assert query.mutation is not None
    card = _build_card(bot, query.mutation, query.label, note=query.note)
    await matcher.finish(card)


@mutation_cmd.handle()
async def handle_mutation(bot: Bot, arg: Message = CommandArg()) -> None:
    text = arg.extract_plain_text().strip()
    try:
        book = _book()
    except MutationDataError as e:
        await mutation_cmd.finish(f"突变数据加载失败：{e}")
    await _finish_query(mutation_cmd, bot, _resolve_query(book, text, local_today()))


@next_cmd.handle()
async def handle_next(bot: Bot) -> None:
    try:
        book = _book()
    except MutationDataError as e:
        await next_cmd.finish(f"突变数据加载失败：{e}")
    mutation = book.at(local_today() + timedelta(days=7))
    await _finish_query(next_cmd, bot, _Query(mutation, "下周突变"))


@search_cmd.handle()
async def handle_search(bot: Bot, arg: Message = CommandArg()) -> None:
    text = arg.extract_plain_text().strip()
    try:
        book = _book()
    except MutationDataError as e:
        await search_cmd.finish(f"突变数据加载失败：{e}")
    await _finish_query(
        search_cmd, bot, _resolve_search_query(book, text, local_today())
    )


@brutal_cmd.handle()
async def handle_brutal(bot: Bot, arg: Message = CommandArg()) -> None:
    text = arg.extract_plain_text().strip()
    if text in _HELP_WORDS:
        await brutal_cmd.finish(HELP_TEXT)
    level = parse_brutal_level(text) if text else random.randint(1, MAX_LEVEL)
    if level is None:
        await brutal_cmd.finish(BRUTAL_USAGE_HINT)
    combo = random_brutal(level)
    try:
        card = _brutal_card(bot, combo)
    except MutationDataError as e:
        await brutal_cmd.finish(f"突变数据加载失败：{e}")
    await brutal_cmd.finish(card)


@factor_cmd.handle()
async def handle_factor(bot: Bot, arg: Message = CommandArg()) -> None:
    text = arg.extract_plain_text().strip()
    try:
        book = _factor_book()
    except FactorDataError as e:
        await factor_cmd.finish(f"因子数据加载失败：{e}")
    factor, reply = _resolve_factor_query(book, text)
    if reply is not None:
        await factor_cmd.finish(reply)
    assert factor is not None
    await factor_cmd.finish(_effect_card(bot, factor))


@sub_cmd.handle()
async def handle_subscribe(bot: Bot, event: Event) -> None:
    """「订阅突变」把当前会话加入每周一 08:00 的推送列表。"""
    target = extract_target(bot, event)
    if target is None:
        await sub_cmd.finish("当前会话暂不支持推送订阅（仅支持群聊与单聊/私聊）")
    if _push_store().add(target):
        await sub_cmd.finish("已订阅本周突变推送：每周一 08:00 自动发送本周突变")
    await sub_cmd.finish("本会话已订阅本周突变推送，无需重复订阅")


@unsub_cmd.handle()
async def handle_unsubscribe(bot: Bot, event: Event) -> None:
    """「退订突变」把当前会话移出推送列表。"""
    target = extract_target(bot, event)
    if target is None:
        await unsub_cmd.finish("当前会话暂不支持推送订阅（仅支持群聊与单聊/私聊）")
    if _push_store().remove(target):
        await unsub_cmd.finish("已退订本周突变推送")
    await unsub_cmd.finish("本会话未订阅本周突变推送")


@test_push_cmd.handle()
async def handle_test_push() -> None:
    """「测试推送」立即全量推送一次（不改动定时送达记录），用于线上验证。"""
    summary = await _push_weekly(force=True)
    if summary.attempted == 0:
        await test_push_cmd.finish(
            "当前无订阅会话，请先在目标会话发送「订阅突变」再测试\n"
            f"定时配置：{_schedule_text()}"
        )
    lines = [
        f"本周突变推送已执行：成功 {summary.sent}/{summary.attempted} 条",
        f"定时配置：{_schedule_text()}",
    ]
    if summary.errors:
        lines.append("失败原因：")
        lines += summary.errors[:3]
        if len(summary.errors) > 3:
            lines.append("……")
    await test_push_cmd.finish("\n".join(lines))


@help_cmd.handle()
async def handle_help() -> None:
    """「帮助」返回插件全部功能。"""
    await help_cmd.finish(HELP_TEXT)
