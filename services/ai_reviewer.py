"""AI复盘服务 - Nekro LLM 适配版"""

from typing import Optional, TYPE_CHECKING

from ..compat import logger
from .llm import chat as llm_chat

if TYPE_CHECKING:
    from ..models import GameRoom


AI_REVIEW_SYSTEM_PROMPT = (
    "你是一个资深的狼人杀游戏分析专家。请根据提供的游戏数据，生成一份专业的复盘报告。\n"
    "要求：\n"
    "1. 分析关键决策点和转折点\n"
    "2. 评价各阵营的策略和失误\n"
    "3. 指出精彩的操作和值得学习的地方\n"
    "4. 游戏日志中包含了狼人夜晚的密谋内容（标记为「💬 XXX（狼人）密谋：...」），"
    "如果有精彩、搞笑或关键的狼人聊天，可以适当引用原文，增加复盘的趣味性和真实感\n"
    "5. 评选出本局MVP（表现最好的玩家）和本局超级划水玩家（存在感最低/失误最多的玩家）\n"
    "6. 语言风格轻松幽默，但分析要专业深入\n"
    "7. 控制在1000字以内\n"
    "8. 使用emoji让内容更生动\n\n"
    "输出格式参考：\n"
    "[复盘分析内容]\n"
    "[如有精彩的狼人聊天，可在此引用，格式：💬 「XXX：原话内容」]\n\n"
    "🏆 本局MVP：[玩家昵称] - [简短理由]\n"
    "💤 本局超级划水：[玩家昵称] - [简短理由]"
)


class AIReviewer:
    """AI复盘服务"""

    def __init__(self):
        pass

    async def generate_review(self, room: "GameRoom", winning_faction: str) -> str:
        """生成AI复盘报告"""
        try:
            if not room.config.enable_ai_review:
                logger.info("[狼人杀] AI复盘已关闭，跳过生成")
                return ""

            game_data = self._format_game_data(room, winning_faction)
            system_prompt, user_prompt = self._build_prompts(room, game_data, winning_faction)

            review_text = await llm_chat(
                model_group=room.config.ai_review_model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.8,
                max_tokens=2000,
                timeout=90,
            )
            if not review_text:
                logger.warning("[狼人杀] AI复盘未生成内容")
                return ""

            return f"\n\n🤖 AI复盘\n{'=' * 30}\n{review_text}\n{'=' * 30}"

        except Exception as e:
            logger.error(f"[狼人杀] AI复盘生成失败: {e}")
            return ""

    def _build_prompts(self, room: "GameRoom", game_data: str, winning_faction: str) -> tuple:
        """构建AI提示词"""
        if room.config.ai_review_prompt:
            faction_name = "狼人" if winning_faction == "werewolf" else "好人"
            system_prompt = room.config.ai_review_prompt.replace(
                "{winning_faction}", faction_name
            ).replace("{game_data}", game_data)
            user_prompt = f"请为以下狼人杀游戏生成复盘报告：\n\n{game_data}"
            logger.info("[狼人杀] 使用自定义AI复盘提示词")
        else:
            system_prompt = AI_REVIEW_SYSTEM_PROMPT
            user_prompt = f"请为以下狼人杀游戏生成复盘报告：\n\n{game_data}"

        return system_prompt, user_prompt

    def _format_game_data(self, room: "GameRoom", winning_faction: str) -> str:
        """整理游戏数据为AI可读格式"""
        from ..models import Role

        lines = []

        lines.append("【游戏结果】")
        faction_name = "狼人" if winning_faction == "werewolf" else "好人"
        lines.append(f"胜利方：{faction_name}")
        lines.append("")

        lines.append("【玩家身份】")
        role_names = {
            Role.WEREWOLF: "狼人",
            Role.SEER: "预言家",
            Role.WITCH: "女巫",
            Role.HUNTER: "猎人",
            Role.VILLAGER: "村民"
        }
        for player in room.players.values():
            role_name = role_names.get(player.role, str(player.role))
            lines.append(f"{player.display_name} - {role_name}")
        lines.append("")

        if room.game_log:
            lines.append("【游戏进程】")
            for log_entry in room.game_log:
                lines.append(log_entry)
            lines.append("")

        return "\n".join(lines)
