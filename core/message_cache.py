"""
消息缓存模块
负责缓存最近的对话消息，供思考时参考
"""

import asyncio
import threading
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
        self._sync_lock = threading.Lock()

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
            session_key = str(session_id or "").strip()
            if not session_key:
                return
            if session_key not in self._cache:
                self._cache[session_key] = deque(maxlen=self.max_rounds * 2)

            self._cache[session_key].append(
                CachedMessage(
                    role=str(role or "assistant").strip() or "assistant",
                    content=str(content or "").strip(),
                    timestamp=time.time(),
                    session_id=session_key,
                    sender_id=str(sender_id).strip() if sender_id else None,
                    sender_name=str(sender_name).strip() if sender_name else None,
                    group_id=str(group_id).strip() if group_id else None,
                )
            )

            logger.debug(
                f"[MessageCache] 添加消息: session={session_key}, role={role}, sender_id={sender_id}, sender_name={sender_name}"
            )

    async def get_recent_messages(self, session_id: str, rounds: int = 2) -> list[str]:
        """获取最近的对话消息"""
        async with self._lock:
            session_key = str(session_id or "").strip()
            if session_key not in self._cache:
                return []

            messages = list(self._cache[session_key])
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
            session_key = str(session_id or "").strip()
            if session_key not in self._cache:
                return {
                    "sender_id": None,
                    "sender_name": None,
                    "group_id": None,
                    "display_name": None,
                }

            messages = list(self._cache[session_key])
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

    async def get_recent_session_ids(self) -> list[str]:
        """按最近活跃时间倒序返回会话 ID"""
        async with self._lock:
            pairs: list[tuple[str, float]] = []
            for session_id, messages in self._cache.items():
                if not messages:
                    continue
                pairs.append((session_id, messages[-1].timestamp))
            pairs.sort(key=lambda x: x[1], reverse=True)
            return [session_id for session_id, _ in pairs]

    async def get_most_recent_session_id(self) -> str | None:
        """获取最近活跃的会话ID"""
        session_ids = await self.get_recent_session_ids()
        return session_ids[0] if session_ids else None

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

    def get_state(self, allowed_session_ids: list[str] | set[str] | tuple[str, ...] | None = None, max_sessions: int | None = None) -> dict:
        """获取当前状态（用于持久化）。支持按会话过滤并限制数量。"""
        with self._sync_lock:
            allowed = None
            if allowed_session_ids is not None:
                allowed = {str(x).strip() for x in allowed_session_ids if str(x).strip()}

            pairs: list[tuple[str, float, deque]] = []
            for session_id, messages in self._cache.items():
                session_key = str(session_id or "").strip()
                if not session_key or not messages:
                    continue
                if allowed is not None and session_key not in allowed:
                    continue
                pairs.append((session_key, messages[-1].timestamp, messages))

            pairs.sort(key=lambda x: x[1], reverse=True)
            if max_sessions is not None and max_sessions > 0:
                pairs = pairs[:max_sessions]

            state = {}
            for session_key, _, messages in pairs:
                state[session_key] = [
                    {
                        "role": str(msg.role or "assistant"),
                        "content": str(msg.content or ""),
                        "timestamp": float(msg.timestamp or time.time()),
                        "session_id": msg.session_id,
                        "sender_id": msg.sender_id,
                        "sender_name": msg.sender_name,
                        "group_id": msg.group_id,
                    }
                    for msg in messages
                    if str(getattr(msg, "content", "") or "").strip()
                ]
            return state

    def restore_state(self, state: dict):
        """从状态数据恢复"""
        with self._sync_lock:
            self._cache.clear()
            if not isinstance(state, dict):
                logger.warning("[MessageCache] 恢复状态失败：state 不是 dict")
                return

            restored_sessions = 0
            skipped_messages = 0
            for session_id, messages in state.items():
                session_key = str(session_id or "").strip()
                if not session_key or not isinstance(messages, list):
                    continue
                queue = deque(maxlen=self.max_rounds * 2)
                for msg_data in messages:
                    if not isinstance(msg_data, dict):
                        skipped_messages += 1
                        continue
                    role = str(msg_data.get("role") or "assistant").strip() or "assistant"
                    if role not in {"user", "assistant"}:
                        role = "assistant"
                    content = str(msg_data.get("content") or "").strip()
                    if not content:
                        skipped_messages += 1
                        continue
                    try:
                        timestamp = float(msg_data.get("timestamp", time.time()) or time.time())
                    except Exception:
                        timestamp = time.time()
                    queue.append(
                        CachedMessage(
                            role=role,
                            content=content,
                            timestamp=timestamp,
                            session_id=str(msg_data.get("session_id") or session_key).strip() or session_key,
                            sender_id=str(msg_data.get("sender_id")).strip() if msg_data.get("sender_id") else None,
                            sender_name=str(msg_data.get("sender_name")).strip() if msg_data.get("sender_name") else None,
                            group_id=str(msg_data.get("group_id")).strip() if msg_data.get("group_id") else None,
                        )
                    )
                if queue:
                    self._cache[session_key] = queue
                    restored_sessions += 1

            logger.info(f"[MessageCache] 恢复了 {restored_sessions} 个会话的缓存，跳过坏消息 {skipped_messages} 条")
