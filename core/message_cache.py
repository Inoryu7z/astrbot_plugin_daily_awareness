"""
消息缓存模块
负责缓存最近的对话消息，供思考时参考
"""

import asyncio
import time
from collections import deque
from typing import Optional
from dataclasses import dataclass, field

from astrbot.api import logger


@dataclass
class CachedMessage:
    """缓存的消息"""
    role: str  # "user" 或 "assistant"
    content: str
    timestamp: float = field(default_factory=time.time)
    session_id: Optional[str] = None
    sender_id: Optional[str] = None
    sender_name: Optional[str] = None
    group_id: Optional[str] = None


class MessageCache:
    """消息缓存管理器"""

    def __init__(self, max_rounds: int = 10):
        self.max_rounds = max_rounds
        self._cache: dict[str, deque] = {}
        self._lock = asyncio.Lock()

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        sender_id: str | None = None,
        sender_name: str | None = None,
        group_id: str | None = None,
    ):
        """添加消息到缓存"""
        async with self._lock:
            if session_id not in self._cache:
                self._cache[session_id] = deque(maxlen=self.max_rounds * 2)

            self._cache[session_id].append(
                CachedMessage(
                    role=role,
                    content=content,
                    timestamp=time.time(),
                    session_id=session_id,
                    sender_id=str(sender_id).strip() if sender_id else None,
                    sender_name=str(sender_name).strip() if sender_name else None,
                    group_id=str(group_id).strip() if group_id else None,
                )
            )

            logger.debug(
                f"[MessageCache] 添加消息: session={session_id}, role={role}, sender_id={sender_id}, sender_name={sender_name}"
            )

    async def get_recent_messages(self, session_id: str, rounds: int = 2) -> list[str]:
        """获取最近的对话消息"""
        async with self._lock:
            if session_id not in self._cache:
                return []

            messages = list(self._cache[session_id])
            recent = messages[-(rounds * 2):] if rounds > 0 else []

            formatted = []
            for msg in recent:
                if msg.role == "user":
                    role_name = self._build_user_label(msg)
                else:
                    role_name = "我的回复"
                formatted.append(f"{role_name}: {msg.content}")

            return formatted

    async def get_latest_counterpart(self, session_id: str) -> dict:
        """获取当前会话最近的互动对象信息"""
        async with self._lock:
            if session_id not in self._cache:
                return {
                    "sender_id": None,
                    "sender_name": None,
                    "group_id": None,
                    "display_name": None,
                }

            messages = list(self._cache[session_id])
            for msg in reversed(messages):
                if msg.role != "user":
                    continue
                return {
                    "sender_id": msg.sender_id,
                    "sender_name": msg.sender_name,
                    "group_id": msg.group_id,
                    "display_name": self._resolve_display_name(msg.sender_id, msg.sender_name),
                }

            return {
                "sender_id": None,
                "sender_name": None,
                "group_id": None,
                "display_name": None,
            }

    async def get_all_session_ids(self) -> list[str]:
        """获取所有有缓存的会话ID"""
        async with self._lock:
            return list(self._cache.keys())

    async def get_most_recent_session_id(self) -> str | None:
        """获取最近活跃的会话ID"""
        async with self._lock:
            latest_session_id = None
            latest_timestamp = -1.0

            for session_id, messages in self._cache.items():
                if not messages:
                    continue
                last_msg = messages[-1]
                if last_msg.timestamp > latest_timestamp:
                    latest_timestamp = last_msg.timestamp
                    latest_session_id = session_id

            return latest_session_id

    def _resolve_display_name(self, sender_id: str | None, sender_name: str | None) -> str:
        if sender_name:
            return sender_name
        if sender_id:
            return f"当前对象(ID:{sender_id})"
        return "当前对象"

    def _build_user_label(self, msg: CachedMessage) -> str:
        display = self._resolve_display_name(msg.sender_id, msg.sender_name)
        if msg.sender_id and msg.sender_name:
            return f"{display}(ID:{msg.sender_id})"
        return display

    def get_state(self) -> dict:
        """获取当前状态（用于持久化）"""
        state = {}
        for session_id, messages in self._cache.items():
            state[session_id] = [
                {
                    "role": msg.role,
                    "content": msg.content,
                    "timestamp": msg.timestamp,
                    "session_id": msg.session_id,
                    "sender_id": msg.sender_id,
                    "sender_name": msg.sender_name,
                    "group_id": msg.group_id,
                }
                for msg in messages
            ]
        return state

    def restore_state(self, state: dict):
        """从状态数据恢复"""
        self._cache.clear()
        for session_id, messages in state.items():
            self._cache[session_id] = deque(maxlen=self.max_rounds * 2)
            for msg_data in messages:
                self._cache[session_id].append(
                    CachedMessage(
                        role=msg_data["role"],
                        content=msg_data["content"],
                        timestamp=msg_data.get("timestamp", time.time()),
                        session_id=msg_data.get("session_id"),
                        sender_id=msg_data.get("sender_id"),
                        sender_name=msg_data.get("sender_name"),
                        group_id=msg_data.get("group_id"),
                    )
                )
        logger.info(f"[MessageCache] 恢复了 {len(self._cache)} 个会话的缓存")
