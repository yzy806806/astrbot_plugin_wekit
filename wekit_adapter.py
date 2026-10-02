# -*- coding: utf-8 -*-
"""
AstrBot 平台适配器：对接安卓端 WeKit(wcx) 的 REST API（**主动轮询模式**）。

    收消息  轮询  GET  {base}/api/conversations/{convId}/history?page-index=1&page-size=N
    发消息  调用  POST {base}/api/messages/text     {type, convId, content}

鉴权：Authorization: Bearer <token>

★ 重要：**不要在插件里做白名单/冷却/唤醒判断**
AstrBot 的消息流水线已经提供了整套机制，读的都是 `platform_settings`（WebUI → 平台设置）：
    session_status_check → waking_check → whitelist_check → rate_limit_check
    → content_safety_check → preprocess → process(LLM) → result_decorate → respond
  - 白名单：`enable_id_white_list` / `id_whitelist` / `wl_ignore_admin_on_group` / `wl_ignore_admin_on_friend`
  - 唤醒：`wake_prefix` / `friend_message_needs_wake_prefix`
  - 限流：`rate_limit` / `rate_limit_strategy`
插件只要**如实上报消息**（设好 session_id / group_id / 消息类型）并**如实发送**即可，
过滤交给 AstrBot —— 这也是本插件支持**多会话**的原因：白名单里可以配多个会话 ID。

群聊/私聊区分：convId 以 `@chatroom` 结尾 → GROUP_MESSAGE，否则 FRIEND_MESSAGE。

已知限制（WCX REST API 的 history 不返回 msgSvrId）：
  - 无法引用回复
  - 无法获取图片内容（图片消息的 content 是 `<type:image>`）
  - 去重靠 `sender|content|type` 的内容指纹，同一人连发两条完全相同的文本会漏掉第二条
"""

import asyncio
import hashlib
import time
import uuid
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set

import aiohttp

from astrbot import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.platform import (AstrBotMessage, MessageMember, MessageType,
                                  Platform, PlatformMetadata,
                                  register_platform_adapter)
from astrbot.core.platform.platform import PlatformStatus

from .wekit_event import WeKitEvent

DEFAULT_CONFIG_TMPL = {
    "base_url": "http://127.0.0.1:30010",
    "token": "",
    "conv_ids": "",                       # 逗号分隔，可填多个会话
    "poll_interval": 5,
    "page_size": 10,
    # 本人在 WCX history 里的 sender 恒为 "<myself>"（无昵称、无 wxid）。
    # 这里指定一个固定昵称，让画像/学习/人格迭代都归到同一 ID 下。
    "self_nickname": "猫南北",
}


@register_platform_adapter(
    "wekit", "WeKit 微信适配器（主动轮询 REST API）",
    default_config_tmpl=DEFAULT_CONFIG_TMPL,
)
class WeKitAdapter(Platform):
    def __init__(self, platform_config: dict, platform_settings: dict,
                 event_queue: asyncio.Queue) -> None:
        # Platform 基类只接 (config, event_queue)，platform_settings 由管理器传入
        super().__init__(platform_config, event_queue)
        self.platform_settings = platform_settings

        self.base_url: str = str(platform_config.get(
            "base_url", "http://127.0.0.1:30010")).rstrip("/")
        self.token: str = platform_config.get("token", "")
        self.poll_interval: float = float(platform_config.get("poll_interval", 5))
        self.page_size: int = int(platform_config.get("page_size", 10))
        # 本人发言在 WCX 里只有 "<myself>"，映射到这个昵称
        self.self_nickname: str = str(
            platform_config.get("self_nickname") or "猫南北").strip() or "猫南北"

        # 多个会话：逗号分隔（兼容旧的 conv_id 单值配置）
        raw = platform_config.get("conv_ids") or platform_config.get("conv_id") or ""
        self.conv_ids: List[str] = [
            s.strip() for s in str(raw).split(",") if s.strip()]

        self.metadata = PlatformMetadata(
            name="wekit",
            description="WeKit 微信适配器（主动轮询）",
            id=platform_config.get("id", "wekit"),
        )

        # 每个会话独立的"已见消息"指纹（history 无 msgSvrId，只能靠内容指纹判新）
        self._seen: Dict[str, Deque[str]] = {}
        self._seen_set: Dict[str, Set[str]] = {}

        self._session: Optional[aiohttp.ClientSession] = None
        self._stop = asyncio.Event()

        if not self.token:
            logger.warning("[WeKit] 未配置 token，若手机 API 启用了鉴权会 401")
        if not self.conv_ids:
            logger.warning("[WeKit] 未配置 conv_ids（目标会话），适配器不会有消息")

    # ---------- Platform 接口 ----------
    def meta(self) -> PlatformMetadata:
        return self.metadata

    async def run(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30))
        logger.info(f"[WeKit] 启动轮询：{self.base_url} "
                    f"会话={self.conv_ids} 间隔={self.poll_interval}s")

        # 状态跟随基类生命周期，避免 terminate 后仍显示 running
        self.status = PlatformStatus.RUNNING

        for conv in self.conv_ids:
            self._seen.setdefault(conv, deque(maxlen=500))
            self._seen_set.setdefault(conv, set())
        await self._prime()          # 建立基线：已有消息不补发
        try:
            while not self._stop.is_set():
                try:
                    await self._poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    logger.error(f"[WeKit] 轮询异常: {e}")
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            logger.info("[WeKit] 轮询已停止")
        finally:
            if self._session:
                await self._session.close()

    async def send_by_session(self, session, message_chain: MessageChain) -> None:
        text = self._chain_to_text(message_chain)
        if not text:
            return
        conv_id = getattr(session, "session_id", None) or str(session)
        ok = await self._send_text(conv_id, text)
        logger.info(f"[WeKit] 发送{'成功' if ok else '失败'} -> {conv_id}: {text[:40]}...")

    # ---------- 内部 ----------
    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def _fetch_history(self, conv_id: str) -> List[Dict[str, Any]]:
        """拉指定会话的最近消息，返回按**时间正序**（旧→新）。"""
        url = (f"{self.base_url}/api/conversations/{conv_id}/history"
               f"?page-index=1&page-size={self.page_size}")
        async with self._session.get(url, headers=self._headers()) as resp:
            if resp.status != 200:
                logger.error(f"[WeKit] history {conv_id} 返回 {resp.status}")
                return []
            data = await resp.json(content_type=None)
        msgs = data if isinstance(data, list) else (
            data.get("data") or data.get("messages") or [])
        return list(reversed(msgs))     # API 倒序（新在前），反转为正序

    @staticmethod
    def _fingerprint(m: Dict[str, Any]) -> str:
        raw = f"{m.get('sender','')}|{m.get('content','')}|{m.get('type','')}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def _mark(self, conv_id: str, m: Dict[str, Any]) -> None:
        fp = self._fingerprint(m)
        if fp in self._seen_set[conv_id]:
            return
        self._seen[conv_id].append(fp)
        self._seen_set[conv_id].add(fp)

    async def _prime(self) -> None:
        for conv in self.conv_ids:
            for m in await self._fetch_history(conv):
                self._mark(conv, m)
            logger.info(f"[WeKit] 基线 {conv}: {len(self._seen[conv])} 条（不补发）")

    async def _poll_once(self) -> None:
        for conv in self.conv_ids:
            for m in await self._fetch_history(conv):
                fp = self._fingerprint(m)
                if fp in self._seen_set[conv]:
                    continue
                self._mark(conv, m)
                # 注意：不过滤 <myself>。人格自迭代需要采集本人的发言来学习
                # 说话风格，self 回声由 Iris 的 collector / learning 模块负责
                # （它们自己会跳过 self_id 与 bot 消息）。
                abm = self.convert_message(conv, m)
                if abm is None:
                    continue
                # ★ 过滤（白名单/唤醒/限流）交给 AstrBot 流水线，这里原样上报
                self._event_queue.put_nowait(WeKitEvent(abm, self.meta(), self))

    def convert_message(self, conv_id: str,
                        data: Dict[str, Any]) -> Optional[AstrBotMessage]:
        content = str(data.get("content") or "")
        raw_sender = data.get("sender")
        sender = "" if raw_sender is None else str(raw_sender)
        # WCX 对本人发言只给字面量 "<myself>"，没有昵称也没有 wxid。
        # 换成固定昵称，让画像 / learning / 人格自迭代都归到同一个 ID 下，
        # 而不是产生一个叫 "<myself>" 或 "unknown" 的虚拟用户。
        if sender.strip() in ("", "<myself>", "<self>"):
            sender = self.self_nickname
        if not content:
            return None

        abm = AstrBotMessage()
        abm.message = [Plain(text=content)]
        abm.message_str = content
        abm.raw_message = data
        abm.message_id = str(uuid.uuid4())
        abm.self_id = self.metadata.id
        # AstrBotMessage 自带 timestamp 初始化，但用消息自身时间戳更准
        try:
            abm.timestamp = int(data.get("timestamp") or time.time())
        except (TypeError, ValueError):
            abm.timestamp = int(time.time())

        is_group = conv_id.endswith("@chatroom")
        abm.type = (MessageType.GROUP_MESSAGE if is_group
                    else MessageType.FRIEND_MESSAGE)
        abm.sender = MessageMember(user_id=sender, nickname=sender)
        if is_group:
            # group_id setter 会自动建 Group 对象
            abm.group_id = conv_id
            if abm.group is not None:
                abm.group.group_name = data.get("groupName") or conv_id
        # AstrBot 的 session 由 AstrMessageEvent 内部构造（platform:type:session_id），
        # 这里保留 message_obj.session_id 供调试与兼容旧代码
        abm.session_id = conv_id
        return abm

    async def _send_text(self, conv_id: str, text: str) -> bool:
        url = f"{self.base_url}/api/messages/text"
        body = {"type": "text", "convId": conv_id, "content": text}
        try:
            async with self._session.post(
                    url, json=body, headers=self._headers()) as resp:
                if resp.status == 200:
                    return True
                logger.error(f"[WeKit] 发送失败 {resp.status}: "
                             f"{(await resp.text())[:150]}")
                return False
        except Exception as e:  # noqa: BLE001
            logger.error(f"[WeKit] 发送异常: {e}")
            return False

    @staticmethod
    def _chain_to_text(chain: MessageChain) -> str:
        parts = []
        for comp in getattr(chain, "chain", None) or []:
            if isinstance(comp, Plain):
                parts.append(comp.text)
            else:
                parts.append(f"[{type(comp).__name__}]")
        return "".join(parts).strip()

    async def terminate(self) -> None:
        self._stop.set()
