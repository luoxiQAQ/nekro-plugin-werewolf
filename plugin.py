"""狼人杀插件 - Nekro-Agent 入口

移植自 astrbot_plugin_werewolf v3.0.2 (miao)。
游戏规则：9人局（3狼人 + 3神 + 3平民），支持 AI 玩家与 AI 复盘。

交互模型：
- 群聊裸文本命令（创建房间/加入房间/投票 N/发言完毕 ...）由消息钩子拦截处理；
- 私聊夜晚命令（办掉/验人/救人/毒人/不操作/开枪/密谋/查角色）同样由消息钩子处理；
- 同时注册到 Nekro 命令系统（/创建房间 等带前缀形式亦可触发）。
"""

import re
from typing import Annotated, Optional

from nekro_agent.api.plugin import (
    Arg,
    CmdCtl,
    CommandExecutionContext,
    CommandPermission,
    CommandResponse,
    ConfigBase,
    NekroPlugin,
)
from nekro_agent.api.signal import MsgSignal
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.schemas.chat_message import ChatMessage
from pydantic import Field

from .compat import logger
from .game_router import AI_NAME_BLACKLIST, AT_ID_PATTERN, GameRouter
from .models import GameConfig, GamePhase
from .onebot import extract_chat_type_and_id, send_group_text
from .runtime_cfg import bind_config, get_config
from .services import GameManager
from .utils import set_command_prefix

plugin = NekroPlugin(
    name="狼人杀",
    module_name="nekro_plugin_werewolf",
    description="群聊狼人杀游戏：创建房间拉人开局，夜晚私聊行动，白天发言投票，支持 AI 玩家补位与 AI 复盘",
    author="miao_luoxi",
    version="1.0.0",
    url="https://github.com/libin99527/astrbot_plugin_werewolf",
    support_adapter=["onebot_v11"],
)

# Nekro 命令前缀形式的消息会先经过命令系统，钩子只需处理裸文本；
# 这里仍兼容以 / 或 ／ 开头但未命中命令系统的写法（如 /投票3）。
LEADING_PREFIX_PATTERN = re.compile(r"^[/／]\s*")

# 群聊中始终拦截的命令（无需房间存在）
GLOBAL_GROUP_COMMANDS = {"创建房间", "狼人杀帮助", "游戏状态"}

# 有房间存在时拦截的群聊命令（支持可选参数）
ROOM_GROUP_COMMANDS = {
    "加入房间", "加人房间", "加入", "加人",
    "开始游戏", "结束游戏",
    "发言完毕", "遗言完毕", "开始投票", "查角色",
}

# 无房间时仍给出引导的群聊命令
NO_ROOM_GROUP_COMMANDS = {"加入房间", "加人房间", "开始游戏", "结束游戏"}

# 夜晚/私聊命令：命令名 -> 是否带目标参数
NIGHT_COMMANDS = {
    "办掉": True, "验人": True, "毒人": True, "开枪": True,
    "救人": False, "不操作": False,
}

AI_JOIN_PATTERN = re.compile(r"^[/／]?(.{1,10}?)加入$")
KICK_AI_PATTERN = re.compile(r"^[/／]?踢出AI\s*(.*)$")


@plugin.mount_config()
class WerewolfConfig(ConfigBase):
    """狼人杀插件配置"""

    total_players: int = Field(default=9, title="总玩家数", description="游戏总人数（需与各角色数量之和一致，否则使用默认 9 人局）")
    werewolf_count: int = Field(default=3, title="狼人数量", description="狼人玩家人数")
    seer_count: int = Field(default=1, title="预言家数量", description="预言家人数")
    witch_count: int = Field(default=1, title="女巫数量", description="女巫人数")
    hunter_count: int = Field(default=1, title="猎人数量", description="猎人人")
    villager_count: int = Field(default=3, title="平民数量", description="平民人数")

    timeout_wolf: int = Field(default=120, title="狼人行动超时(秒)", description="狼人夜晚刀人阶段的超时时间")
    timeout_seer: int = Field(default=120, title="预言家超时(秒)", description="预言家验人阶段的超时时间")
    timeout_witch: int = Field(default=120, title="女巫超时(秒)", description="女巫行动阶段的超时时间")
    timeout_hunter: int = Field(default=120, title="猎人开枪超时(秒)", description="猎人开枪阶段的超时时间")
    timeout_speaking: int = Field(default=120, title="发言超时(秒)", description="白天发言/遗言的单人超时时间")
    timeout_vote: int = Field(default=120, title="投票超时(秒)", description="白天投票阶段的超时时间")
    timeout_dead_min: int = Field(default=10, title="已死角色最小等待(秒)", description="已死亡角色阶段的最小随机等待时间")
    timeout_dead_max: int = Field(default=15, title="已死角色最大等待(秒)", description="已死亡角色阶段的最大随机等待时间")

    ban_duration_days: int = Field(default=30, title="禁言时长(天)", description="出局玩家被禁言的时长（天）")
    enable_group_ops: bool = Field(default=True, title="启用群管理操作", description="开启后游戏会使用禁言/群名片/临时管理员功能，需要 Bot 是群管理员或群主；关闭则仅进行游戏流程")

    enable_ai_review: bool = Field(default=True, title="启用AI复盘", description="游戏结束后调用大模型生成复盘报告")
    ai_review_model_group: str = Field(default="", title="AI复盘模型组", description="用于生成复盘的模型组名称，留空使用默认模型组")
    ai_review_prompt: str = Field(default="", title="自定义复盘提示词", description="支持 {winning_faction} 与 {game_data} 占位符，留空使用内置提示词")
    ai_player_model_group: str = Field(default="", title="AI玩家模型组", description="AI 玩家决策使用的模型组名称，留空使用默认模型组")

    ai_join_need_admin: bool = Field(default=False, title="添加AI玩家需要管理员", description="开启后仅 Nekro 管理员可使用「名字加入」添加 AI 玩家")


# ==================== 游戏管理器 ====================

_game_manager: Optional[GameManager] = None
_router: Optional[GameRouter] = None


def _build_game_config(cfg: WerewolfConfig) -> GameConfig:
    """由插件配置构建游戏配置，并校验角色数量"""
    game_config = GameConfig(
        total_players=cfg.total_players,
        werewolf_count=cfg.werewolf_count,
        seer_count=cfg.seer_count,
        witch_count=cfg.witch_count,
        hunter_count=cfg.hunter_count,
        villager_count=cfg.villager_count,
        timeout_wolf=cfg.timeout_wolf,
        timeout_seer=cfg.timeout_seer,
        timeout_witch=cfg.timeout_witch,
        timeout_hunter=cfg.timeout_hunter,
        timeout_speaking=cfg.timeout_speaking,
        timeout_vote=cfg.timeout_vote,
        timeout_dead_min=cfg.timeout_dead_min,
        timeout_dead_max=cfg.timeout_dead_max,
        ban_duration_days=cfg.ban_duration_days,
        enable_group_ops=cfg.enable_group_ops,
        enable_ai_review=cfg.enable_ai_review,
        ai_review_model=cfg.ai_review_model_group,
        ai_review_prompt=cfg.ai_review_prompt,
        ai_player_model=cfg.ai_player_model_group,
    )
    if not game_config.validate():
        logger.warning("[狼人杀] 角色配置不匹配！使用默认配置：9人局（3狼3神3平民）")
        game_config = GameConfig()
    return game_config


def get_game_manager() -> GameManager:
    """获取游戏管理器单例（配置修改后新房间会读取最新配置）"""
    global _game_manager, _router
    cfg = get_config()
    game_config = _build_game_config(cfg) if cfg else GameConfig()

    if _game_manager is None:
        _game_manager = GameManager(game_config)
        _router = GameRouter(_game_manager)
    else:
        # 房间创建时使用最新配置；已有房间保持各自的游戏配置快照
        _game_manager.config = game_config
    return _game_manager


def get_router() -> GameRouter:
    """获取命令路由器"""
    get_game_manager()
    assert _router is not None
    return _router


# ==================== 插件生命周期 ====================


@plugin.mount_init_method()
async def init() -> None:
    """插件加载"""
    bind_config(plugin.get_config())
    set_command_prefix("")
    manager = get_game_manager()
    config = manager.config
    logger.info(
        f"[狼人杀] 插件已加载 | "
        f"游戏配置：{config.total_players}人局"
        f"({config.werewolf_count}狼{config.god_count}神{config.villager_count}民) | "
        f"AI复盘：{'开启' if config.enable_ai_review else '关闭'}"
    )


@plugin.mount_cleanup_method()
async def cleanup() -> None:
    """插件卸载时清理所有房间"""
    global _game_manager, _router
    if _game_manager is not None:
        for group_id in list(_game_manager.rooms.keys()):
            await _game_manager.cleanup_room(group_id)
        _game_manager = None
        _router = None
    logger.info("[狼人杀] 插件已清理")


@plugin.on_enabled()
async def on_enabled() -> None:
    """插件启用时重新绑定配置"""
    bind_config(plugin.get_config())
    await init()


@plugin.on_disabled()
async def on_disabled() -> None:
    """插件禁用时清理房间"""
    await cleanup()


# ==================== 权限辅助 ====================


async def _send_group_reply(group_id: str, text: str) -> None:
    """向群聊发送游戏回复"""
    if text:
        await send_group_text(group_id, text)


async def is_nekro_admin(adapter_key: str, platform_userid: str) -> bool:
    """判断平台用户是否为 Nekro 管理员（perm_level >= Admin）"""
    try:
        from nekro_agent.models.db_user import DBUser
        from nekro_agent.services.user.role import Role

        user = await DBUser.get_by_union_id(
            adapter_key=adapter_key, platform_userid=str(platform_userid)
        )
        if not user:
            return False
        return user.perm_level >= Role.Admin
    except Exception as e:
        logger.warning(f"[狼人杀] 查询用户权限失败: {e}")
        return False


# ==================== 消息钩子 ====================


def _strip_prefix(text: str) -> str:
    """去掉可能的命令前缀与首尾空白"""
    text = text.strip()
    text = LEADING_PREFIX_PATTERN.sub("", text)
    return text.strip()


def _extract_at_ids(text: str) -> list:
    """提取文本中 @ 的用户 ID 列表"""
    return AT_ID_PATTERN.findall(text)


async def _handle_group_message(ctx: AgentCtx, message: ChatMessage) -> Optional[MsgSignal]:
    """处理群聊消息"""
    _, group_id = extract_chat_type_and_id(message.chat_key)
    if not group_id:
        return None

    router = get_router()
    manager = get_game_manager()
    room = manager.get_room(group_id)
    sender_id = str(message.sender_id).strip()
    sender_name = message.sender_nickname or message.sender_name or f"玩家{sender_id[-4:]}"
    text = _strip_prefix(message.content_text)

    # 1. 全局命令（无房间也可用）
    if text in GLOBAL_GROUP_COMMANDS:
        if text == "创建房间":
            reply = await router.create_room(group_id, sender_id, sender_name)
        elif text == "游戏状态":
            reply = await router.show_status(group_id)
        else:  # 狼人杀帮助
            reply = await router.show_help(group_id)
        if reply:
            await _send_group_reply(group_id, reply)
        return MsgSignal.BLOCK_ALL

    # 2. AI 玩家加入（「名字加入」）
    ai_match = AI_JOIN_PATTERN.match(text)
    if ai_match and room and room.phase == GamePhase.WAITING:
        ai_name = ai_match.group(1).strip()
        if ai_name and ai_name not in AI_NAME_BLACKLIST:
            cfg = get_config()
            if getattr(cfg, "ai_join_need_admin", False) and not await is_nekro_admin(
                ctx.adapter_key or "onebot_v11", sender_id
            ):
                await _send_group_reply(
                    group_id, "⚠️ 只有管理员才能添加 AI 玩家！"
                )
                return MsgSignal.BLOCK_ALL
            reply = await router.ai_join_room(group_id, ai_name)
            if reply:
                await _send_group_reply(group_id, reply)
            return MsgSignal.BLOCK_ALL

    # 3. 踢出AI
    kick_match = KICK_AI_PATTERN.match(text)
    if kick_match and room:
        reply = await router.kick_ai_player(group_id, kick_match.group(1))
        if reply:
            await _send_group_reply(group_id, reply)
        return MsgSignal.BLOCK_ALL

    # 4. 夜晚命令在群聊中打出时（玩家已在游戏中），给出对应错误提示（与原版一致）
    night_match = re.match(r"^(办掉|验人|毒人|开枪)\s*(\S*)$", text)
    if night_match and manager.get_room_by_player(sender_id)[1]:
        target_str = night_match.group(2) or "".join(_extract_at_ids(text))
        if night_match.group(1) == "办掉":
            reply = await router.werewolf_kill(sender_id, target_str)
        elif night_match.group(1) == "验人":
            reply = await router.seer_check(sender_id, target_str)
        elif night_match.group(1) == "毒人":
            reply = await router.witch_poison(sender_id, target_str)
        else:
            reply = await router.hunter_shoot(sender_id, target_str)
        if reply:
            await _send_group_reply(group_id, reply)
        return MsgSignal.BLOCK_ALL

    if text in ("救人", "不操作", "密谋") and manager.get_room_by_player(sender_id)[1]:
        if text == "救人":
            reply = await router.witch_save(sender_id)
        elif text == "不操作":
            reply = await router.witch_pass(sender_id)
        else:
            reply = "⚠️ 请私聊机器人使用此命令！"
        if reply:
            await _send_group_reply(group_id, reply)
        return MsgSignal.BLOCK_ALL

    # 5. 房间内命令（白天流程与房间管理）
    if room:
        if text in ("加入房间", "加人房间", "加入", "加人"):
            reply = await router.join_room(group_id, sender_id, sender_name)
            await _send_group_reply(group_id, reply)
            return MsgSignal.BLOCK_ALL

        if text == "开始游戏":
            reply = await router.start_game(group_id, sender_id)
            await _send_group_reply(group_id, reply)
            return MsgSignal.BLOCK_ALL

        if text == "结束游戏":
            reply = await router.end_game(group_id, sender_id)
            await _send_group_reply(group_id, reply)
            return MsgSignal.BLOCK_ALL

        if text in ("发言完毕", "遗言完毕", "开始投票", "查角色"):
            if text == "发言完毕":
                reply = await router.finish_speaking(group_id, sender_id)
            elif text == "遗言完毕":
                reply = await router.finish_last_words(group_id, sender_id)
            elif text == "开始投票":
                reply = await router.start_vote(group_id, sender_id)
            else:
                reply = await router.check_role(sender_id, is_private=False)
            if reply:
                await _send_group_reply(group_id, reply)
            return MsgSignal.BLOCK_ALL

        vote_match = re.match(r"^投票\s*(.*)$", text)
        if vote_match:
            target_str = vote_match.group(1).strip() or "".join(_extract_at_ids(text))
            reply = await router.day_vote(group_id, sender_id, target_str)
            if reply:
                await _send_group_reply(group_id, reply)
            return MsgSignal.BLOCK_ALL

        # 发言/遗言捕获（当前发言者的普通消息）
        if router.capture_group_text(group_id, sender_id, message.content_text):
            return MsgSignal.BLOCK_ALL

        return None

    # 6. 无房间时的引导命令
    if text in NO_ROOM_GROUP_COMMANDS:
        if text in ("加入房间", "加人房间"):
            reply = "❌ 当前群未创建房间！请使用 创建房间"
        else:
            reply = "❌ 当前群没有创建的房间！"
        await _send_group_reply(group_id, reply)
        return MsgSignal.BLOCK_ALL

    return None


async def _handle_private_message(ctx: AgentCtx, message: ChatMessage) -> Optional[MsgSignal]:
    """处理私聊消息（夜晚行动等）"""
    router = get_router()
    manager = get_game_manager()
    sender_id = str(message.sender_id).strip()
    text = _strip_prefix(message.content_text)
    in_game = manager.get_room_by_player(sender_id)[1] is not None

    # 精确命中的命令始终处理
    if text == "查角色":
        reply = await router.check_role(sender_id, is_private=True)
        await _reply_private(ctx, reply)
        return MsgSignal.BLOCK_ALL

    # 夜晚命令：玩家在游戏中，或未在游戏中时给出提示
    night_match = re.match(r"^(办掉|验人|毒人|开枪)\s*(.*)$", text)
    if night_match:
        if not in_game:
            await _reply_private(ctx, "❌ 你没有参与任何游戏！")
            return MsgSignal.BLOCK_ALL
        target_str = night_match.group(2).strip() or "".join(_extract_at_ids(text))
        if night_match.group(1) == "办掉":
            reply = await router.werewolf_kill(sender_id, target_str)
        elif night_match.group(1) == "验人":
            reply = await router.seer_check(sender_id, target_str)
        elif night_match.group(1) == "毒人":
            reply = await router.witch_poison(sender_id, target_str)
        else:
            reply = await router.hunter_shoot(sender_id, target_str)
        await _reply_private(ctx, reply)
        return MsgSignal.BLOCK_ALL

    if text in ("救人", "不操作"):
        if not in_game:
            await _reply_private(ctx, "❌ 你没有参与任何游戏！")
            return MsgSignal.BLOCK_ALL
        if text == "救人":
            reply = await router.witch_save(sender_id)
        else:
            reply = await router.witch_pass(sender_id)
        await _reply_private(ctx, reply)
        return MsgSignal.BLOCK_ALL

    wolf_chat_match = re.match(r"^密谋\s*(.*)$", text, re.DOTALL)
    if wolf_chat_match:
        if not in_game:
            await _reply_private(ctx, "❌ 你没有参与任何游戏！")
            return MsgSignal.BLOCK_ALL
        reply = await router.werewolf_chat(sender_id, wolf_chat_match.group(1))
        await _reply_private(ctx, reply)
        return MsgSignal.BLOCK_ALL

    return None


async def _reply_private(ctx: AgentCtx, text: str) -> None:
    """私聊回复（优先走原聊天通道，跨聊天不可用时走 OneBot 私聊）"""
    if not text:
        return
    try:
        await ctx.send_text(text, record=False)
    except Exception:
        from .onebot import send_private_text
        _, user_id = extract_chat_type_and_id(ctx.chat_key)
        if user_id:
            await send_private_text(user_id, text)


@plugin.mount_on_user_message()
async def on_user_message(ctx: AgentCtx, message: ChatMessage) -> Optional[MsgSignal]:
    """拦截狼人杀游戏命令与发言，避免触发 AI 推理"""
    try:
        # 仅处理 OneBot V11 平台消息
        if message.adapter_key and message.adapter_key != "onebot_v11":
            return None

        chat_type, _ = extract_chat_type_and_id(message.chat_key)
        if chat_type == "group":
            return await _handle_group_message(ctx, message)
        if chat_type == "private":
            return await _handle_private_message(ctx, message)
        return None
    except Exception:
        logger.exception("[狼人杀] 处理消息失败")
        return None


# ==================== Nekro 命令系统注册 ====================
#
# 与消息钩子共享 GameRouter 逻辑；带前缀（如 /创建房间）的消息会先被
# Nekro 命令系统消费，不会重复进入消息钩子。


def _group_id_from_context(context: CommandExecutionContext) -> str:
    _, group_id = extract_chat_type_and_id(context.chat_key)
    return group_id


@plugin.mount_command(
    name="create_room",
    aliases=["创建房间"],
    description="创建狼人杀游戏房间",
    permission=CommandPermission.PUBLIC,
    usage="create_room",
    category="狼人杀",
)
async def create_room_command(context: CommandExecutionContext) -> CommandResponse:
    router = get_router()
    reply = await router.create_room(
        _group_id_from_context(context), str(context.user_id), context.username
    )
    return CmdCtl.success(reply) if reply else CmdCtl.failed(reply or "创建失败")


@plugin.mount_command(
    name="join_room",
    aliases=["加入房间", "加入", "加人房间", "加人"],
    description="加入当前群的狼人杀游戏",
    permission=CommandPermission.PUBLIC,
    usage="join_room",
    category="狼人杀",
)
async def join_room_command(context: CommandExecutionContext) -> CommandResponse:
    router = get_router()
    reply = await router.join_room(
        _group_id_from_context(context), str(context.user_id), context.username
    )
    return CmdCtl.success(reply) if reply else CmdCtl.failed(reply or "加入失败")


@plugin.mount_command(
    name="start_game",
    aliases=["开始游戏"],
    description="开始狼人杀游戏（房主专用，需人齐）",
    permission=CommandPermission.PUBLIC,
    usage="start_game",
    category="狼人杀",
)
async def start_game_command(context: CommandExecutionContext) -> CommandResponse:
    router = get_router()
    reply = await router.start_game(_group_id_from_context(context), str(context.user_id))
    return CmdCtl.success(reply) if reply else CmdCtl.success("游戏开始！")


@plugin.mount_command(
    name="end_game",
    aliases=["结束游戏"],
    description="强制结束当前群的狼人杀游戏（房主专用）",
    permission=CommandPermission.PUBLIC,
    usage="end_game",
    category="狼人杀",
)
async def end_game_command(context: CommandExecutionContext) -> CommandResponse:
    router = get_router()
    reply = await router.end_game(_group_id_from_context(context), str(context.user_id))
    return CmdCtl.success(reply) if reply else CmdCtl.failed("操作失败")


@plugin.mount_command(
    name="werewolf_status",
    aliases=["游戏状态"],
    description="查看当前群的狼人杀游戏状态",
    permission=CommandPermission.PUBLIC,
    usage="werewolf_status",
    category="狼人杀",
)
async def status_command(context: CommandExecutionContext) -> CommandResponse:
    router = get_router()
    return CmdCtl.success(await router.show_status(_group_id_from_context(context)))


@plugin.mount_command(
    name="werewolf_help",
    aliases=["狼人杀帮助"],
    description="查看狼人杀游戏帮助",
    permission=CommandPermission.PUBLIC,
    usage="werewolf_help",
    category="狼人杀",
)
async def help_command(context: CommandExecutionContext) -> CommandResponse:
    router = get_router()
    reply = await router.show_help(_group_id_from_context(context))
    return CmdCtl.success(reply) if reply else CmdCtl.success("帮助已发送")


@plugin.mount_command(
    name="ai_join",
    aliases=["AI加入"],
    description="添加 AI 玩家到狼人杀房间（也可直接发送 名字加入）",
    permission=CommandPermission.PUBLIC,
    usage="ai_join <名字>",
    category="狼人杀",
)
async def ai_join_command(
    context: CommandExecutionContext,
    name: Annotated[str, Arg("AI 玩家名称，例如：小咪")] = "",
) -> CommandResponse:
    cfg = get_config()
    if getattr(cfg, "ai_join_need_admin", False) and not (
        context.is_super_user or context.is_advanced_user
    ):
        return CmdCtl.failed("⚠️ 只有管理员才能添加 AI 玩家！")
    router = get_router()
    reply = await router.ai_join_room(_group_id_from_context(context), name)
    return CmdCtl.success(reply) if reply else CmdCtl.failed(reply or "添加失败")


@plugin.mount_command(
    name="kick_ai",
    aliases=["踢出AI"],
    description="将 AI 玩家移出狼人杀房间",
    permission=CommandPermission.PUBLIC,
    usage="kick_ai <名字>",
    category="狼人杀",
)
async def kick_ai_command(
    context: CommandExecutionContext,
    name: Annotated[str, Arg("要移除的 AI 玩家名称", greedy=True)] = "",
) -> CommandResponse:
    router = get_router()
    reply = await router.kick_ai_player(_group_id_from_context(context), name)
    return CmdCtl.success(reply) if reply else CmdCtl.failed(reply or "移除失败")


@plugin.mount_command(
    name="day_vote",
    aliases=["投票"],
    description="白天投票放逐玩家（可用编号或 @玩家）",
    permission=CommandPermission.PUBLIC,
    usage="day_vote <编号|@玩家>",
    category="狼人杀",
)
async def day_vote_command(
    context: CommandExecutionContext,
    target: Annotated[str, Arg("投票目标：编号或 @玩家")] = "",
) -> CommandResponse:
    router = get_router()
    reply = await router.day_vote(
        _group_id_from_context(context), str(context.user_id), target.strip()
    )
    return CmdCtl.success(reply) if reply else CmdCtl.failed(reply or "投票失败")


@plugin.mount_command(
    name="finish_speaking",
    aliases=["发言完毕"],
    description="结束当前发言，轮到下一位",
    permission=CommandPermission.PUBLIC,
    usage="finish_speaking",
    category="狼人杀",
)
async def finish_speaking_command(context: CommandExecutionContext) -> CommandResponse:
    router = get_router()
    reply = await router.finish_speaking(
        _group_id_from_context(context), str(context.user_id)
    )
    return CmdCtl.success(reply) if reply else CmdCtl.failed(reply or "操作失败")


@plugin.mount_command(
    name="finish_last_words",
    aliases=["遗言完毕"],
    description="结束遗言",
    permission=CommandPermission.PUBLIC,
    usage="finish_last_words",
    category="狼人杀",
)
async def finish_last_words_command(context: CommandExecutionContext) -> CommandResponse:
    router = get_router()
    reply = await router.finish_last_words(
        _group_id_from_context(context), str(context.user_id)
    )
    return CmdCtl.success(reply) if reply else CmdCtl.failed(reply or "操作失败")


@plugin.mount_command(
    name="start_vote",
    aliases=["开始投票"],
    description="跳过发言环节直接进入投票（房主专用）",
    permission=CommandPermission.PUBLIC,
    usage="start_vote",
    category="狼人杀",
)
async def start_vote_command(context: CommandExecutionContext) -> CommandResponse:
    router = get_router()
    reply = await router.start_vote(_group_id_from_context(context), str(context.user_id))
    return CmdCtl.success(reply) if reply else CmdCtl.failed(reply or "操作失败")


@plugin.mount_command(
    name="check_role",
    aliases=["查角色"],
    description="查看自己的狼人杀角色（私聊使用）",
    permission=CommandPermission.PUBLIC,
    usage="check_role",
    category="狼人杀",
)
async def check_role_command(context: CommandExecutionContext) -> CommandResponse:
    router = get_router()
    reply = await router.check_role(str(context.user_id), is_private=True)
    return CmdCtl.success(reply) if reply else CmdCtl.failed(reply or "查询失败")


@plugin.mount_command(
    name="wolf_kill",
    aliases=["办掉"],
    description="狼人夜晚刀人（私聊使用）",
    permission=CommandPermission.PUBLIC,
    usage="wolf_kill <编号>",
    category="狼人杀",
)
async def wolf_kill_command(
    context: CommandExecutionContext,
    target: Annotated[str, Arg("目标玩家编号")] = "",
) -> CommandResponse:
    router = get_router()
    reply = await router.werewolf_kill(str(context.user_id), target.strip())
    return CmdCtl.success(reply) if reply else CmdCtl.failed(reply or "操作失败")


@plugin.mount_command(
    name="wolf_chat",
    aliases=["密谋"],
    description="狼人与队友密谋交流（私聊使用）",
    permission=CommandPermission.PUBLIC,
    usage="wolf_chat <消息>",
    category="狼人杀",
)
async def wolf_chat_command(
    context: CommandExecutionContext,
    message: Annotated[str, Arg("要发送给狼队友的消息", greedy=True)] = "",
) -> CommandResponse:
    router = get_router()
    reply = await router.werewolf_chat(str(context.user_id), message)
    return CmdCtl.success(reply) if reply else CmdCtl.failed(reply or "发送失败")


@plugin.mount_command(
    name="seer_check",
    aliases=["验人"],
    description="预言家查验玩家身份（私聊使用）",
    permission=CommandPermission.PUBLIC,
    usage="seer_check <编号>",
    category="狼人杀",
)
async def seer_check_command(
    context: CommandExecutionContext,
    target: Annotated[str, Arg("目标玩家编号")] = "",
) -> CommandResponse:
    router = get_router()
    reply = await router.seer_check(str(context.user_id), target.strip())
    return CmdCtl.success(reply) if reply else CmdCtl.failed(reply or "操作失败")


@plugin.mount_command(
    name="witch_save",
    aliases=["救人"],
    description="女巫使用解药救今晚被杀的玩家（私聊使用）",
    permission=CommandPermission.PUBLIC,
    usage="witch_save",
    category="狼人杀",
)
async def witch_save_command(context: CommandExecutionContext) -> CommandResponse:
    router = get_router()
    reply = await router.witch_save(str(context.user_id))
    return CmdCtl.success(reply) if reply else CmdCtl.failed(reply or "操作失败")


@plugin.mount_command(
    name="witch_poison",
    aliases=["毒人"],
    description="女巫使用毒药毒杀玩家（私聊使用）",
    permission=CommandPermission.PUBLIC,
    usage="witch_poison <编号>",
    category="狼人杀",
)
async def witch_poison_command(
    context: CommandExecutionContext,
    target: Annotated[str, Arg("目标玩家编号")] = "",
) -> CommandResponse:
    router = get_router()
    reply = await router.witch_poison(str(context.user_id), target.strip())
    return CmdCtl.success(reply) if reply else CmdCtl.failed(reply or "操作失败")


@plugin.mount_command(
    name="witch_pass",
    aliases=["不操作"],
    description="女巫今晚不使用道具（私聊使用）",
    permission=CommandPermission.PUBLIC,
    usage="witch_pass",
    category="狼人杀",
)
async def witch_pass_command(context: CommandExecutionContext) -> CommandResponse:
    router = get_router()
    reply = await router.witch_pass(str(context.user_id))
    return CmdCtl.success(reply) if reply else CmdCtl.failed(reply or "操作失败")


@plugin.mount_command(
    name="hunter_shoot",
    aliases=["开枪"],
    description="猎人开枪带走一名玩家（私聊使用）",
    permission=CommandPermission.PUBLIC,
    usage="hunter_shoot <编号>",
    category="狼人杀",
)
async def hunter_shoot_command(
    context: CommandExecutionContext,
    target: Annotated[str, Arg("目标玩家编号")] = "",
) -> CommandResponse:
    router = get_router()
    reply = await router.hunter_shoot(str(context.user_id), target.strip())
    return CmdCtl.success(reply) if reply else CmdCtl.failed(reply or "操作失败")
