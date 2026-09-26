"""游戏命令路由

移植自原插件的 handlers/（RoomCommandHandler、NightCommandHandler、
DayCommandHandler、QueryCommandHandler），改为与框架无关的纯函数式接口：
入参为群号/QQ号/文本参数，返回需要回复的文本（空串表示无需回复）。
由 plugin.py 分别接入 Nekro 消息钩子与 Nekro 命令系统。
"""

import os
import re
import tempfile
from typing import Optional

from .compat import logger
from .models import GamePhase, Role, AIPlayerConfig
from .phases import (
    NightWolfPhase,
    NightSeerPhase,
    NightWitchPhase,
    DaySpeakingPhase,
    DayVotePhase,
    LastWordsPhase,
    PhaseManager,
)
from .roles import RoleFactory
from .services import GameManager
from .utils import cmd

# At 段在 content_text 中的文本形式: [@id:123456;nickname:xxx@]
AT_ID_PATTERN = re.compile(r"\[@id:(\d+);")

# AI玩家名称黑名单（避免与命令冲突）
AI_NAME_BLACKLIST = {
    "加入房间", "创建房间", "开始游戏", "结束游戏",
    "投票", "办掉", "验人", "救人", "毒人", "开枪", "房间",
}


class GameRouter:
    """游戏命令路由器"""

    def __init__(self, game_manager: GameManager):
        self.manager = game_manager
        self.message_service = game_manager.message_service
        self._tmp_dir = os.path.join(tempfile.gettempdir(), "nekro_werewolf")
        os.makedirs(self._tmp_dir, exist_ok=True)

    # ========== 目标解析 ==========

    def resolve_target(self, room, target_str: str) -> Optional[str]:
        """解析目标：支持 @标记、编号、QQ号"""
        if not target_str:
            return None
        target_str = str(target_str).strip()

        # 方式1：从 At 标记中提取 QQ 号
        at_match = AT_ID_PATTERN.search(target_str)
        if at_match and at_match.group(1) in room.players:
            return at_match.group(1)

        # 方式2：编号或裸 QQ 号
        number_match = re.search(r"(\d{1,5})", target_str)
        if number_match:
            resolved = room.parse_target(number_match.group(1))
            if resolved:
                return resolved
        return None

    # ========== 房间管理命令（群聊） ==========

    async def create_room(self, group_id: str, sender_id: str, sender_name: str) -> str:
        """创建房间"""
        if not group_id:
            return "⚠️ 请在群聊中使用此命令！"

        if self.manager.room_exists(group_id):
            return "❌ 当前群已存在游戏房间！请先结束现有游戏。"

        room = self.manager.create_room(group_id=group_id, creator_id=sender_id)

        config = self.manager.config
        return (
            f"✅ 狼人杀房间创建成功！\n\n"
            f"📋 游戏规则：\n"
            f"• {config.total_players}人局（{config.werewolf_count}狼人 + {config.god_count}神 + {config.villager_count}平民）\n"
            f"• 神职：{config.get_role_description()}\n"
            f"• 夜晚：狼人办掉 → 预言家验人 → 女巫行动\n"
            f"• 白天：遗言 → 发言 → 投票放逐\n"
            f"• 遗言规则：第一晚被狼杀有遗言，投票放逐有遗言，被毒无遗言\n"
            f"• 猎人：被狼杀或投票放逐可开枪，被毒不能开枪\n"
            f"• 游戏结束后生成AI复盘报告\n\n"
            f"💡 使用 {cmd('加入房间')} 来参与游戏\n"
            f"🤖 使用 {cmd('（名字）加入')} 让AI玩家加入\n"
            f"👥 {config.total_players}人齐全后，房主使用 {cmd('开始游戏')}"
        )

    async def join_room(self, group_id: str, sender_id: str, sender_name: str) -> str:
        """加入房间"""
        if not group_id:
            return "⚠️ 请在群聊中使用此命令！"

        room = self.manager.get_room(group_id)
        if not room:
            return f"❌ 当前群未创建房间！请使用 {cmd('创建房间')}"

        if room.phase != GamePhase.WAITING:
            return "❌ 游戏已开始，无法加入！"

        if room.is_player_in_room(sender_id):
            return "⚠️ 你已经在游戏中了！"

        if room.is_full:
            return f"❌ 房间已满（{room.player_count}/{self.manager.config.total_players}）！"

        player_name = sender_name or f"玩家{sender_id[-4:]}"
        self.manager.add_player(room, sender_id, player_name)

        return (
            f"✅ {player_name} 成功加入游戏！\n\n"
            f"当前人数：{room.player_count}/{self.manager.config.total_players}"
        )

    async def start_game(self, group_id: str, sender_id: str) -> str:
        """开始游戏（房主专用）"""
        if not group_id:
            return "❌ 请在群聊中使用此命令！"

        room = self.manager.get_room(group_id)
        if not room:
            return "❌ 当前群没有创建的房间！"

        if sender_id != room.creator_id:
            return "⚠️ 只有房主才能开始游戏！"

        if room.player_count != self.manager.config.total_players:
            return f"❌ 人数不足！当前 {room.player_count}/{self.manager.config.total_players} 人"

        if room.phase != GamePhase.WAITING:
            return "❌ 游戏已经开始！"

        # 公告游戏开始
        await self.message_service.announce_game_start(room)

        # 开始游戏（分配编号/角色、私聊告知身份）
        await self.manager.start_game(room)

        # 进入狼人行动阶段
        wolf_phase = NightWolfPhase(self.manager)
        await wolf_phase.on_enter(room)
        return ""

    async def end_game(self, group_id: str, sender_id: str) -> str:
        """强制结束游戏（房主专用）"""
        if not group_id:
            return "❌ 请在群聊中使用此命令！"

        room = self.manager.get_room(group_id)
        if not room:
            return "❌ 当前群没有进行中的游戏！"

        if sender_id != room.creator_id:
            return "⚠️ 只有房主才能结束游戏！"

        await self.manager.cleanup_room(group_id)
        return "✅ 游戏已强制结束！"

    async def ai_join_room(self, group_id: str, ai_name: str) -> str:
        """AI玩家加入房间"""
        if not group_id:
            return "⚠️ 请在群聊中使用此命令！"

        room = self.manager.get_room(group_id)
        if not room:
            return f"❌ 当前群未创建房间！请使用 {cmd('创建房间')}"

        if room.phase != GamePhase.WAITING:
            return "❌ 游戏已开始，无法加入！"

        if room.is_full:
            return f"❌ 房间已满（{room.player_count}/{self.manager.config.total_players}）！"

        ai_name = (ai_name or "").strip()
        if not ai_name:
            return f"❌ AI名称不能为空！\n使用格式：{cmd('小咪加入')}"

        if len(ai_name) > 10:
            return "❌ AI名称不能超过10个字符！"

        if ai_name in AI_NAME_BLACKLIST:
            return f"❌ '{ai_name}' 是保留名称，请换一个！"

        ai_player_id = f"ai_{ai_name}"
        if room.is_player_in_room(ai_player_id):
            return f"⚠️ AI玩家 {ai_name} 已经在游戏中了！"

        ai_config = AIPlayerConfig(
            name=ai_name,
            model_id=self.manager.config.ai_player_model
        )

        ai_player = self.manager.add_ai_player(room, ai_name, ai_config)

        return (
            f"{ai_player.name} 加入游戏！\n\n"
            f"当前人数：{room.player_count}/{self.manager.config.total_players}"
        )

    async def kick_ai_player(self, group_id: str, target_name: str) -> str:
        """踢出AI玩家"""
        if not group_id:
            return "❌ 请在群聊中使用此命令！"

        room = self.manager.get_room(group_id)
        if not room:
            return "❌ 当前群没有创建的房间！"

        if room.phase != GamePhase.WAITING:
            return "❌ 游戏已开始，无法踢出玩家！"

        target_name = (target_name or "").strip()
        if not target_name:
            ai_players = [p for p in room.players.values() if p.is_ai]
            if not ai_players:
                return "❌ 当前房间没有AI玩家！"
            ai_list = "\n".join([f"  • {p.name}" for p in ai_players])
            return (
                f"❌ 请指定要踢出的AI名称！\n\n"
                f"当前AI玩家：\n{ai_list}\n\n"
                f"使用格式：{cmd('踢出AI')} 小咪"
            )

        ai_player_id = f"ai_{target_name}"
        player = room.get_player(ai_player_id)

        if not player:
            return f"❌ 未找到AI玩家：{target_name}"

        if not player.is_ai:
            return f"❌ {target_name} 不是AI玩家！"

        del room.players[ai_player_id]
        self.manager.ai_player_service.clear_player_data(ai_player_id)

        return (
            f"✅ AI玩家 {target_name} 已被踢出！\n\n"
            f"当前人数：{room.player_count}/{self.manager.config.total_players}"
        )

    async def show_status(self, group_id: str) -> str:
        """查看游戏状态"""
        if not group_id:
            return "❌ 请在群聊中使用此命令！"

        room = self.manager.get_room(group_id)
        if not room:
            return "❌ 当前群没有进行中的游戏！"

        status_text = (
            f"📊 游戏状态\n\n"
            f"阶段：{room.phase.value}\n"
            f"天数：第 {room.current_round} 天\n"
            f"存活人数：{room.alive_count}/{room.player_count}\n\n"
            f"玩家列表：\n"
        )

        players = sorted(room.players.values(), key=lambda p: p.number)
        for p in players:
            status_icon = "✅" if p.is_alive else "💀"
            status_text += f"  {status_icon} {p.number}号 - {p.name}\n"

        return status_text

    async def show_help(self, group_id: str) -> str:
        """显示帮助（尝试发送菜单图片，失败降级为文本）"""
        config = self.manager.config

        if group_id:
            try:
                from .draw import draw_menu_image
                from .onebot import send_group_image

                image = draw_menu_image(config.total_players)
                output_path = os.path.join(self._tmp_dir, "werewolf_menu.png")
                image.save(output_path)
                if await send_group_image(group_id, output_path):
                    return ""
            except Exception as e:
                logger.warning(f"[狼人杀] 生成/发送菜单图片失败: {e}，降级为文本")

        return self.help_text(config)

    @staticmethod
    def help_text(config) -> str:
        """构建文本版帮助"""
        return (
            "📖 狼人杀游戏 - 命令列表\n\n"
            "基础命令：\n"
            "  创建房间 - 创建游戏房间\n"
            "  加入房间 - 加入房间\n"
            "  开始游戏 - 开始游戏（房主）\n"
            "  查角色 - 查看角色（私聊）\n"
            "  游戏状态 - 查看游戏状态\n"
            "  结束游戏 - 结束游戏（房主）\n"
            "  （名字）加入 - 添加AI玩家\n"
            "  踢出AI 名字 - 移除AI玩家\n\n"
            f"游戏命令（使用编号1-{config.total_players}）：\n"
            "  办掉 编号 - 狼人夜晚刀人（私聊，如：办掉 1）\n"
            "  密谋 消息 - 狼人与队友交流（私聊）\n"
            "  验人 编号 - 预言家查验（私聊，如：验人 3）\n"
            "  毒人 编号 - 女巫使用毒药（私聊）\n"
            "  救人 - 女巫使用解药（私聊）\n"
            "  不操作 - 女巫不使用道具（私聊）\n"
            "  开枪 编号 - 猎人开枪带走（私聊）\n"
            "  发言完毕 - 发言说完\n"
            "  遗言完毕 - 遗言说完\n"
            "  投票 编号 - 白天投票放逐（如：投票 2）\n"
            "  开始投票 - 跳过发言直接投票（房主）\n\n"
            "游戏规则：\n"
            f"• {config.total_players}人局：{config.werewolf_count}狼人 + {config.god_count}神 + {config.villager_count}平民\n"
            f"• 使用编号（1-{config.total_players}号）代替QQ号\n"
            "• 遗言规则：第一晚被狼杀有遗言，投票放逐有遗言，被毒无遗言\n"
            "• 猎人：被狼杀或投票放逐可开枪，被毒不能开枪\n"
            f"• 游戏结束后{'生成AI复盘报告' if config.enable_ai_review else '不生成AI复盘'}\n"
            "• 狼人胜利：好人 ≤ 狼人 或 神职全灭\n"
            "• 好人胜利：狼人全部出局"
        )

    # ========== 夜晚命令（私聊） ==========

    async def werewolf_kill(self, player_id: str, target_str: str) -> str:
        """狼人办掉"""
        _, room = self.manager.get_room_by_player(player_id)
        if not room:
            return "❌ 你没有参与任何游戏！"

        if room.phase != GamePhase.NIGHT_WOLF:
            return "⚠️ 现在不是狼人行动阶段！"

        player = room.get_player(player_id)
        if not player or player.role != Role.WEREWOLF:
            return "❌ 你不是狼人！"

        if not player.is_alive:
            return "❌ 你已经出局了！"

        target_id = self.resolve_target(room, target_str)
        if not target_id:
            return f"❌ 无效的目标！\n请使用玩家编号（1-{room.player_count}）\n示例：{cmd('办掉')} 1"

        if not room.is_player_alive(target_id):
            return "❌ 目标玩家已经出局！"

        room.vote_state.night_votes[player_id] = target_id

        target_player = room.get_player(target_id)
        room.log(f"🐺 {player.display_name}（狼人）选择刀 {target_player.display_name}")

        # 同步刀人选择到AI狼人队友上下文
        for teammate in room.get_alive_werewolves():
            if teammate.id != player_id and teammate.is_ai and teammate.ai_context:
                teammate.ai_context.add_event(f"狼队友 {player.display_name} 选择刀 {target_player.display_name}")

        alive_wolves = room.get_alive_werewolves()
        human_wolves = [w for w in alive_wolves if not w.is_ai]
        human_voted_count = sum(1 for w in human_wolves if w.id in room.vote_state.night_votes)

        reply = f"✅ 你选择了办掉 {target_player.display_name}！"
        if len(human_wolves) > 1:
            reply += f"当前 {human_voted_count}/{len(human_wolves)} 名人类狼人已投票"

        # 如果有AI狼人，每次人类狼人投票都触发AI重新决策
        ai_wolves = [w for w in alive_wolves if w.is_ai]
        if ai_wolves:
            wolf_phase = NightWolfPhase(self.manager)
            await wolf_phase.trigger_ai_wolf_vote(room)

        # 检查是否所有狼人都投票了（包括AI）
        if len(room.vote_state.night_votes) >= len(alive_wolves):
            wolf_phase = NightWolfPhase(self.manager)
            await wolf_phase.on_all_voted(room)

        return reply

    async def werewolf_chat(self, player_id: str, message_text: str) -> str:
        """狼人密谋（私聊）"""
        _, room = self.manager.get_room_by_player(player_id)
        if not room:
            return "❌ 你没有参与任何游戏！"

        player = room.get_player(player_id)
        if not player or player.role != Role.WEREWOLF:
            return "❌ 你不是狼人！"

        if not player.is_alive:
            return "❌ 你已经出局了！"

        if room.phase != GamePhase.NIGHT_WOLF:
            return "⚠️ 只能在夜晚狼人行动阶段与队友交流！"

        message_text = (message_text or "").strip()
        if not message_text:
            return f"❌ 请输入要发送的消息！\n用法：{cmd('密谋')} 消息内容"

        teammates = [w for w in room.get_alive_werewolves() if w.id != player_id]
        if not teammates:
            return "❌ 没有其他存活的狼人队友！"

        import time
        room.wolf_last_chat_time = time.time()

        msg = f"🐺 队友 {player.display_name} 说：\n{message_text}"
        success_count = 0
        for teammate in teammates:
            if teammate.is_ai:
                if teammate.ai_context:
                    teammate.ai_context.add_wolf_chat(
                        player.display_name, message_text, room.current_round
                    )
                success_count += 1
            else:
                if await self.message_service.send_private_message(room, teammate.id, msg):
                    success_count += 1

        room.log(f"💬 {player.display_name}（狼人）密谋：{message_text}")
        return f"✅ 消息已发送给 {success_count} 名队友！"

    async def seer_check(self, player_id: str, target_str: str) -> str:
        """预言家验人（私聊）"""
        _, room = self.manager.get_room_by_player(player_id)
        if not room:
            return "❌ 你没有参与任何游戏！"

        if room.phase != GamePhase.NIGHT_SEER:
            return "⚠️ 现在不是预言家验人阶段！"

        player = room.get_player(player_id)
        if not player or player.role != Role.SEER:
            return "❌ 你不是预言家！"

        if room.seer_checked:
            return "❌ 你今晚已经验过人了！"

        target_id = self.resolve_target(room, target_str)
        if not target_id:
            return f"❌ 无效的目标！\n请使用玩家编号（1-{room.player_count}）\n示例：{cmd('验人')} 3"

        if target_id == player_id:
            return "❌ 不能验证自己！"

        target_player = room.get_player(target_id)
        if not target_player:
            return "❌ 目标玩家不存在！"

        is_werewolf = target_player.role == Role.WEREWOLF

        if is_werewolf:
            result_msg = f"🔮 验人结果：\n\n玩家 {target_player.display_name} 是 🐺 狼人！"
            room.log(f"🔮 {player.display_name}（预言家）验 {target_player.display_name}：狼人")
        else:
            result_msg = f"🔮 验人结果：\n\n玩家 {target_player.display_name} 是 ✅ 好人！"
            room.log(f"🔮 {player.display_name}（预言家）验 {target_player.display_name}：好人")

        # 进入女巫阶段
        seer_phase = NightSeerPhase(self.manager)
        await seer_phase.on_checked(room)

        return result_msg

    async def witch_save(self, player_id: str) -> str:
        """女巫救人（私聊）"""
        _, room = self.manager.get_room_by_player(player_id)
        if not room:
            return "❌ 你没有参与任何游戏！"

        if room.phase != GamePhase.NIGHT_WITCH:
            return "⚠️ 现在不是女巫行动阶段！"

        player = room.get_player(player_id)
        if not player or player.role != Role.WITCH:
            return "❌ 你不是女巫！"

        witch_state = room.witch_state
        if witch_state.has_acted:
            return "❌ 你今晚已经行动过了！"

        if witch_state.antidote_used:
            return "❌ 解药已经用过了！"

        if not room.last_killed_id:
            return "❌ 今晚没有人被杀，无法使用解药！"

        witch_state.saved_player_id = room.last_killed_id
        witch_state.antidote_used = True
        witch_state.has_acted = True

        saved_player = room.get_player(room.last_killed_id)
        room.log(f"💊 {player.display_name}（女巫）使用解药救了 {saved_player.display_name}")

        witch_phase = NightWitchPhase(self.manager)
        await witch_phase.on_acted(room)

        return f"✅ 你使用解药救了 {saved_player.display_name}！"

    async def witch_poison(self, player_id: str, target_str: str) -> str:
        """女巫毒人（私聊）"""
        _, room = self.manager.get_room_by_player(player_id)
        if not room:
            return "❌ 你没有参与任何游戏！"

        if room.phase != GamePhase.NIGHT_WITCH:
            return "⚠️ 现在不是女巫行动阶段！"

        player = room.get_player(player_id)
        if not player or player.role != Role.WITCH:
            return "❌ 你不是女巫！"

        witch_state = room.witch_state
        if witch_state.has_acted:
            return "❌ 你今晚已经行动过了！"

        if witch_state.poison_used:
            return "❌ 毒药已经用过了！"

        target_id = self.resolve_target(room, target_str)
        if not target_id:
            return f"❌ 无效的目标！\n请使用玩家编号（1-{room.player_count}）\n示例：{cmd('毒人')} 5"

        if not room.is_player_alive(target_id):
            return "❌ 目标玩家已经出局！"

        if target_id == player_id:
            return "❌ 不能毒自己！"

        witch_state.poisoned_player_id = target_id
        witch_state.poison_used = True
        witch_state.has_acted = True

        target_player = room.get_player(target_id)
        room.log(f"💊 {player.display_name}（女巫）使用毒药毒了 {target_player.display_name}")

        witch_phase = NightWitchPhase(self.manager)
        await witch_phase.on_acted(room)

        return f"✅ 你使用毒药毒了 {target_player.display_name}！"

    async def witch_pass(self, player_id: str) -> str:
        """女巫不操作（私聊）"""
        _, room = self.manager.get_room_by_player(player_id)
        if not room:
            return "❌ 你没有参与任何游戏！"

        if room.phase != GamePhase.NIGHT_WITCH:
            return "⚠️ 现在不是女巫行动阶段！"

        player = room.get_player(player_id)
        if not player or player.role != Role.WITCH:
            return "❌ 你不是女巫！"

        witch_state = room.witch_state
        if witch_state.has_acted:
            return "❌ 你今晚已经行动过了！"

        witch_state.has_acted = True
        room.log(f"💊 {player.display_name}（女巫）选择不操作")

        witch_phase = NightWitchPhase(self.manager)
        await witch_phase.on_acted(room)

        return "✅ 你选择不操作！"

    async def hunter_shoot(self, player_id: str, target_str: str) -> str:
        """猎人开枪（私聊）"""
        _, room = self.manager.get_room_by_player(player_id)
        if not room:
            return "❌ 你没有参与任何游戏！"

        player = room.get_player(player_id)
        if not player or player.role != Role.HUNTER:
            return "❌ 你不是猎人！"

        if room.hunter_state.pending_shot_player_id != player_id:
            return "❌ 当前不能开枪！"

        from .roles import HunterDeathType
        if room.hunter_state.death_type == HunterDeathType.POISON:
            return "❌ 你被女巫毒死，不能开枪！"

        target_id = self.resolve_target(room, target_str)
        if not target_id:
            return f"❌ 无效的目标！\n请使用玩家编号（1-{room.player_count}）\n示例：{cmd('开枪')} 1"

        if not room.is_player_alive(target_id):
            target_player = room.get_player(target_id)
            name = target_player.display_name if target_player else target_id
            return f"❌ {name} 已经出局！"

        if target_id == player_id:
            return "❌ 不能开枪带走自己！"

        target_player = room.get_player(target_id)
        result = f"💥 你开枪带走了 {target_player.display_name}！"

        phase_manager = PhaseManager(self.manager)
        await phase_manager.on_hunter_shot(room, target_id)

        return result

    async def check_role(self, player_id: str, is_private: bool) -> str:
        """查看自己的角色（私聊）"""
        if not is_private:
            return "⚠️ 请私聊机器人使用此命令！"

        _, room = self.manager.get_room_by_player(player_id)
        if not room:
            return "❌ 你没有参与任何游戏！"

        player = room.get_player(player_id)
        if not player or not player.role:
            return "❌ 游戏尚未开始，角色还未分配！"

        role_info = RoleFactory.get_role_info(player.role, player, room)
        return f"🎭 你的角色是：\n\n{role_info}"

    # ========== 白天命令（群聊） ==========

    async def finish_last_words(self, group_id: str, player_id: str) -> str:
        """遗言完毕"""
        room = self.manager.get_room(group_id)
        if not room:
            return "❌ 当前群没有进行中的游戏！"

        if room.phase != GamePhase.LAST_WORDS:
            return "⚠️ 现在不是遗言阶段！"

        if room.last_killed_id != player_id:
            return "⚠️ 只有被杀的玩家才能使用此命令！"

        last_words_phase = LastWordsPhase(self.manager)
        await last_words_phase.on_finish(room)
        return "✅ 遗言完毕！"

    async def finish_speaking(self, group_id: str, player_id: str) -> str:
        """发言完毕"""
        room = self.manager.get_room(group_id)
        if not room:
            return "❌ 当前群没有进行中的游戏！"

        if room.phase not in (GamePhase.DAY_SPEAKING, GamePhase.DAY_PK):
            return "⚠️ 现在不是发言阶段！"

        if room.speaking_state.current_speaker_id != player_id:
            return "⚠️ 现在不是你的发言时间！"

        speaking_phase = DaySpeakingPhase(self.manager)
        await speaking_phase.on_finish_speaking(room)
        return "✅ 发言完毕！"

    async def start_vote(self, group_id: str, player_id: str) -> str:
        """跳过发言进入投票（房主专用）"""
        room = self.manager.get_room(group_id)
        if not room:
            return "❌ 当前群没有进行中的游戏！"

        if player_id != room.creator_id:
            return "⚠️ 只有房主才能跳过发言环节！"

        if room.phase not in (GamePhase.DAY_SPEAKING, GamePhase.DAY_PK):
            return "⚠️ 现在不是发言阶段！"

        # 取消定时器
        room.cancel_timer()

        # 取消当前发言者临时管理员
        from .services import BanService
        if room.speaking_state.current_speaker_id:
            await BanService.remove_temp_admin(room, room.speaking_state.current_speaker_id)

        phase_manager = PhaseManager(self.manager)

        if room.phase == GamePhase.DAY_PK:
            await phase_manager.enter_pk_vote_phase(room)
        else:
            await phase_manager.enter_vote_phase(room)

        return "✅ 房主跳过发言环节，直接进入投票！"

    async def day_vote(self, group_id: str, player_id: str, target_str: str) -> str:
        """投票放逐"""
        room = self.manager.get_room(group_id)
        if not room:
            return "❌ 当前群没有进行中的游戏！"

        if room.phase != GamePhase.DAY_VOTE:
            return f"⚠️ 现在不是投票阶段！"

        if not room.is_player_in_room(player_id):
            return "❌ 你不在游戏中！"

        if not room.is_player_alive(player_id):
            return "❌ 你已经出局了！"

        if not target_str:
            return f"❌ 请指定投票目标！\n使用：{cmd('投票')} 编号\n示例：{cmd('投票')} 2"

        target_id = self.resolve_target(room, target_str)
        if not target_id:
            return f"❌ 无效的目标：{target_str}\n请使用玩家编号（1-{room.player_count}），或直接 @ 玩家"

        if not room.is_player_alive(target_id):
            return "❌ 目标玩家已经出局！"

        # PK投票限制
        if room.vote_state.is_pk_vote and target_id not in room.vote_state.pk_players:
            pk_names = []
            for pid in room.vote_state.pk_players:
                p = room.get_player(pid)
                if p:
                    pk_names.append(p.display_name)
            return (
                f"❌ PK投票只能投给平票玩家！\n\n"
                f"可投票对象：\n" + "\n".join([f"  • {name}" for name in pk_names])
            )

        # 记录投票
        room.vote_state.day_votes[player_id] = target_id

        voter = room.get_player(player_id)
        target = room.get_player(target_id)
        is_pk = room.vote_state.is_pk_vote
        if is_pk:
            room.log(f"🗳️ PK投票：{voter.display_name} 投给 {target.display_name}")
        else:
            room.log(f"🗳️ {voter.display_name} 投票给 {target.display_name}")

        # 同步投票到所有AI玩家上下文
        for p in room.players.values():
            if p.is_ai and p.ai_context:
                p.ai_context.add_vote(voter.display_name, target.display_name, is_pk)

        reply = (
            f"✅ {voter.display_name} 投票给 {target.display_name}！\n"
            f"当前已投票 {len(room.vote_state.day_votes)}/{room.alive_count} 人"
        )

        # 检查是否所有人都投票了
        if len(room.vote_state.day_votes) >= room.alive_count:
            vote_phase = DayVotePhase(self.manager)
            await vote_phase.on_all_voted(room)

        return reply

    # ========== 发言捕获 ==========

    def capture_group_text(self, group_id: str, player_id: str, message_text: str) -> bool:
        """捕获发言阶段和遗言阶段的玩家发言、投票阶段的讨论

        返回 True 表示消息已被游戏消费（应阻止继续处理）
        """
        if not message_text or not message_text.strip():
            return False

        room = self.manager.get_room(group_id)
        if not room:
            return False

        # 投票阶段：监听所有存活玩家的讨论
        if room.phase == GamePhase.DAY_VOTE:
            player = room.get_player(player_id)
            if player and player.is_alive:
                room.vote_discussion.append({
                    "player": player.display_name,
                    "content": message_text[:100]
                })
                for p in room.players.values():
                    if p.is_ai and p.ai_context:
                        p.ai_context.add_vote_discussion(player.display_name, message_text[:120])
                return False  # 记录但不拦截，正常聊天继续

        # 发言阶段和遗言阶段：只记录当前发言者
        if room.phase not in (GamePhase.DAY_SPEAKING, GamePhase.DAY_PK, GamePhase.LAST_WORDS):
            return False

        if room.phase == GamePhase.LAST_WORDS:
            if room.last_killed_id != player_id:
                return False
        else:
            if room.speaking_state.current_speaker_id != player_id:
                return False

        # 拦截当前发言者的消息并记录（避免触发 AI 回复）
        room.speaking_state.current_speech.append(message_text)
        return True
