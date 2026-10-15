"""插件配置。"""

from pathlib import Path

from nonebot import get_plugin_config
from pydantic import BaseModel


class Config(BaseModel):
    """插件配置。"""

    tubian_data_dir: Path = Path(__file__).parent / "突变列表数据"
    """突变数据目录，内含「突变列表数据.xlsx」与「因子图标」文件夹。

    默认为本模块下的「突变列表数据」文件夹，可通过环境变量 TUBIAN_DATA_DIR 覆盖。
    """


config = get_plugin_config(Config)
