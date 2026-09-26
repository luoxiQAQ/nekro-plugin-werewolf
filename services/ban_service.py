"""禁言与群名片管理服务 - Nekro/OneBot V11 适配版

接口与 AstrBot 版保持一致，底层改为 OneBot 直连。
Bot 权限不足时操作失败会被吞掉并记录日志，游戏流程不受影响。
"""

from typing import TYPE_CHECKING

from ..compat import logger
from ..onebot import set_group_ban, set_group_whole_ban, set_group_admin, set_group_card

if TYPE_CHECKING:
    from ..models import GameRoom


class BanService:
    """禁言管理服务"""

    @staticmethod
    async def ban_player(room: "GameRoom", player_id: str) -> bool:
        """禁言玩家"""
        if not room.config.enable_group_ops:
            return False
        duration = 86400 * room.config.ban_duration_days
        success = await set_group_ban(room.group_id, player_id, duration)
        if success:
            room.banned_player_ids.add(player_id)
            logger.info(f"[狼人杀] 已禁言玩家 {player_id}")
        return success

    @staticmethod
    async def unban_player(room: "GameRoom", player_id: str) -> bool:
        """解除禁言"""
        if not room.config.enable_group_ops:
            return False
        success = await set_group_ban(room.group_id, player_id, 0)
        if success:
            room.banned_player_ids.discard(player_id)
            logger.info(f"[狼人杀] 已解除禁言 {player_id}")
        return success

    @staticmethod
    async def unban_all_players(room: "GameRoom") -> None:
        """解除所有禁言"""
        if not room.config.enable_group_ops:
            room.banned_player_ids.clear()
            return
        for player_id in list(room.banned_player_ids):
            await BanService.unban_player(room, player_id)
        room.banned_player_ids.clear()

    @staticmethod
    async def set_group_whole_ban(room: "GameRoom", enable: bool) -> bool:
        """设置全员禁言"""
        if not room.config.enable_group_ops:
            return False
        success = await set_group_whole_ban(room.group_id, enable)
        if success:
            logger.info(f"[狼人杀] 全员禁言状态: {enable}")
        return success

    @staticmethod
    async def set_temp_admin(room: "GameRoom", player_id: str) -> bool:
        """设置临时管理员（用于发言）"""
        if not room.config.enable_group_ops:
            return False
        success = await set_group_admin(room.group_id, player_id, True)
        if success:
            room.temp_admin_ids.add(player_id)
            logger.info(f"[狼人杀] 已设置临时管理员 {player_id}")
        return success

    @staticmethod
    async def remove_temp_admin(room: "GameRoom", player_id: str) -> bool:
        """取消临时管理员"""
        if not room.config.enable_group_ops:
            return False
        success = await set_group_admin(room.group_id, player_id, False)
        if success:
            room.temp_admin_ids.discard(player_id)
            logger.info(f"[狼人杀] 已取消临时管理员 {player_id}")
        return success

    @staticmethod
    async def clear_temp_admins(room: "GameRoom") -> None:
        """清除所有临时管理员"""
        if not room.config.enable_group_ops:
            room.temp_admin_ids.clear()
            return
        for player_id in list(room.temp_admin_ids):
            await BanService.remove_temp_admin(room, player_id)
        room.temp_admin_ids.clear()

    @staticmethod
    async def set_group_card(room: "GameRoom", player_id: str, card: str) -> bool:
        """设置群名片"""
        if not room.config.enable_group_ops:
            return False
        success = await set_group_card(room.group_id, player_id, card)
        if success:
            logger.info(f"[狼人杀] 已将玩家 {player_id} 群名片改为 {card}")
        return success

    @staticmethod
    async def set_player_numbers(room: "GameRoom") -> None:
        """将所有玩家群名片改为编号（仅人类玩家）"""
        for player in room.players.values():
            if player.is_ai:
                continue
            if not player.original_card:
                player.original_card = player.name
            await BanService.set_group_card(room, player.id, f"{player.number}号")

    @staticmethod
    async def restore_player_cards(room: "GameRoom") -> None:
        """恢复所有玩家原始群名片（仅人类玩家）"""
        for player in room.players.values():
            if player.is_ai:
                continue
            if player.original_card:
                await BanService.set_group_card(room, player.id, player.original_card)
