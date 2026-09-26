"""运行时配置持有者

服务层模块需要在插件配置加载后读取开关（如群操作开关、模型组名）。
为避免 plugin.py 与服务层循环导入，通过此模块单向绑定配置实例。
"""

from typing import Optional

_config: Optional[object] = None


def bind_config(config: object) -> None:
    """绑定插件配置实例（在插件初始化时调用）"""
    global _config
    _config = config


def get_config():
    """获取插件配置实例，未绑定时返回 None"""
    return _config
