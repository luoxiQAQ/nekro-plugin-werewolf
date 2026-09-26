"""兼容层 - 为移植自 AstrBot 的代码提供统一的日志器"""

import logging

try:
    from nekro_agent.core.logger import get_sub_logger

    logger = get_sub_logger("plugin.werewolf")
except Exception:  # pragma: no cover - 本地测试或日志系统不可用时降级
    logger = logging.getLogger("nekro_plugin_werewolf")

__all__ = ["logger"]
