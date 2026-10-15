"""nonebot-plugin-tubian —— 星际争霸每周突变查询（非官方机器人版）。

根据「突变列表数据.xlsx」与当前时间自动选中本周突变并返回，
支持查询下周突变、按序号或名称查询指定突变。

面向 NapCat / OneBot 等非官方机器人端：卡片为一条内联图文消息，
行为与第一版一致。
"""

from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

from nonebot import on_command
from nonebot.adapters import Bot, Event, Message
from nonebot.adapters import MessageSegment as BaseMessageSegment
from nonebot.message import event_preprocessor
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata
from nonebot.rule import TRIE_VALUE, TrieRule

from .config import Config, config
from .data import (
    Mutation,
    MutationBook,
    MutationDataError,
    fmt_date,
    local_today,
    normalize_query,
)

SEARCH_LIMIT = 10

__plugin_meta__ = PluginMetadata(
    name="星际争霸每周突变",
    description="根据突变列表 Excel 与当前时间，自动选中本周突变并返回",
    usage=(
        "突变 — 查询本周突变（按当前时间自动选中）\n"
        "下周突变 — 查询下周突变\n"
        "突变 <序号> — 按轮换序号查询，如：突变 128\n"
        "突变 <名称> — 按突变名称搜索，如：突变 恐惧神庙"
    ),
    type="application",
    config=Config,
)

HELP_TEXT = (
    "星际争霸每周突变查询：\n"
    "突变 — 查询本周突变（按当前时间自动选中）\n"
    "下周突变 — 查询下周突变\n"
    "突变 <序号> — 按轮换序号查询，如：突变 128\n"
    "突变 <名称> — 按突变名称搜索，如：突变 恐惧神庙"
)

# 与下方 on_command 注册保持一致
_COMMAND_WORDS = ("突变", "下周突变", "本周突变", "tbx", "tb")

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


def _image_segment(bot: Bot, path: Path) -> BaseMessageSegment | None:
    """按适配器构造图片消息段，不支持或读图失败时返回 None。

    图片内容以 bytes 传入：OneBot 侧会编码为 base64 发送。
    NapCat 等实现常与机器人不在同一文件系统（如各自 Docker 部署），
    若传本地路径 / file://，对方读不到文件会在 copyfile 时报 ENOENT。
    """
    adapter_name = bot.adapter.get_name()
    if "OneBot V11" not in adapter_name and "QQ" not in adapter_name:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if "OneBot V11" in adapter_name:
        from nonebot.adapters.onebot.v11 import MessageSegment

        return MessageSegment.image(data)
    from nonebot.adapters.qq import MessageSegment

    return MessageSegment.file_image(data, file_name=path.name)


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
    footer = f"右侧数字：{mutation.number}" if mutation.number else ""
    if note:
        footer = f"{footer}\n{note}" if footer else note
    return header, footer


def _build_card(
    bot: Bot, event: Event, mutation: Mutation, label: str, note: str | None = None
) -> Message:
    """构造一条突变信息卡片（文字 + 因子图标内联，单条消息）。"""
    msg_cls = event.get_message().__class__
    header, footer = _card_text(mutation, label, note)
    message = msg_cls(header)
    if mutation.factors:
        message += msg_cls("因子：\n")
        for factor in mutation.factors:
            icon = _book().icon_path(factor)
            segment = _image_segment(bot, icon) if icon else None
            if segment is not None:
                message += segment
            message += msg_cls(f"{factor}\n")
    if footer:
        message += msg_cls(footer)
    return message


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
    """把指令参数解析为卡片参数或直接回复文本。"""
    if not text or text in _CURRENT_WORDS:
        return _Query(book.at(today), "本周突变")
    if text in _NEXT_WORDS:
        return _Query(book.at(today + timedelta(days=7)), "下周突变")
    if text in _HELP_WORDS:
        return _Query(None, reply=HELP_TEXT)
    if text.isdigit():
        return _resolve_number(book, text)
    return _resolve_search(book, text, today)


def _starts_with_command(message: Message) -> bool:
    """跳过开头的 at/reply 段后，判断剩余文本是否以本插件指令开头。"""
    for seg in message:
        if seg.type in {"at", "reply"}:
            continue
        if seg.type != "text":
            return False
        text = str(seg.data.get("text", "")).lstrip()
        if text.startswith("/"):
            text = text[1:].lstrip()
        return text.startswith(_COMMAND_WORDS)
    return False


def _strip_command_prefix(message: Message) -> None:
    """剥掉指令前的 at/reply 段，使 NoneBot 命令解析能看到指令文本。

    OneBot 群聊 @ 机器人再接指令时消息以 at 段开头，
    命令解析只看首个消息段，会整体失配；仅当剩余文本是本插件指令时才剥离，
    不影响其他消息与其他插件。
    """
    if not message or message[0].type == "text":
        return
    if not _starts_with_command(message):
        return
    while message and message[0].type in {"at", "reply"}:
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
_ensure_command_prefixes()


@mutation_cmd.handle()
async def handle_mutation(bot: Bot, event: Event, arg: Message = CommandArg()) -> None:
    text = arg.extract_plain_text().strip()
    try:
        book = _book()
    except MutationDataError as e:
        await mutation_cmd.finish(f"突变数据加载失败：{e}")

    query = _resolve_query(book, text, local_today())
    if query.reply is not None:
        await mutation_cmd.finish(query.reply)
    assert query.mutation is not None
    await mutation_cmd.finish(
        _build_card(bot, event, query.mutation, query.label, note=query.note)
    )


@next_cmd.handle()
async def handle_next(bot: Bot, event: Event) -> None:
    try:
        book = _book()
    except MutationDataError as e:
        await next_cmd.finish(f"突变数据加载失败：{e}")
    mutation = book.at(local_today() + timedelta(days=7))
    await next_cmd.finish(_build_card(bot, event, mutation, "下周突变"))
