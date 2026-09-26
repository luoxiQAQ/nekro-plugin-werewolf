"""行动基类 - 所有AI行动的基础（Nekro LLM 适配版）"""
import asyncio
import re
from typing import Optional, TYPE_CHECKING

from ....compat import logger
from ...llm import chat as llm_chat

if TYPE_CHECKING:
    from ....models import Player


class BaseAction:
    """AI行动基类"""

    LLM_TIMEOUT_SECONDS = 45

    def __init__(self, context=None):
        self.context = context

    async def _call_llm(
        self,
        prompt: str,
        player: "Player",
        max_retries: int = 3,
        retry_delay: float = 1.0,
        timeout: float = None
    ) -> Optional[str]:
        """调用LLM获取AI决策（带重试和超时保护）"""
        model_group = ""
        if player.ai_config:
            model_group = player.ai_config.model_id
            max_retries = player.ai_config.max_retries
            retry_delay = player.ai_config.retry_delay

        if timeout is None:
            timeout = self.LLM_TIMEOUT_SECONDS

        for attempt in range(max_retries):
            try:
                result = await asyncio.wait_for(
                    llm_chat(
                        model_group=model_group,
                        system_prompt=(
                            "你是一个沉浸式狼人杀玩家。你必须完全代入你的角色，像真人一样说话，"
                            "绝对不能暴露你是AI。回复要简洁自然，像在群里聊天。"
                        ),
                        user_prompt=prompt,
                        temperature=0.9,
                        max_tokens=800,
                        timeout=timeout,
                    ),
                    timeout=timeout + 5,
                )

                if result:
                    logger.info(f"[狼人杀AI] {player.name} 决策: {result[:100]}")
                    return result
                logger.warning(f"[狼人杀AI] {player.name} 第{attempt + 1}次调用返回空内容")

            except asyncio.TimeoutError:
                logger.warning(f"[狼人杀AI] {player.name} 第{attempt + 1}次调用超时（{timeout}秒）")
            except Exception as e:
                logger.warning(f"[狼人杀AI] {player.name} 第{attempt + 1}次调用失败: {e}")

            # 统一在循环末尾等待重试
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay)

        logger.error(f"[狼人杀AI] {player.name} 所有重试均失败")
        return None

    @staticmethod
    def extract_number(response: str) -> Optional[int]:
        """从响应中提取数字"""
        numbers = re.findall(r'\d+', response)
        if numbers:
            return int(numbers[0])
        return None

    @staticmethod
    def validate_target_range(target: int, min_val: int = 1, max_val: int = 9) -> Optional[int]:
        """验证目标范围"""
        if min_val <= target <= max_val:
            return target
        return None
