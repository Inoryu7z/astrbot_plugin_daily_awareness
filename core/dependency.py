"""
依赖管理模块
负责检查和管理与其他插件的依赖关系
"""

import inspect
from typing import Optional, Any
from astrbot.api import logger


class DependencyManager:
    """依赖管理器"""

    DAYFLOW_PLUGIN_NAMES = {
        "astrbot_plugin_life_scheduler",
        "astrbot_plugin_dayflow_life_scheduler",
    }

    def __init__(self, context):
        self.context = context
        self._has_life_scheduler: Optional[bool] = None
        self._has_livingmemory: Optional[bool] = None
        self._life_scheduler_instance = None
        self._livingmemory_instance = None

    def _extract_star_instance(self, star_metadata):
        for attr in ("star", "instance", "plugin", "obj", "star_cls"):
            value = getattr(star_metadata, attr, None)
            if value is not None:
                return value
        return None

    def check_dependencies(self) -> dict:
        """检查依赖插件状态"""
        result = {"life_scheduler": False, "livingmemory": False}

        try:
            for star_metadata in self.context.get_all_stars():
                star_name = str(getattr(star_metadata, "name", "") or "").strip()
                star_instance = self._extract_star_instance(star_metadata)

                if star_name in self.DAYFLOW_PLUGIN_NAMES:
                    result["life_scheduler"] = True
                    self._life_scheduler_instance = star_instance
                    logger.info(f"[DayMind] 检测到日程插件 {star_name}，将获取日程数据")

                elif star_name == "astrbot_plugin_livingmemory":
                    result["livingmemory"] = True
                    self._livingmemory_instance = star_instance
                    logger.info("[DayMind] 检测到 livingmemory 插件，日记将存入记忆系统")

            self._has_life_scheduler = result["life_scheduler"]
            self._has_livingmemory = result["livingmemory"]

            if not result["life_scheduler"]:
                logger.info("[DayMind] 未检测到 Dayflow 日程插件，将仅基于对话进行思考")

            if not result["livingmemory"]:
                logger.info("[DayMind] 未检测到 livingmemory 插件，日记将仅本地存储")

        except Exception as e:
            logger.warning(f"[DayMind] 检查依赖插件时出错: {e}")

        return result

    @property
    def has_life_scheduler(self) -> bool:
        """是否存在日程插件"""
        if self._has_life_scheduler is None:
            self.check_dependencies()
        return bool(self._has_life_scheduler)

    @property
    def has_livingmemory(self) -> bool:
        """是否存在 livingmemory 插件"""
        if self._has_livingmemory is None:
            self.check_dependencies()
        return bool(self._has_livingmemory)

    async def get_schedule_data(
        self,
        session_id: str | None = None,
        persona_name: str | None = None,
        debug: bool = False,
    ) -> dict:
        """获取日程数据"""
        if not self.has_life_scheduler:
            if debug:
                logger.info("[DayMind][debug] get_schedule_data: 未检测到日程插件")
            return {}

        try:
            target = self._life_scheduler_instance
            if target is None:
                self.check_dependencies()
                target = self._life_scheduler_instance

            if target and hasattr(target, "get_life_context"):
                data = await target.get_life_context(session_id=session_id, persona_name=persona_name)
                data = data if isinstance(data, dict) else {}
                if debug:
                    logger.info(
                        f"[DayMind][debug] get_schedule_data success: session={session_id}, persona={persona_name}, "
                        f"outfit={str(data.get('outfit', ''))[:120]}, schedule={str(data.get('schedule', ''))[:300]}"
                    )
                return data

            if debug:
                logger.info("[DayMind][debug] get_schedule_data: 日程插件存在但未找到 get_life_context 接口")
        except Exception as e:
            logger.warning(f"[DayMind] 获取日程数据失败: {e}")

        return {}

    def get_memory_engine(self):
        """获取 livingmemory 的 memory_engine"""
        if not self.has_livingmemory:
            return None

        try:
            if self._livingmemory_instance:
                if hasattr(self._livingmemory_instance, "initializer"):
                    initializer = self._livingmemory_instance.initializer
                    if initializer and hasattr(initializer, "memory_engine"):
                        return initializer.memory_engine
        except Exception as e:
            logger.warning(f"[DayMind] 获取 memory_engine 失败: {e}")

        return None

    async def _maybe_await(self, value):
        if inspect.isawaitable(value):
            return await value
        return value

    def _extract_persona_name_from_obj(self, persona_obj) -> str | None:
        if not persona_obj:
            return None
        if isinstance(persona_obj, dict):
            return persona_obj.get("name") or persona_obj.get("persona_id") or persona_obj.get("id")
        for attr in ("name", "persona_id", "id"):
            if hasattr(persona_obj, attr):
                value = getattr(persona_obj, attr, None)
                if value:
                    return value
        return None

    def _extract_persona_desc_from_obj(self, persona_obj) -> str | None:
        if not persona_obj:
            return None
        if isinstance(persona_obj, dict):
            for key in ("system_prompt", "prompt", "persona_desc", "description", "content"):
                value = persona_obj.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return None

        for attr in ("system_prompt", "prompt", "persona_desc", "description", "content"):
            if hasattr(persona_obj, attr):
                value = getattr(persona_obj, attr, None)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    async def resolve_persona_context(self, session_id: str | None = None) -> dict:
        """尽力解析会话当前人格上下文（ID/名称/描述）"""
        result = {
            "persona_id": None,
            "persona_name": None,
            "persona_desc": None,
        }

        if not session_id:
            return result

        persona_id = None
        try:
            conv_mgr = getattr(self.context, "conversation_manager", None)
            if conv_mgr:
                curr_cid = await conv_mgr.get_curr_conversation_id(session_id)
                if curr_cid:
                    conversation = await conv_mgr.get_conversation(session_id, curr_cid)
                    persona_id = getattr(conversation, "persona_id", None) if conversation else None
                    if persona_id:
                        result["persona_id"] = persona_id
        except Exception as e:
            logger.debug(f"[DayMind] 从 conversation_manager 获取 persona_id 失败: {e}")

        persona_mgr = getattr(self.context, "persona_manager", None)
        if not persona_mgr:
            return result

        try:
            persona_obj = None
            if persona_id and hasattr(persona_mgr, "get_persona"):
                persona_obj = await self._maybe_await(persona_mgr.get_persona(persona_id))

            if not persona_obj and hasattr(persona_mgr, "get_default_persona_v3"):
                persona_obj = await persona_mgr.get_default_persona_v3(session_id)

            result["persona_name"] = self._extract_persona_name_from_obj(persona_obj) or persona_id
            result["persona_desc"] = self._extract_persona_desc_from_obj(persona_obj)
            if not result["persona_id"]:
                result["persona_id"] = persona_id or result["persona_name"]
        except Exception as e:
            logger.debug(f"[DayMind] 解析人格上下文失败: {e}")

        return result

    async def resolve_persona_id(self, session_id: str | None = None) -> str | None:
        """尽力解析会话当前人格 ID"""
        ctx = await self.resolve_persona_context(session_id)
        return ctx.get("persona_id") or ctx.get("persona_name")

    async def store_to_memory(
        self,
        date_str: str,
        content: str,
        session_id: str | None = None,
        persona_id: str | None = None,
        metadata: dict | None = None,
    ) -> bool:
        """
        将完整日记存入 livingmemory 记忆系统
        """
        memory_engine = self.get_memory_engine()
        if not memory_engine:
            logger.debug("[DayMind] memory_engine 不可用")
            return False

        try:
            resolved_persona_id = persona_id or await self.resolve_persona_id(session_id)

            final_metadata = {
                "type": "diary",
                "date": date_str,
                "source": "daymind",
                "status": "active",
            }
            if metadata:
                final_metadata.update(metadata)

            await memory_engine.add_memory(
                content=content,
                session_id=session_id,
                persona_id=resolved_persona_id,
                importance=0.7,
                metadata=final_metadata,
            )

            logger.info(
                f"[DayMind] 日记已存入记忆系统: {date_str}, "
                f"session_id={session_id}, persona_id={resolved_persona_id}"
            )
            return True

        except Exception as e:
            logger.error(f"[DayMind] 存入 livingmemory 失败: {e}", exc_info=True)
            return False

    async def mark_daymind_diary_memories_deleted(
        self,
        date_str: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """将指定日期的 DayMind diary memory 标记为已删除，而非物理删除。"""
        memory_engine = self.get_memory_engine()
        result = {"matched": 0, "updated": 0, "ids": []}
        if not memory_engine or not session_id:
            return result

        try:
            memories = await memory_engine.get_session_memories(session_id, limit=1000)
            for memory in memories:
                metadata = memory.get("metadata") or {}
                if not isinstance(metadata, dict):
                    continue
                if metadata.get("type") != "diary":
                    continue
                if metadata.get("source") != "daymind":
                    continue
                if str(metadata.get("date") or "").strip() != date_str:
                    continue
                if str(metadata.get("status") or "active").strip() == "deleted":
                    continue

                result["matched"] += 1
                updates = {
                    "metadata": {
                        "status": "deleted",
                        "deleted": True,
                        "deleted_by": "daymind_regeneration",
                        "deleted_at": __import__("time").time(),
                    }
                }
                success = await memory_engine.update_memory(memory["id"], updates)
                if success:
                    result["updated"] += 1
                    result["ids"].append(memory["id"])

            if result["updated"]:
                logger.info(
                    f"[DayMind] 已将旧日记记忆标记删除: date={date_str}, "
                    f"session_id={session_id}, updated={result['updated']}"
                )
        except Exception as e:
            logger.error(f"[DayMind] 标记旧日记记忆删除失败: {e}", exc_info=True)

        return result
