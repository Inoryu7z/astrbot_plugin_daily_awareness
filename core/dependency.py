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
    PREFERRED_DAYFLOW_PLUGIN_NAME = "astrbot_plugin_dayflow_life_scheduler"
    LEGACY_DAYFLOW_PLUGIN_NAME = "astrbot_plugin_life_scheduler"

    def __init__(self, context):
        self.context = context
        self._has_life_scheduler: Optional[bool] = None
        self._has_livingmemory: Optional[bool] = None
        self._life_scheduler_instance = None
        self._livingmemory_instance = None

    def _extract_star_instances(self, star_metadata):
        instances = []
        seen_ids = set()
        for attr in ("star", "instance", "plugin", "obj", "star_cls"):
            value = getattr(star_metadata, attr, None)
            if value is None:
                continue
            marker = id(value)
            if marker in seen_ids:
                continue
            seen_ids.add(marker)
            instances.append(value)
        return instances

    def _is_valid_dayflow_instance(self, instance) -> bool:
        if instance is None:
            return False
        if hasattr(instance, "get_life_context"):
            return True
        service = getattr(instance, "service", None)
        if service is None:
            return False
        if hasattr(service, "get_life_context"):
            return True
        return hasattr(service, "generate_schedule") and hasattr(service, "save_generated")

    def _is_valid_livingmemory_instance(self, instance) -> bool:
        if instance is None:
            return False
        initializer = getattr(instance, "initializer", None)
        memory_engine = getattr(initializer, "memory_engine", None) if initializer else None
        return memory_engine is not None

    def check_dependencies(self) -> dict:
        """检查依赖插件状态"""
        result = {"life_scheduler": False, "livingmemory": False}
        self._life_scheduler_instance = None
        self._livingmemory_instance = None

        try:
            preferred_valid_instance = None
            legacy_valid_instance = None
            preferred_incomplete_detected = False
            legacy_incomplete_detected = False

            for star_metadata in self.context.get_all_stars():
                star_name = str(getattr(star_metadata, "name", "") or "").strip()
                star_instances = self._extract_star_instances(star_metadata)

                if star_name == self.PREFERRED_DAYFLOW_PLUGIN_NAME:
                    valid_candidate = next((item for item in star_instances if self._is_valid_dayflow_instance(item)), None)
                    if valid_candidate is not None:
                        preferred_valid_instance = valid_candidate
                    elif star_instances:
                        preferred_incomplete_detected = True
                    continue

                if star_name == self.LEGACY_DAYFLOW_PLUGIN_NAME:
                    valid_candidate = next((item for item in star_instances if self._is_valid_dayflow_instance(item)), None)
                    if valid_candidate is not None:
                        legacy_valid_instance = valid_candidate
                    elif star_instances:
                        legacy_incomplete_detected = True
                    continue

                if star_name == "astrbot_plugin_livingmemory":
                    valid_candidate = next((item for item in star_instances if self._is_valid_livingmemory_instance(item)), None)
                    if valid_candidate is not None:
                        result["livingmemory"] = True
                        self._livingmemory_instance = valid_candidate
                        logger.info("[DayMind] 检测到 livingmemory 插件，日记将存入记忆系统")
                    elif star_instances:
                        logger.warning("[DayMind] 检测到 livingmemory 插件但未找到可用 memory_engine，已跳过绑定")

            bound_name = None
            if preferred_valid_instance is not None:
                self._life_scheduler_instance = preferred_valid_instance
                result["life_scheduler"] = True
                bound_name = self.PREFERRED_DAYFLOW_PLUGIN_NAME
            elif legacy_valid_instance is not None:
                self._life_scheduler_instance = legacy_valid_instance
                result["life_scheduler"] = True
                bound_name = self.LEGACY_DAYFLOW_PLUGIN_NAME

            if bound_name:
                logger.info(f"[DayMind] 检测到日程插件 {bound_name}，将获取日程数据")

            if preferred_incomplete_detected and preferred_valid_instance is None and legacy_valid_instance is None:
                logger.warning(f"[DayMind] 检测到 {self.PREFERRED_DAYFLOW_PLUGIN_NAME} 但实例接口不完整，已跳过绑定")
            elif preferred_incomplete_detected and preferred_valid_instance is None and legacy_valid_instance is not None:
                logger.debug(
                    f"[DayMind] 首选日程插件 {self.PREFERRED_DAYFLOW_PLUGIN_NAME} 接口不完整，已回退绑定旧插件 {self.LEGACY_DAYFLOW_PLUGIN_NAME}"
                )

            if legacy_incomplete_detected and preferred_valid_instance is None and legacy_valid_instance is None:
                logger.debug(f"[DayMind] 兼容探测到旧插件 {self.LEGACY_DAYFLOW_PLUGIN_NAME} 但实例接口不完整，已跳过")

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

    def _is_missing_today_schedule(self, data: dict | None) -> bool:
        if not isinstance(data, dict) or not data:
            return True
        meta = data.get("meta") or {}
        outfit = str(data.get("outfit") or "").strip()
        schedule = str(data.get("schedule") or "").strip()
        fallback = bool(meta.get("fallback", False)) if isinstance(meta, dict) else False
        if fallback:
            return True
        if outfit in {"", "尚未生成"}:
            return True
        if not schedule:
            return True
        if "今日日程尚未生成成功" in schedule:
            return True
        return False

    async def get_schedule_data(
        self,
        session_id: str | None = None,
        persona_name: str | None = None,
        target_date: str | None = None,
        debug: bool = False,
    ) -> dict:
        """获取日程数据"""
        if not self.has_life_scheduler:
            self.check_dependencies()
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
                data = await target.get_life_context(session_id=session_id, persona_name=persona_name, target_date=target_date)
                data = data if isinstance(data, dict) else {}
                if debug:
                    logger.info(
                        f"[DayMind][debug] get_schedule_data success: session={session_id}, persona={persona_name}, target_date={target_date or ''}, "
                        f"outfit={str(data.get('outfit', ''))[:120]}, schedule={str(data.get('schedule', ''))[:300]}"
                    )
                return data

            service = getattr(target, "service", None) if target else None
            if service and hasattr(service, "get_life_context"):
                data = await service.get_life_context(session_id=session_id, persona_name=persona_name, target_date=target_date)
                data = data if isinstance(data, dict) else {}
                if debug:
                    logger.info(
                        f"[DayMind][debug] get_schedule_data success(service): session={session_id}, persona={persona_name}, target_date={target_date or ''}, "
                        f"outfit={str(data.get('outfit', ''))[:120]}, schedule={str(data.get('schedule', ''))[:300]}"
                    )
                return data

            if debug:
                logger.info("[DayMind][debug] get_schedule_data: 日程插件存在但未找到 get_life_context 接口")
        except Exception as e:
            logger.warning(f"[DayMind] 获取日程数据失败: {e}")

        return {}

    async def ensure_today_schedule(
        self,
        session_id: str | None = None,
        persona_name: str | None = None,
        persona_desc: str | None = None,
        target_date: str | None = None,
        debug: bool = False,
    ) -> dict[str, Any]:
        """确保当前人格存在目标日期日程；若缺失则尝试调用 Dayflow 自动补生成。"""
        result: dict[str, Any] = {
            "status": "failed",
            "message": "目标日期日程不可用",
            "data": {},
            "generated_now": False,
            "persona_name": persona_name,
            "target_date": target_date,
        }

        initial_data = await self.get_schedule_data(
            session_id=session_id,
            persona_name=persona_name,
            target_date=target_date,
            debug=debug,
        )
        if not self._is_missing_today_schedule(initial_data):
            result.update({
                "status": "existing",
                "message": "目标日期日程已存在",
                "data": initial_data,
                "generated_now": False,
            })
            return result

        if not self.has_life_scheduler:
            self.check_dependencies()
        if not self.has_life_scheduler:
            result["message"] = "未检测到可用的 Dayflow 日程实例，无法自动补生成目标日期日程"
            return result

        target = self._life_scheduler_instance
        if target is None:
            self.check_dependencies()
            target = self._life_scheduler_instance
        if target is None:
            result["message"] = "未获取到 Dayflow 插件实例，无法自动补生成目标日期日程"
            return result

        service = getattr(target, "service", None)
        if service is None:
            result["message"] = "Dayflow 插件未暴露 service，无法自动补生成目标日期日程"
            return result

        try:
            resolved_persona_name = persona_name
            resolved_persona_desc = persona_desc
            resolved_persona_id = None
            if session_id and (not resolved_persona_name or not resolved_persona_desc):
                persona_ctx = await self.resolve_persona_context(session_id)
                resolved_persona_name = resolved_persona_name or persona_ctx.get("persona_name") or persona_ctx.get("persona_id")
                resolved_persona_desc = resolved_persona_desc or persona_ctx.get("persona_desc")
                resolved_persona_id = persona_ctx.get("persona_id")

            store_key = service.normalize_persona_key(resolved_persona_name, resolved_persona_id)
            result["persona_name"] = resolved_persona_name or store_key

            if debug:
                logger.info(
                    f"[DayMind][debug] ensure_today_schedule start: session={session_id}, requested_persona={persona_name}, "
                    f"resolved_persona={resolved_persona_name}, store_key={store_key}, target_date={target_date or ''}"
                )

            ok = await service.enter_generation(store_key)
            if not ok:
                latest_data = await self.get_schedule_data(
                    session_id=session_id,
                    persona_name=resolved_persona_name,
                    target_date=target_date,
                    debug=debug,
                )
                if not self._is_missing_today_schedule(latest_data):
                    result.update({
                        "status": "existing",
                        "message": "目标日期日程已由其他任务生成",
                        "data": latest_data,
                        "generated_now": False,
                    })
                    return result
                result["message"] = f"当前人格 {resolved_persona_name or store_key} 的目标日期日程正在生成中，请稍后再试"
                return result

            try:
                generated = await service.generate_schedule(
                    event=None,
                    persona_name=store_key,
                    persona_desc=resolved_persona_desc or f"人格：{resolved_persona_name or store_key}。",
                    target_date=target_date,
                )
                if generated.get("meta", {}).get("error"):
                    result["message"] = generated.get("memo") or "自动补生成目标日期日程失败"
                    return result
                service.save_generated(store_key, generated)
            finally:
                await service.exit_generation(store_key)

            final_data = await self.get_schedule_data(
                session_id=session_id,
                persona_name=resolved_persona_name,
                target_date=target_date,
                debug=debug,
            )
            if self._is_missing_today_schedule(final_data):
                result["message"] = "目标日期日程补生成后仍不可用，请检查 Dayflow 存储链路"
                result["data"] = final_data or {}
                return result

            result.update({
                "status": "generated",
                "message": "已自动补生成目标日期日程",
                "data": final_data,
                "generated_now": True,
            })
            logger.info(
                f"[DayMind] 已自动补生成目标日期日程: session_id={session_id}, persona={resolved_persona_name or store_key}, target_date={target_date or ''}"
            )
            return result

        except Exception as e:
            logger.error(f"[DayMind] ensure_today_schedule 失败: {e}", exc_info=True)
            result["message"] = str(e)
            return result

    def _extract_memory_engine_from_instance(self, instance):
        if not instance:
            return None
        try:
            initializer = getattr(instance, "initializer", None)
            if not initializer:
                return None
            memory_engine = getattr(initializer, "memory_engine", None)
            return memory_engine if memory_engine is not None else None
        except Exception as e:
            logger.warning(f"[DayMind] 从 livingmemory 实例提取 memory_engine 失败: {e}")
            return None

    def get_memory_engine(self, refresh_if_invalid: bool = True, debug: bool = False):
        """获取 livingmemory 的 memory_engine；若缓存失效则自动重查。"""
        if not self.has_livingmemory:
            if debug:
                logger.info("[DayMind][debug] get_memory_engine: has_livingmemory=False")
            return None

        memory_engine = self._extract_memory_engine_from_instance(self._livingmemory_instance)
        if memory_engine is not None:
            if debug:
                logger.info(
                    f"[DayMind][debug] get_memory_engine: cache_hit=True, "
                    f"instance_cls={self._livingmemory_instance.__class__.__name__ if self._livingmemory_instance else 'None'}, "
                    f"engine_cls={memory_engine.__class__.__name__}"
                )
            return memory_engine

        if debug:
            logger.info(
                f"[DayMind][debug] get_memory_engine: cache_hit=False, refresh_if_invalid={refresh_if_invalid}, "
                f"instance_exists={self._livingmemory_instance is not None}"
            )

        if refresh_if_invalid:
            deps = self.check_dependencies()
            memory_engine = self._extract_memory_engine_from_instance(self._livingmemory_instance)
            if debug:
                logger.info(
                    f"[DayMind][debug] get_memory_engine refresh result: has_livingmemory={deps.get('livingmemory')}, "
                    f"instance_exists={self._livingmemory_instance is not None}, engine_ok={memory_engine is not None}"
                )
            return memory_engine

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
        memory_engine = self.get_memory_engine(refresh_if_invalid=True, debug=True)
        if not memory_engine:
            logger.warning(
                f"[DayMind] memory_engine 不可用，跳过存储: date={date_str}, session_id={session_id}, "
                f"persona_id={persona_id}, has_livingmemory={self.has_livingmemory}, "
                f"instance_exists={self._livingmemory_instance is not None}"
            )
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

            logger.info(
                f"[DayMind][debug] store_to_memory start: date={date_str}, session_id={session_id}, "
                f"persona_id={resolved_persona_id}, metadata_keys={sorted(final_metadata.keys())}"
            )

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
        persona_id: str | None = None,
        persona_name: str | None = None,
    ) -> dict[str, Any]:
        """将指定日期的 DayMind diary memory 标记为已删除，而非物理删除。"""
        memory_engine = self.get_memory_engine(refresh_if_invalid=True, debug=True)
        result = {"matched": 0, "updated": 0, "ids": []}
        if not memory_engine:
            logger.info(
                f"[DayMind][debug] mark_daymind_diary_memories_deleted skipped: date={date_str}, "
                f"session_id={session_id}, engine_ok={memory_engine is not None}"
            )
            return result

        try:
            memories = []
            if session_id:
                try:
                    memories = await memory_engine.get_session_memories(session_id, limit=1000)
                except Exception as e:
                    logger.warning(f"[DayMind] 通过 session_id 读取记忆失败，尝试回退到全量筛选: {e}")

            if not memories:
                persona_hint = persona_id or persona_name or await self.resolve_persona_id(session_id)
                search_query = f"daymind {date_str} {persona_hint or ''}".strip()
                try:
                    memories = await memory_engine.search_memory(search_query, session_id=session_id, top_k=50)
                except Exception as e:
                    logger.warning(f"[DayMind] 回退搜索旧日记记忆失败: {e}")
                    memories = []

            diary_identity = f"daymind:{persona_name}:{date_str}" if persona_name else None
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
                if diary_identity and str(metadata.get("diary_identity") or "").strip() not in {"", diary_identity}:
                    continue
                if persona_name and str(metadata.get("persona_name") or "").strip() not in {"", str(persona_name).strip()}:
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

            logger.info(
                f"[DayMind][debug] mark_daymind_diary_memories_deleted result: date={date_str}, "
                f"session_id={session_id}, matched={result['matched']}, updated={result['updated']}"
            )
            if result["updated"]:
                logger.info(
                    f"[DayMind] 已将旧日记记忆标记删除: date={date_str}, "
                    f"session_id={session_id}, updated={result['updated']}"
                )
        except Exception as e:
            logger.error(f"[DayMind] 标记旧日记记忆删除失败: {e}", exc_info=True)

        return result
