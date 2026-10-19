"""插件配置。"""

from pathlib import Path

from nonebot import get_plugin_config
from pydantic import BaseModel, Field


class Config(BaseModel):
    """插件配置。"""

    tubian_data_dir: Path = Path(__file__).parent / "突变列表数据"
    """突变数据目录，内含「突变列表数据.xlsx」与「因子图标」文件夹。

    默认为本模块下的「突变列表数据」文件夹，可通过环境变量 TUBIAN_DATA_DIR 覆盖。
    """

    tubian_push_weekday: int = Field(0, ge=0, le=6)
    """推送星期：0=周一 … 6=周日。可通过环境变量 TUBIAN_PUSH_WEEKDAY 覆盖。

    线上定时测试时可临时改为当天星期值。
    """

    tubian_push_hour: int = Field(8, ge=0, le=23)
    """推送时刻（小时，0～23）。可通过环境变量 TUBIAN_PUSH_HOUR 覆盖。"""

    tubian_push_minute: int = Field(0, ge=0, le=59)
    """推送时刻（分钟，0～59）。可通过环境变量 TUBIAN_PUSH_MINUTE 覆盖。"""


config = get_plugin_config(Config)
