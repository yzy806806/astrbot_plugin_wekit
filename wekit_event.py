# -*- coding: utf-8 -*-

"""WeKit 平台的消息事件类。

AstrBot 的核心处理完（LLM / 插件）后调用 event.send(...) 回复，
这里转交给 adapter.send_by_session，由适配器 POST 到手机 API。
"""

from typing import TYPE_CHECKING

from astrbot import logger
from astrbot.api.event import MessageChain
# MessageChain 在 astrbot.api.event 里导出，Plain 等消息组件在 message_components
from astrbot.api.message_components import Plain
from astrbot.api.platform import (AstrBotMessage, AstrMessageEvent,
                                  PlatformMetadata)

if TYPE_CHECKING:
    from .wekit_adapter import WeKitAdapter


class WeKitEvent(AstrMessageEvent):
    adapter: "WeKitAdapter"

    def __init__(self, message_obj: AstrBotMessage,
                 platform_meta: PlatformMetadata,
                 adapter_instance: "WeKitAdapter") -> None:
        session_id = getattr(message_obj, "session_id", None) or "unknown_session"
        super().__init__(
            message_str=message_obj.message_str or "",
            message_obj=message_obj,
            platform_meta=platform_meta,
            session_id=session_id,
        )
        self.adapter = adapter_instance

    async def send(self, message_chain: MessageChain) -> None:
        logger.info(f"[WeKit] send() session={self.session}")
        if getattr(self, "adapter", None):
            await self.adapter.send_by_session(
                session=self.session, message_chain=message_chain)
        else:
            logger.error("[WeKit] adapter 未设置，无法发送")
        await super().send(message_chain)
