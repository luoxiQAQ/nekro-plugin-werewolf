"""LLM 调用封装 - 通过 Nekro-Agent 的模型组直接调用

替代 AstrBot 的 provider.text_chat。默认使用 Nekro 的 USE_MODEL_GROUP，
也可在插件配置中指定专门的模型组。
"""

import asyncio
from typing import Optional

from ..compat import logger


async def chat(
    model_group: str = "",
    system_prompt: str = "",
    user_prompt: str = "",
    temperature: float = 0.7,
    max_tokens: int = 1024,
    timeout: float = 45.0,
) -> Optional[str]:
    """调用 Nekro 配置的 LLM 模型组，返回文本内容，失败返回 None

    Args:
        model_group: 模型组名称，留空使用 Nekro 默认模型组
        system_prompt: 系统提示词
        user_prompt: 用户提示词
        temperature: 温度参数
        max_tokens: 最大生成 token 数
        timeout: 超时时间（秒）
    """
    try:
        from nekro_agent.core.config import config as nekro_config
        from nekro_agent.services.agent.openai import gen_openai_chat_response, parse_extra_body
    except Exception as e:  # pragma: no cover - 非插件运行环境
        logger.error(f"[狼人杀] 无法导入 Nekro LLM 模块: {e}")
        return None

    group_name = model_group.strip() or nekro_config.USE_MODEL_GROUP
    try:
        mg = nekro_config.get_model_group_info(group_name)
    except KeyError as e:
        logger.error(f"[狼人杀] 模型组不可用: {e}")
        return None

    try:
        extra_body = parse_extra_body(
            getattr(mg, "EXTRA_BODY", "") or "",
            source_hint=f"Werewolf model group: {group_name}",
        )
    except Exception:
        extra_body = None

    try:
        response = await asyncio.wait_for(
            gen_openai_chat_response(
                model=mg.CHAT_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                api_key=mg.API_KEY,
                base_url=mg.BASE_URL,
                proxy_url=getattr(mg, "CHAT_PROXY", "") or None,
                temperature=temperature,
                max_tokens=max_tokens,
                extra_body=extra_body,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(f"[狼人杀] LLM 调用超时（{timeout}秒，模型组 {group_name}）")
        return None
    except Exception as e:
        logger.warning(f"[狼人杀] LLM 调用失败: {e}")
        return None

    content = (getattr(response, "response_content", "") or "").strip()
    return content or None
