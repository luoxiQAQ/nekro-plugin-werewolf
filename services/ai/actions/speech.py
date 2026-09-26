"""发言行动 - 白天发言和遗言"""
import re
import random
from typing import List, TYPE_CHECKING
from ....compat import logger

from .base import BaseAction
from ..context import ContextBuilder, SituationAnalyzer, BehaviorAnalyzer
from ..prompts import (
    ANTI_HALLUCINATION_PROTOCOL,
    HUMAN_STYLE_TIPS,
    ROLE_SOUL_SETTINGS,
    PERSONALITY_TEMPLATES,
    ROLE_PROMPTS,
    SPEECH_TIPS,
    PK_TIPS,
    LAST_WORDS_TIPS
)

if TYPE_CHECKING:
    from ....models import GameRoom, Player


class SpeechAction(BaseAction):
    """发言行动"""

    def __init__(self, context):
        super().__init__(context)
        self._player_personalities = {}

    def _get_player_personality(self, player: "Player") -> str:
        """获取或分配玩家性格"""
        if player.id not in self._player_personalities:
            personality_key = random.choice(list(PERSONALITY_TEMPLATES.keys()))
            self._player_personalities[player.id] = personality_key
            logger.info(f"[狼人杀AI] 为 {player.name} 分配性格: {personality_key}")
        return PERSONALITY_TEMPLATES[self._player_personalities[player.id]]

    async def generate_speech(self, player: "Player", room: "GameRoom", is_pk: bool = False) -> str:
        """AI生成白天发言"""
        context = ContextBuilder.build_context(player, room)
        context += "\n" + SituationAnalyzer.get_situation_awareness(room)

        # 检查特殊事件
        special_event_tip = ContextBuilder.get_special_event_tip(player, room)
        if special_event_tip:
            context += "\n" + special_event_tip

        # 添加战术指令
        tactical_directive = SituationAnalyzer.get_tactical_directive(player, room)
        if tactical_directive:
            context += "\n" + tactical_directive

        # 添加对跳辩论提示词
        duel_context = SituationAnalyzer.get_duel_context(player, room)
        if duel_context:
            context += "\n" + duel_context

        # 添加玩家行为分析
        behavior_analysis = BehaviorAnalyzer.get_behavior_analysis_prompt(player, room)
        if behavior_analysis:
            context += "\n" + behavior_analysis

        role_key = ContextBuilder.get_role_key(player)
        role_name = player.role.display_name if player.role else "玩家"
        soul_setting = ROLE_SOUL_SETTINGS.get(role_key, "")
        personality = self._get_player_personality(player)

        if is_pk:
            pk_tips = PK_TIPS.get(role_key, PK_TIPS["villager"])
            prompt = ROLE_PROMPTS["pk_speech"].format(
                anti_hallucination=ANTI_HALLUCINATION_PROTOCOL,
                soul_setting=soul_setting,
                personality=personality,
                context=context,
                pk_tips=pk_tips,
                human_style=HUMAN_STYLE_TIPS
            )
        else:
            # 动态调整村民提示词
            if role_key == "villager" and player.ai_context and player.ai_context.current_round == 1:
                speech_tips = """【👨‍🌾 村民首日发言】
⚠️ 这是第一天，信息量较少，不要过度推理或编造不存在的信息！

🗣️ 首日发言建议：
1. 如果还没有人发言：简单表态，等待信息
2. 如果已有少量发言：简单评价，不要过度分析
3. 如果有预言家跳出：可以表态支持或怀疑，但要基于实际发言
4. 严禁编造"昨天"、"前一天"等虚假信息

💡 记住：根据已有的发言内容发言，不要分析不存在的事情！"""
            else:
                speech_tips = SPEECH_TIPS.get(role_key, SPEECH_TIPS["villager"])

            prompt = ROLE_PROMPTS["day_speech"].format(
                anti_hallucination=ANTI_HALLUCINATION_PROTOCOL,
                soul_setting=soul_setting,
                personality=personality,
                context=context,
                speech_tips=speech_tips,
                human_style=HUMAN_STYLE_TIPS
            )

        response = await self._call_llm(prompt, player)
        if response:
            response = re.sub(r'^[\[【]?(发言|说话|speech)[\]】]?[：:]\s*', '', response, flags=re.IGNORECASE)
            return response[:300]

        defaults = [
            "我先听听大家怎么说吧",
            "目前信息太少了，我再观察一下",
            "emmm 我暂时没什么想法",
        ]
        return random.choice(defaults)

    async def generate_last_words(self, player: "Player", room: "GameRoom") -> str:
        """AI生成遗言"""
        context = ContextBuilder.build_context(player, room)
        role_key = ContextBuilder.get_role_key(player)
        role_name = player.role.display_name if player.role else "玩家"

        last_words_tips = LAST_WORDS_TIPS.get(role_key, LAST_WORDS_TIPS["villager"])

        # 预言家特殊处理
        if role_key == "seer" and player.ai_context and player.ai_context.seer_results:
            results = [f"{r['target']}是{'狼' if r['is_werewolf'] else '金水'}"
                      for r in player.ai_context.seer_results]
            last_words_tips += f"\n\n🔮 【重要】你的查验记录：{'; '.join(results)}\n务必全部公布出来！"

        prompt = ROLE_PROMPTS["last_words"].format(
            anti_hallucination=ANTI_HALLUCINATION_PROTOCOL,
            context=context,
            role_name=role_name,
            last_words_tips=last_words_tips,
            human_style=HUMAN_STYLE_TIPS
        )

        response = await self._call_llm(prompt, player)
        if response:
            return response[:100]

        return "我没什么好说的了，祝大家好运。"
