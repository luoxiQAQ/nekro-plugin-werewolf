"""局势分析器 - 分析游戏局势和玩家行为"""
import re
from typing import Dict, List, TYPE_CHECKING
from ....compat import logger

from ..prompts import (
    SITUATION_TEMPLATE,
    TACTICAL_DIRECTIVES,
    DUEL_CONTEXT_TEMPLATE,
    BEHAVIOR_ANALYSIS_TIPS,
    BEHAVIOR_TAG_DEFINITIONS
)

if TYPE_CHECKING:
    from ....models import GameRoom, Player


class SituationAnalyzer:
    """局势分析器"""

    @staticmethod
    def get_situation_awareness(room: "GameRoom") -> str:
        """获取局势感知"""
        alive_count = room.alive_count
        if alive_count >= 7:
            situation = "游戏初期，信息较少"
            good_count, wolf_count = alive_count - 3, 3
        elif alive_count >= 5:
            situation = "游戏中期，局势逐渐明朗"
            good_count, wolf_count = alive_count - 2, 2
        else:
            situation = "游戏后期，每一票都很关键！"
            good_count, wolf_count = alive_count - 1, 1

        return SITUATION_TEMPLATE.format(
            alive_count=alive_count,
            good_count=good_count,
            wolf_count=wolf_count,
            situation=situation
        )

    @staticmethod
    def get_tactical_directive(player: "Player", room: "GameRoom") -> str:
        """获取动态战术指令"""
        from .builder import ContextBuilder

        alive_count = room.alive_count
        alive_wolves = room.get_alive_werewolves()
        wolf_count = len(alive_wolves)
        good_count = alive_count - wolf_count

        role_key = ContextBuilder.get_role_key(player)

        # 狼人视角
        if role_key == "werewolf":
            if wolf_count >= good_count:
                return TACTICAL_DIRECTIVES["wolf_advantage"]
            elif wolf_count >= good_count - 1 and alive_count <= 5:
                return TACTICAL_DIRECTIVES["wolf_final_push"]
            elif wolf_count == 1 and alive_count >= 4:
                return TACTICAL_DIRECTIVES["wolf_disadvantage"]
            elif player.ai_context:
                events = player.ai_context.game_events
                for event in events[-5:]:
                    if "查杀" in event and any(
                        teammate.display_name in event
                        for teammate in alive_wolves
                        if teammate.id != player.id
                    ):
                        return TACTICAL_DIRECTIVES["wolf_teammate_exposed"]
            if player.ai_context:
                events = player.ai_context.game_events
                for event in events[-10:]:
                    if "预言家" in event and ("死" in event or "出局" in event):
                        return TACTICAL_DIRECTIVES["good_confused"]
            return TACTICAL_DIRECTIVES["normal"]

        # 好人视角
        else:
            if good_count <= wolf_count + 1 and alive_count <= 5:
                return TACTICAL_DIRECTIVES["good_desperate"]
            return ""

    @staticmethod
    def get_duel_context(player: "Player", room: "GameRoom") -> str:
        """检测对跳并生成辩论提示词"""
        if not player.ai_context:
            return ""

        events = player.ai_context.game_events
        seer_jumpers = []

        for event in events:
            if "预言家" in event and ("跳" in event or "是预言家" in event):
                match = re.search(r'(\d+号\S*|\S+).*?预言家', event)
                if match:
                    jumper = match.group(1)
                    if jumper not in seer_jumpers:
                        seer_jumpers.append(jumper)

        if len(seer_jumpers) >= 2:
            if player.display_name in seer_jumpers:
                opponent = [j for j in seer_jumpers if j != player.display_name][0]
                return DUEL_CONTEXT_TEMPLATE["attacker"].format(opponent=opponent)
            else:
                return DUEL_CONTEXT_TEMPLATE["observer"].format(
                    player_a=seer_jumpers[0],
                    player_b=seer_jumpers[1]
                )

        return ""


class BehaviorAnalyzer:
    """玩家行为分析器"""

    @staticmethod
    def analyze_player_behaviors(room: "GameRoom") -> Dict[str, List[str]]:
        """分析所有玩家的行为并生成标签"""
        player_tags: Dict[str, List[str]] = {}

        for p in room.get_alive_players():
            tags = []
            if not p.ai_context:
                continue

            speeches = p.ai_context.speeches if hasattr(p.ai_context, 'speeches') else []
            player_speeches = [s for s in speeches if s.get('player') == p.display_name]

            if player_speeches:
                last_speech = player_speeches[-1].get('content', '') if player_speeches else ''
                if len(last_speech) < 15 or any(kw in last_speech for kw in ['过了', '没想法', '听不出']):
                    tags.append("划水")

            votes = p.ai_context.vote_history if hasattr(p.ai_context, 'vote_history') else []
            player_votes = [v for v in votes if v.get('voter') == p.display_name]

            if len(player_votes) >= 2:
                tags.append("跟票")

            player_tags[p.display_name] = tags

        return player_tags

    @staticmethod
    def get_behavior_analysis_prompt(player: "Player", room: "GameRoom") -> str:
        """生成玩家行为分析提示词"""
        if not player.ai_context:
            return ""

        lines = ["【🏷️ 玩家行为画像】"]
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

        for p in room.get_alive_players():
            if p.id == player.id:
                continue

            tags = []
            speeches = player.ai_context.speeches
            votes = player.ai_context.vote_history

            p_speeches = [s for s in speeches if s.get('player') == p.display_name]
            p_votes = [v for v in votes if v.get('voter') == p.display_name]

            if p_speeches:
                last_speech = p_speeches[-1].get('content', '')
                if len(last_speech) < 20 or any(kw in last_speech for kw in ['过了', '没想法', '听不出', '不知道']):
                    tags.append("划水")
                if any(kw in last_speech for kw in ['！', '?!', '什么鬼', '搞笑']):
                    tags.append("情绪激动")
                if any(kw in last_speech for kw in ['因为', '所以', '逻辑', '分析', '证据']):
                    tags.append("逻辑清晰")
                if any(kw in last_speech for kw in ['不一定是狼', '可能冤枉', '再看看', '先别投']):
                    tags.append("试图保人")
                if any(kw in last_speech for kw in ['假预言家', '悍跳', '不信', '骗子']):
                    tags.append("攻击预言家")

            if tags:
                lines.append(f"- {p.display_name}：【标签：{'】【标签：'.join(tags)}】")

        if len(lines) > 2:
            lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            lines.append("")
            lines.append(BEHAVIOR_ANALYSIS_TIPS)
            return "\n".join(lines)

        return ""
