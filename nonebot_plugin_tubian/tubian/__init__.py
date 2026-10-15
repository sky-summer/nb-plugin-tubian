"""nonebot-plugin-tubian —— 星际争霸每周突变查询。

根据「突变列表数据.xlsx」与当前时间自动选中本周突变并返回，
支持查询下周突变、按序号或名称查询指定突变。
"""

from datetime import timedelta
from functools import lru_cache
from pathlib import Path

from nonebot import on_command
from nonebot.adapters import Bot, Event, Message
from nonebot.adapters import MessageSegment as BaseMessageSegment
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

from .config import Config, config
from .data import Mutation, MutationBook, MutationDataError, fmt_date, local_today

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


def _build_card(
    bot: Bot, event: Event, book: MutationBook, mutation: Mutation, label: str
) -> Message:
    """构造一条突变信息卡片（文字 + 因子图标）。"""
    msg_cls = event.get_message().__class__
    text = (
        f"【{label}】{mutation.name}\n"
        f"地图：{mutation.map_name} · 序号 {mutation.mid}\n"
        f"日期：{fmt_date(mutation.start_date)} ～ {mutation.end_date:%Y/%m/%d}\n"
    )
    today = local_today()
    if mutation.contains(today):
        text += f"剩余时间：{mutation.remaining_days(today)} 天\n"
    message = msg_cls(text)
    if mutation.factors:
        message += msg_cls("因子：\n")
        for factor in mutation.factors:
            icon = book.icon_path(factor)
            segment = _image_segment(bot, icon) if icon else None
            if segment is not None:
                message += segment
            message += msg_cls(f"{factor}\n")
    if mutation.number:
        message += msg_cls(f"右侧数字：{mutation.number}")
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


mutation_cmd = on_command("突变", aliases={"本周突变", "tb"}, priority=10, block=True)
next_cmd = on_command("下周突变", aliases={"tbx"}, priority=10, block=True)


@mutation_cmd.handle()
async def handle_mutation(bot: Bot, event: Event, arg: Message = CommandArg()) -> None:
    text = arg.extract_plain_text().strip()
    today = local_today()
    try:
        book = _book()
    except MutationDataError as e:
        await mutation_cmd.finish(f"突变数据加载失败：{e}")

    if not text or text in {"本周", "这周", "当前", "now"}:
        mutation = book.at(today)
        label = "本周突变"
    elif text in {"下周", "下一周", "next"}:
        mutation = book.at(today + timedelta(days=7))
        label = "下周突变"
    elif text in {"帮助", "help", "usage"}:
        await mutation_cmd.finish(HELP_TEXT)
    elif text.isdigit():
        mutation = book.get(int(text))
        if mutation is None:
            await mutation_cmd.finish(
                f"未找到序号为 {text} 的突变（范围 1～{book.count}）"
            )
        label = f"突变 #{mutation.mid}"
    else:
        found = book.search(text)
        if not found:
            await mutation_cmd.finish(f"未找到名称包含「{text}」的突变")
        if len(found) > 1:
            await mutation_cmd.finish(_search_summary(found, text))
        mutation = found[0]
        label = f"突变 #{mutation.mid}"

    await mutation_cmd.finish(_build_card(bot, event, book, mutation, label))


@next_cmd.handle()
async def handle_next(bot: Bot, event: Event) -> None:
    try:
        book = _book()
    except MutationDataError as e:
        await next_cmd.finish(f"突变数据加载失败：{e}")
    mutation = book.at(local_today() + timedelta(days=7))
    await next_cmd.finish(_build_card(bot, event, book, mutation, "下周突变"))
