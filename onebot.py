"""OneBot V11 底层封装

统一获取 Bot 实例并封装群聊/私聊消息发送与群管理操作。
所有方法在适配器不可用或调用失败时安全降级（返回 False / None），不抛出异常。
"""

import base64
import os
from typing import List, Optional

from .compat import logger


def get_onebot_bot():
    """获取 OneBot V11 Bot 实例，不可用时抛出异常"""
    from nekro_agent.adapters.onebot_v11.core.bot import get_bot

    return get_bot()


def has_onebot_bot() -> bool:
    """当前是否存在可用的 OneBot V11 Bot 连接"""
    try:
        get_onebot_bot()
        return True
    except Exception:
        return False


def extract_chat_type_and_id(chat_key: str) -> tuple:
    """从 chat_key（如 onebot_v11-group_123456）解析 (类型, ID)

    类型为 "group" / "private"，无法解析时返回 ("", "")
    """
    for part in chat_key.split("-")[1:]:
        if part.startswith("group_"):
            return "group", part[len("group_"):]
        if part.startswith("private_"):
            return "private", part[len("private_"):]
    return "", ""


async def send_group_text(group_id: str, text: str, at_user_id: Optional[str] = None) -> bool:
    """发送群聊文本消息，可选先 @ 某人"""
    try:
        bot = get_onebot_bot()
    except Exception as e:
        logger.error(f"[狼人杀] OneBot Bot 不可用，无法发送群消息: {e}")
        return False

    message: List[dict] = []
    if at_user_id:
        message.append({"type": "at", "data": {"qq": str(at_user_id)}})
    message.append({"type": "text", "data": {"text": text}})
    try:
        await bot.call_api("send_group_msg", group_id=int(group_id), message=message)
        return True
    except Exception as e:
        logger.error(f"[狼人杀] 发送群消息失败（群 {group_id}）: {e}")
        return False


async def send_private_text(user_id: str, text: str) -> bool:
    """发送私聊文本消息"""
    try:
        bot = get_onebot_bot()
    except Exception as e:
        logger.error(f"[狼人杀] OneBot Bot 不可用，无法发送私聊消息: {e}")
        return False

    try:
        await bot.call_api(
            "send_private_msg",
            user_id=int(user_id),
            message=[{"type": "text", "data": {"text": text}}],
        )
        return True
    except Exception as e:
        logger.warning(f"[狼人杀] 发送私聊消息给 {user_id} 失败: {e}")
        return False


async def send_group_image(group_id: str, image_path: str) -> bool:
    """发送群聊图片（本地文件以 base64 编码发送，避免协议端文件路径不通）"""
    try:
        bot = get_onebot_bot()
    except Exception as e:
        logger.error(f"[狼人杀] OneBot Bot 不可用，无法发送图片: {e}")
        return False

    try:
        with open(image_path, "rb") as f:
            data = f.read()
        encoded = base64.b64encode(data).decode("ascii")
        await bot.call_api(
            "send_group_msg",
            group_id=int(group_id),
            message=[{"type": "image", "data": {"file": f"base64://{encoded}"}}],
        )
        return True
    except Exception as e:
        logger.error(f"[狼人杀] 发送图片失败（群 {group_id}）: {e}")
        return False


async def get_group_member_card(group_id: str, user_id: str) -> str:
    """查询群成员群名片（无名片时返回昵称），失败返回空串"""
    try:
        bot = get_onebot_bot()
        info = await bot.call_api(
            "get_group_member_info", group_id=int(group_id), user_id=int(user_id)
        )
        return str(info.get("card") or info.get("nickname") or "")
    except Exception:
        return ""


async def get_user_nickname(user_id: str) -> str:
    """查询用户昵称，失败返回空串"""
    try:
        bot = get_onebot_bot()
        info = await bot.call_api("get_stranger_info", user_id=int(user_id))
        return str(info.get("nickname") or "")
    except Exception:
        return ""


def _group_ops_enabled() -> bool:
    """群管理操作（禁言/群名片/临时管理员）总开关"""
    from .runtime_cfg import get_config

    cfg = get_config()
    return bool(cfg and getattr(cfg, "enable_group_ops", True))


async def _call_group_admin_api(group_id: str, action: str, **params) -> bool:
    """调用群管理 API 的统一包装：总开关关闭或失败时返回 False

    参数中的数字字符串会在此处转换为 int（AI 玩家 ID 等非数字值保持原样，
    由协议端报错后统一吞掉，不影响游戏流程）。
    """
    if not _group_ops_enabled():
        return False
    try:
        bot = get_onebot_bot()
        cleaned = {}
        for key, value in params.items():
            if isinstance(value, str) and value.lstrip("-").isdigit():
                cleaned[key] = int(value)
            else:
                cleaned[key] = value
        await bot.call_api(action, group_id=int(group_id), **cleaned)
        return True
    except Exception as e:
        logger.warning(f"[狼人杀] 群管理操作 {action} 失败（群 {group_id}）: {e}")
        return False


async def set_group_ban(group_id: str, user_id: str, duration: int) -> bool:
    """禁言/解禁群成员（duration=0 解禁）"""
    return await _call_group_admin_api(
        group_id, "set_group_ban", user_id=str(user_id), duration=int(duration)
    )


async def set_group_whole_ban(group_id: str, enable: bool) -> bool:
    """设置全员禁言"""
    return await _call_group_admin_api(group_id, "set_group_whole_ban", enable=enable)


async def set_group_admin(group_id: str, user_id: str, enable: bool) -> bool:
    """设置/取消群管理员"""
    return await _call_group_admin_api(
        group_id, "set_group_admin", user_id=str(user_id), enable=enable
    )


async def set_group_card(group_id: str, user_id: str, card: str) -> bool:
    """设置群名片"""
    return await _call_group_admin_api(
        group_id, "set_group_card", user_id=str(user_id), card=card
    )


def get_font_resource_path() -> str:
    """获取插件自带字体文件路径"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "draw", "resource", "DouyinSansBold.otf")
