"""定时推送本周突变：订阅目标管理与推送发送。

订阅目标由「订阅突变 / 退订突变」指令维护，持久化为 JSON
（默认存放在 localstore 的插件数据目录）。推送时机见 {ref}`push_due`：
配置的星期与时刻（默认周一 08:00）起、进程内至多一次，当天启动会补推。

QQ 官方接口对「主动消息」（不回复用户消息）有权限要求，未开通时返回
40034105。此时改走 {ref}`lazy_due` 补发：随会话的下一条消息被动回复
本周突变卡片（被动回复无需主动消息权限），见 README「定时推送」。
"""

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable, NamedTuple

from nonebot import get_bot, logger, require

require("nonebot_plugin_localstore")
from nonebot_plugin_localstore import get_data_file

if TYPE_CHECKING:
    from nonebot.adapters import Bot, Event, Message

WEEKDAY_NAMES = "一二三四五六日"
"""星期中文名，索引对应 datetime.weekday。"""

STORE_FILE = "push_targets.json"
"""订阅文件名。"""

SCENE_GROUP = "group"
SCENE_C2C = "c2c"
SCENE_PRIVATE = "private"
"""支持的会话类型：群、QQ 单聊、私聊。"""


@dataclass(frozen=True)
class PushTarget:
    """一个推送目标：某机器人的某个会话。"""

    adapter: str
    """适配器名，如 ``QQ`` / ``OneBot V11``。"""

    bot_id: str
    """机器人账号（Bot.self_id），推送时据此取在线机器人。"""

    scene: str
    """会话类型：``group`` / ``c2c`` / ``private``。"""

    target_id: str
    """会话 id：群 group_openid / QQ 单聊 openid / 私聊 user_id。"""

    @property
    def key(self) -> tuple[str, str, str, str]:
        """订阅去重键。"""
        return (self.adapter, self.bot_id, self.scene, self.target_id)


class PushSummary(NamedTuple):
    """一次推送的结果。"""

    sent: int
    """成功条数。"""

    attempted: int
    """尝试条数。"""

    errors: list[str]
    """失败原因，已转成可读提示。"""

    succeeded: tuple[PushTarget, ...]
    """成功送达的目标，供登记送达时间使用。"""


class PushStore:
    """订阅目标的 JSON 持久化。文件不存在视为空订阅，损坏时按空订阅处理。

    同时记录各目标最近一次送达的周一日期（``last_pushed``），用于
    「本周至多推送一次」与主动消息失败后的补发兜底。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._targets: dict[tuple[str, str, str, str], PushTarget] = {}
        self._pushed: dict[tuple[str, str, str, str], date] = {}

    @property
    def path(self) -> Path:
        """订阅文件路径。"""
        return self._path

    def _load(self) -> None:
        """每次操作前从磁盘重读，保持多入口下的一致性。"""
        self._targets.clear()
        self._pushed.clear()
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning(f"读取推送订阅失败，按空订阅处理：{e}")
            return
        if not isinstance(raw, list):
            return
        for item in raw:
            if not isinstance(item, dict):
                continue
            identity = {
                key: item[key]
                for key in ("adapter", "bot_id", "scene", "target_id")
                if key in item
            }
            try:
                target = PushTarget(**identity)
            except TypeError:
                continue
            self._targets[target.key] = target
            raw_pushed = item.get("last_pushed")
            if isinstance(raw_pushed, str):
                try:
                    self._pushed[target.key] = date.fromisoformat(raw_pushed)
                except ValueError:
                    continue

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = []
        for key, target in self._targets.items():
            item = asdict(target)
            if pushed := self._pushed.get(key):
                item["last_pushed"] = pushed.isoformat()
            data.append(item)
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def add(self, target: PushTarget) -> bool:
        """加入订阅，返回是否为新增。"""
        self._load()
        if target.key in self._targets:
            return False
        self._targets[target.key] = target
        self._save()
        return True

    def remove(self, target: PushTarget) -> bool:
        """移除订阅，返回此前是否存在。"""
        self._load()
        if target.key not in self._targets:
            return False
        del self._targets[target.key]
        self._pushed.pop(target.key, None)
        self._save()
        return True

    def targets(self) -> tuple[PushTarget, ...]:
        """全部订阅目标。"""
        self._load()
        return tuple(self._targets.values())

    def subscribed(self, target: PushTarget) -> bool:
        """目标是否在订阅列表中。"""
        self._load()
        return target.key in self._targets

    def last_pushed(self, target: PushTarget) -> date | None:
        """目标最近一次送达的周一日期，从未送达过返回 None。"""
        self._load()
        return self._pushed.get(target.key)

    def mark_pushed(self, target: PushTarget, week: date) -> None:
        """登记目标在 ``week`` 所在一周已送达。"""
        self._load()
        self._pushed[target.key] = week
        self._save()

    def pending_targets(self, week: date) -> tuple[PushTarget, ...]:
        """尚未在 ``week`` 所在一周送达过的目标。"""
        self._load()
        return tuple(
            target
            for target in self._targets.values()
            if self._pushed.get(target.key, date.min) < week
        )


def default_store_path() -> Path:
    """订阅文件默认路径：localstore 的插件数据目录。"""
    return get_data_file("tubian", STORE_FILE)


def extract_target(bot: "Bot", event: "Event") -> PushTarget | None:
    """从消息事件提取推送目标；不支持的会话类型返回 None。

    仅支持群聊与单聊/私聊：QQ 频道等场景的主动消息受限，暂不开放订阅。
    """
    adapter = bot.adapter.get_name()
    if "OneBot V11" in adapter:
        from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent

        if isinstance(event, GroupMessageEvent):
            scene, target_id = SCENE_GROUP, str(event.group_id)
        elif isinstance(event, PrivateMessageEvent):
            scene, target_id = SCENE_PRIVATE, str(event.user_id)
        else:
            return None
    elif "QQ" in adapter:
        from nonebot.adapters.qq import C2CMessageCreateEvent, GroupMessageCreateEvent

        if isinstance(event, GroupMessageCreateEvent):
            scene, target_id = SCENE_GROUP, event.group_openid
        elif isinstance(event, C2CMessageCreateEvent):
            scene, target_id = SCENE_C2C, event.author.id
        else:
            return None
    else:
        return None
    return PushTarget(
        adapter=adapter, bot_id=bot.self_id, scene=scene, target_id=target_id
    )


async def send_target(bot: "Bot", target: PushTarget, message: "Message") -> None:
    """按适配器向订阅目标主动发送一条消息。"""
    if "OneBot V11" in target.adapter:
        if target.scene == SCENE_GROUP:
            await bot.send_group_msg(group_id=int(target.target_id), message=message)
        else:
            await bot.send_private_msg(user_id=int(target.target_id), message=message)
        return
    if target.scene == SCENE_GROUP:
        await bot.send_to_group(group_openid=target.target_id, message=message)
    else:
        await bot.send_to_c2c(openid=target.target_id, message=message)


def describe_send_error(error: Exception) -> str:
    """把发送异常转成可读的失败原因。"""
    code = getattr(error, "code", None)
    if code == 40034105:
        return (
            "无「主动消息」权限（code 40034105）：QQ 官方限制机器人主动发送群消息，"
            "请到 QQ 开放平台为机器人开通「主动消息」能力；"
            "开通前推送会随会话的下一条消息补发"
        )
    return str(error)


async def push_all(
    targets: Iterable[PushTarget],
    build_message: Callable[["Bot"], "Message"],
) -> PushSummary:
    """向全部订阅目标推送，汇总结果。

    单个目标失败（机器人不在线、发送出错）只记入 ``errors``，不影响其余目标。
    """
    items = tuple(targets)
    sent = 0
    errors: list[str] = []
    succeeded: list[PushTarget] = []
    for target in items:
        try:
            bot = get_bot(target.bot_id)
        except KeyError:
            errors.append(f"{target.scene} {target.target_id}：机器人 {target.bot_id} 不在线")
            continue
        try:
            await send_target(bot, target, build_message(bot))
        except Exception as e:
            errors.append(f"{target.scene} {target.target_id}：{describe_send_error(e)}")
            continue
        sent += 1
        succeeded.append(target)
    return PushSummary(
        sent=sent, attempted=len(items), errors=errors, succeeded=tuple(succeeded)
    )


def push_due(now: datetime, last: date | None, weekday: int, hour: int, minute: int) -> bool:
    """判断此刻是否应当推送本周突变。

    规则：推送日（``weekday``，0=周一）``hour:minute`` 起、且本周（以周一计）
    尚未推送过。``last`` 为本进程最近一次推送的周一日期；进程在推送日
    当天该时刻之后重启时会补推一次，其余时间不补。

    时刻可配置，便于线上定时测试（如把推送日/时刻临时改为当天的几分钟后）。
    """
    if now.weekday() != weekday:
        return False
    if (now.hour, now.minute) < (hour, minute):
        return False
    monday = (now - timedelta(days=now.weekday())).date()
    return last is None or last < monday


def lazy_due(
    now: datetime, last_pushed: date | None, weekday: int, hour: int, minute: int
) -> bool:
    """判断是否应向某订阅会话补发本周突变。

    补发兜底：主动消息无权限、或机器人在推送时刻未开机时，定时推送送不达；
    此后会话里第一条消息到来时，随该消息被动回复本周突变卡片。规则为
    「本周的推送时刻已过、且本会话本周尚未送达」，内容按补发时的本周突变
    现算，不会过期。
    """
    week = week_monday(now)
    if last_pushed is not None and last_pushed >= week:
        return False
    scheduled = datetime.combine(
        week + timedelta(days=weekday), datetime.min.time()
    ) + timedelta(hours=hour, minutes=minute)
    return now >= scheduled


def week_monday(now: datetime) -> date:
    """now 所在一周的周一日期。"""
    return (now - timedelta(days=now.weekday())).date()


def schedule_text(weekday: int, hour: int, minute: int) -> str:
    """推送时刻的展示文本，如「每周一 08:00」。"""
    return f"每周{WEEKDAY_NAMES[weekday]} {hour:02d}:{minute:02d}"
