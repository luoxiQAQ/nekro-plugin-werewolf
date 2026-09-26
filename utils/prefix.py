"""命令前缀工具

Nekro 版狼人杀使用裸文本命令（无需前缀），提示文案中的命令直接显示原名。
保留前缀机制以便提示文案统一走 cmd()。
"""

# 全局命令前缀，由插件初始化时设置（Nekro 版默认为空串）
_command_prefix: str = ""


def set_command_prefix(prefix: str) -> None:
    """设置命令前缀"""
    global _command_prefix
    _command_prefix = prefix


def get_command_prefix() -> str:
    """获取命令前缀"""
    return _command_prefix


def cmd(command: str) -> str:
    """返回带前缀的命令字符串

    Nekro 版默认无前缀: cmd("创建房间") -> "创建房间"
    """
    return f"{_command_prefix}{command}"
