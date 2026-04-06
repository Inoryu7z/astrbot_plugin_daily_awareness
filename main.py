"""
astrbot_plugin_daymind - 心智手记
"""

import json
import datetime
import asyncio
import threading
from pathlib import Path
from typing import Optional

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.star_tools import StarTools

from .config import PLUGIN_DESCRIPTION, PLUGIN_REPO, PLUGIN_VERSION
from .core import (
    AwarenessScheduler,
    ReflectionGenerator,
    DiaryGenerator,
    DependencyManager,
    MessageCache,
    SilentHoursChecker,
    DayMindWebUI,
    MoodManager,
    PersonaConfigMixin,
)


@register(
    "astrbot_plugin_daymind",
    "Inoryu7z",
    PLUGIN_DESCRIPTION,
    PLUGIN_VERSION,
    PLUGIN_REPO,
)
class DayMindPlugin(Star, PersonaConfigMixin):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.data_dir = str(StarTools.get_data_dir())
        self.state_file = Path(self.data_dir) / "awareness_state.json"
        self._state_write_lock = threading.Lock()
        self._state_save_pending = False

        self.dependency_manager = DependencyManager(context)
        self.message_cache = MessageCache(max_rounds=10)
        self.session_persona_map: dict[str, str] = {}
        self.session_persona_activity_map: dict[str, str] = {}

        self.silent_hours = SilentHoursChecker(
            start_time=self.config.get("silent_hours_start", "00:00"),
            end_time=self.config.get("silent_hours_end", "06:00"),
            enabled=self.config.get("silent_hours_enabled", True),
        )

        self.reflection_generator: Optional[ReflectionGenerator] = None
        self.diary_generator: Optional[DiaryGenerator] = None
        self.mood_manager: Optional[MoodManager] = None
        self.scheduler: Optional[AwarenessScheduler] = None
        self.webui: Optional[DayMindWebUI] = None

    def _is_persona_managed(self, persona_name: str | None) -> bool:
        return self._find_persona_config(persona_name) is not None

    def _get_session_persona_retention_days(self) -> int:
        try:
            return max(int(self.config.get("session_persona_retention_days", 30) or 30), 1)
        except Exception:
            return 30

    def _get_session_persona_max_entries(self) -> int:
        try:
            return max(int(self.config.get("session_persona_max_entries", 500) or 500), 1)
        except Exception:
            return 500

    def _safe_parse_iso_datetime(self, value) -> datetime.datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.datetime.fromisoformat(text)
        except Exception:
            return None

    def _prune_session_persona_state(self):
        retention_days = self._get_session_persona_retention_days()
        max_entries = self._get_session_persona_max_entries()
        now = datetime.datetime.now()
        cutoff = now - datetime.timedelta(days=retention_days)

        normalized_activity: dict[str, str] = {}
        for session_id, raw_time in list((self.session_persona_activity_map or {}).items()):
            session_key = str(session_id or "").strip()
            if not session_key:
                continue
            parsed = self._safe_parse_iso_datetime(raw_time)
            if parsed is None:
                continue
            if parsed < cutoff:
                continue
            normalized_activity[session_key] = parsed.isoformat()

        active_items: list[tuple[str, str, str]] = []
        for session_id, persona_name in list((self.session_persona_map or {}).items()):
            session_key = str(session_id or "").strip()
            canonical_persona = self._canonical_persona_name(persona_name)
            if not session_key or not canonical_persona:
                continue
            activity_iso = normalized_activity.get(session_key)
            if not activity_iso:
                continue
            active_items.append((session_key, canonical_persona, activity_iso))

        active_items.sort(key=lambda item: item[2], reverse=True)
        if len(active_items) > max_entries:
            active_items = active_items[:max_entries]

        self.session_persona_map = {session_id: persona_name for session_id, persona_name, _ in active_items}
        self.session_persona_activity_map = {session_id: activity_iso for session_id, _, activity_iso in active_items}

        if self.scheduler is not None:
            self.scheduler.session_persona_map = self.session_persona_map
            self.scheduler.session_persona_activity_map = self.session_persona_activity_map

    def _get_message_cache_state_for_persist(self) -> dict:
        allowed_session_ids = list(self.session_persona_map.keys())
        max_sessions = self._get_session_persona_max_entries()
        return self.message_cache.get_state(
            allowed_session_ids=allowed_session_ids,
            max_sessions=max_sessions,
        )

    def _touch_session_persona(self, session_id: str | None, persona_name: str | None = None, persist: bool = False):
        session_key = str(session_id or "").strip()
        if not session_key:
            return
        if persona_name is not None:
            canonical_persona = self._canonical_persona_name(persona_name)
            if canonical_persona:
                self.session_persona_map[session_key] = canonical_persona
        if session_key in self.session_persona_map:
            self.session_persona_activity_map[session_key] = datetime.datetime.now().isoformat()
        self._prune_session_persona_state()
        if persist:
            self._save_state()

    async def initialize(self):
        version_time = f"{PLUGIN_VERSION} - mood_system"
        logger.info(f"[DayMind] ========== 版本 {version_time} 已加载 ==========")

        self.dependency_manager.check_dependencies()

        self.reflection_generator = ReflectionGenerator(
            self.context, self.config, self.dependency_manager, self.message_cache
        )
        self.diary_generator = DiaryGenerator(
            self.context, self.config, self.dependency_manager
        )

        self.mood_manager = MoodManager(
            self.context, self.config, self.dependency_manager
        )

        self.scheduler = AwarenessScheduler(
            self.context,
            self.config,
            self.data_dir,
            self.reflection_generator,
            self.diary_generator,
            self.dependency_manager,
            self.message_cache,
            self.silent_hours,
            self.session_persona_map,
            self.mood_manager,
            state_persist_callback=self._save_state,
            session_persona_activity_map=self.session_persona_activity_map,
        )

        self._load_state()
        await self.scheduler.start()

        if self.config.get("enable_webui", True):
            try:
                self.webui = DayMindWebUI(
                    self.data_dir,
                    self.config,
                    scheduler=self.scheduler,
                    dependency_manager=self.dependency_manager,
                    plugin=self,
                )
                await self.webui.start()
            except Exception as e:
                logger.error(f"[DayMind] WebUI 启动失败: {e}", exc_info=True)

    async def terminate(self):
        if self.webui:
            try:
                await self.webui.stop()
            except Exception as e:
                logger.warning(f"[DayMind] 停止 WebUI 失败: {e}")
        if self.scheduler:
            await self.scheduler.stop()
        self._save_state()

    def _is_debug_mode(self) -> bool:
        return bool(self.config.get("debug_mode", False))

    def _load_state(self):
        try:
            if not self.state_file.exists():
                return
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            saved_map = data.get("session_persona_map", {}) or {}
            saved_activity = data.get("session_persona_activity_map", {}) or {}
            normalized_map: dict[str, str] = {}
            normalized_activity: dict[str, str] = {}
            for session_id, persona_name in saved_map.items():
                session_key = str(session_id or "").strip()
                canonical = self._canonical_persona_name(persona_name)
                if not session_key or not canonical:
                    continue
                normalized_map[session_key] = canonical
                raw_time = saved_activity.get(session_key)
                parsed = self._safe_parse_iso_datetime(raw_time)
                normalized_activity[session_key] = (parsed or datetime.datetime.now()).isoformat()

            self.session_persona_map = normalized_map
            self.session_persona_activity_map = normalized_activity
            self._prune_session_persona_state()
            if self.scheduler is not None:
                self.scheduler.session_persona_map = self.session_persona_map
                self.scheduler.session_persona_activity_map = self.session_persona_activity_map

            runtime_config = data.get("runtime_config", {}) or {}
            if self.scheduler is not None:
                self.scheduler.load_runtime_config(runtime_config)

            saved_persona_states = data.get("persona_states")
            if self.scheduler is not None and isinstance(saved_persona_states, dict):
                normalized_states: dict[str, dict] = {}
                for persona_name, state in saved_persona_states.items():
                    canonical = self._canonical_persona_name(persona_name)
                    if canonical and isinstance(state, dict):
                        normalized_states[canonical] = state
                self.scheduler.restore_persona_states(normalized_states)

            if "message_cache" in data:
                self.message_cache.restore_state(data["message_cache"])
        except Exception as e:
            logger.warning(f"[DayMind] 加载状态失败: {e}")

    def _build_persist_payload(self) -> dict:
        self._prune_session_persona_state()
        runtime_config = self.scheduler.get_runtime_config() if self.scheduler else {}
        return {
            "date": datetime.datetime.now().strftime("%Y-%m-%d"),
            "message_cache": self._get_message_cache_state_for_persist(),
            "last_update": datetime.datetime.now().isoformat(),
            "session_persona_map": dict(self.session_persona_map),
            "session_persona_activity_map": dict(self.session_persona_activity_map),
            "runtime_config": runtime_config,
            "persona_states": self.scheduler.export_persona_states() if self.scheduler else {},
        }

    def _save_state(self):
        if self._state_save_pending:
            return
        self._state_save_pending = True
        try:
            Path(self.data_dir).mkdir(parents=True, exist_ok=True)
            data = self._build_persist_payload()
            tmp_path = self.state_file.with_suffix(".tmp")
            with self._state_write_lock:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                tmp_path.replace(self.state_file)
        except Exception as e:
            logger.error(f"[DayMind] 保存状态失败: {e}")
        finally:
            self._state_save_pending = False

    def save_runtime_state(self):
        self._save_state()

    def persist_runtime_config(self, updates: dict):
        if self.scheduler:
            self.scheduler.load_runtime_config(updates or {})
        self._save_state()

    async def _resolve_persona_name_for_session(self, session_id: str) -> str | None:
        try:
            ctx = await self.dependency_manager.resolve_persona_context(session_id)
            persona_name = self._canonical_persona_name(ctx.get("persona_name") or ctx.get("persona_id"))
            if persona_name:
                self._touch_session_persona(session_id, persona_name)
                return persona_name
        except Exception as e:
            logger.debug(f"[DayMind] 解析人格失败: {e}")
        return None

    def _get_sender_id(self, event: AstrMessageEvent) -> str | None:
        try:
            if hasattr(event, "get_sender_id"):
                value = event.get_sender_id()
                if value:
                    return str(value)
        except Exception:
            pass
        try:
            sender = getattr(event, "sender", None)
            if sender and hasattr(sender, "user_id"):
                value = getattr(sender, "user_id", None)
                if value:
                    return str(value)
        except Exception:
            pass
        try:
            sender = getattr(getattr(event, "message_obj", None), "sender", None)
            value = getattr(sender, "user_id", None)
            if value:
                return str(value)
        except Exception:
            pass
        return None

    def _get_sender_name(self, event: AstrMessageEvent) -> str | None:
        try:
            if hasattr(event, "get_sender_name"):
                value = event.get_sender_name()
                if value:
                    return str(value)
        except Exception:
            pass
        try:
            sender = getattr(event, "sender", None)
            if sender and hasattr(sender, "nickname"):
                value = getattr(sender, "nickname", None)
                if value:
                    return str(value)
        except Exception:
            pass
        return None

    def _get_group_id(self, event: AstrMessageEvent) -> str | None:
        try:
            if hasattr(event, "get_group_id"):
                value = event.get_group_id()
                if value:
                    return str(value)
        except Exception:
            pass
        try:
            value = getattr(getattr(event, "message_obj", None), "group_id", None)
            if value:
                return str(value)
        except Exception:
            pass
        return None

    async def _resolve_event_persona(self, event: AstrMessageEvent) -> tuple[str, str | None, str | None]:
        session_id = event.unified_msg_origin
        persona_ctx = await self.dependency_manager.resolve_persona_context(session_id)
        persona_name = self._canonical_persona_name(persona_ctx.get("persona_name") or persona_ctx.get("persona_id"))
        persona_desc = persona_ctx.get("persona_desc")
        if persona_name:
            self._touch_session_persona(session_id, persona_name)
        return session_id, persona_name, persona_desc

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            session_id = event.unified_msg_origin
            persona_name = await self._resolve_persona_name_for_session(session_id)
            if persona_name and self._is_persona_managed(persona_name) and event.message_str:
                await self.message_cache.add_message(
                    session_id,
                    "user",
                    event.message_str,
                    sender_id=self._get_sender_id(event),
                    sender_name=self._get_sender_name(event),
                    group_id=self._get_group_id(event),
                )
                logger.debug(f"[DayMind] 已缓存会话人格: {session_id} -> {persona_name}")
        except Exception as e:
            logger.debug(f"[DayMind] on_llm_request 处理失败: {e}")

        if self.scheduler:
            if getattr(req, "system_prompt", None) is None:
                req.system_prompt = ""
            current_text = await self.scheduler.get_current_awareness_for_session(event.unified_msg_origin)
            if current_text:
                req.system_prompt += f"\n\n### 本日状态（截止到目前）\n{current_text}"

            persona_name = self.session_persona_map.get(event.unified_msg_origin)
            if self.mood_manager and self.mood_manager.is_mood_enabled(persona_name) and self.mood_manager.is_inject_mood_into_reply(persona_name):
                mood = self.scheduler.get_current_mood_for_session(event.unified_msg_origin)
                if mood:
                    mood_injection = self.mood_manager.build_mood_injection(mood, persona_name=persona_name)
                    if mood_injection:
                        req.system_prompt += mood_injection
                        if self._is_debug_mode():
                            logger.info(
                                f"[DayMind][debug] 注入心情到回复: session={event.unified_msg_origin}, current={mood.get('label', '未知')}, previous={(mood.get('previous_mood') or {}).get('label', '无')}, sub_labels={mood.get('sub_labels', [])}"
                            )

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp):
        try:
            session_id = event.unified_msg_origin
            persona_name = await self._resolve_persona_name_for_session(session_id)
            if persona_name and self._is_persona_managed(persona_name) and resp and resp.completion_text:
                await self.message_cache.add_message(
                    session_id,
                    "assistant",
                    resp.completion_text,
                    sender_id=getattr(event, "get_self_id", lambda: None)(),
                    sender_name="AstrBot",
                    group_id=self._get_group_id(event),
                )
        except Exception as e:
            logger.debug(f"[DayMind] on_llm_response 处理失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("daymind_status")
    async def daymind_status(self, event: AstrMessageEvent):
        if not self.scheduler:
            yield event.plain_result("调度器未初始化")
            return

        session_id, persona_name, _ = await self._resolve_event_persona(event)
        status = self.scheduler.get_status(persona_name)

        preview = status.get("recent_reflections_preview", [])
        preview_text = "\n".join([f"- {x}" for x in preview]) if preview else "（暂无）"

        current_mood = status.get("current_mood")
        previous_mood = status.get("previous_mood")
        mood_text = "（暂无）"
        if current_mood:
            mood_label = current_mood.get("label", "未知")
            mood_reason = current_mood.get("reason", "")
            sub_labels = current_mood.get("sub_labels", []) or []
            mood_text = f"{mood_label}"
            if sub_labels:
                mood_text += f"（副标签：{'、'.join(sub_labels[:3])}）"
            if mood_reason:
                mood_text += f" - {mood_reason[:50]}"
        previous_mood_text = "（暂无）"
        if previous_mood:
            previous_mood_text = previous_mood.get("label", "未知")

        webui_url = f"http://{self.config.get('webui_host', '127.0.0.1')}:{self.config.get('webui_port', 8899)}" if self.config.get("enable_webui", True) else "（未启用）"

        yield event.plain_result(
            f"DayMind状态\n"
            f"当前会话: {session_id}\n"
            f"当前人格: {persona_name or '（未识别）'}\n"
            f"已管理人格: {', '.join(status.get('enabled_personas', [])) or '（未配置）'}\n"
            f"运行中: {status['is_running']}\n"
            f"自动思考: {status.get('enable_auto_reflection')}\n"
            f"自动日记: {status.get('enable_auto_diary')}\n"
            f"心情系统: {status.get('enable_mood_system')}\n"
            f"心情注入回复: {status.get('inject_mood_into_reply')}\n"
            f"当前心情: {mood_text}\n"
            f"上一轮心情: {previous_mood_text}\n"
            f"今日心情记录数: {status.get('today_moods_count', 0)}\n"
            f"思考参考条数: {status.get('reflection_reference_count')}\n"
            f"今日思考次数: {status['today_reflections_count']}\n"
            f"上次思考时间: {status.get('last_reflection_time')}\n"
            f"思考周期(分钟): {status.get('next_reflection_in_minutes')}\n"
            f"思考去重档位: {status.get('reflection_dedupe_mode')}\n"
            f"记忆绑定人格: {status.get('primary_persona_id')}\n"
            f"思考保留天数: {status.get('reflection_retention_days')}\n"
            f"日记保留天数: {status.get('diary_retention_days')}\n"
            f"默认主题: {status.get('webui_default_theme')}\n"
            f"WebUI: {webui_url}\n"
            f"最近思考预览:\n{preview_text}"
        )

    @filter.command("查看心情")
    async def check_mood(self, event: AstrMessageEvent):
        if not self.scheduler:
            yield event.plain_result("调度器未初始化")
            return

        session_id, persona_name, _ = await self._resolve_event_persona(event)

        if not self.mood_manager or not self.mood_manager.is_mood_enabled(persona_name):
            yield event.plain_result("心情系统未启用")
            return

        mood = self.scheduler.get_current_mood_for_session(session_id)
        if not mood:
            mood = self.scheduler.get_current_mood_for_persona(persona_name)

        if not mood:
            yield event.plain_result(f"当前人格 {persona_name or '（未识别）'} 暂无心情记录")
            return

        label = mood.get("label", "未知")
        reason = mood.get("reason", "")
        source = mood.get("source", "未知")
        updated_at = mood.get("updated_at", "")
        sub_labels = mood.get("sub_labels", []) or []
        previous_mood = mood.get("previous_mood") or self.scheduler.get_previous_mood_for_persona(persona_name)

        style_text = self.mood_manager.get_mood_style_text(mood, persona_name=persona_name)

        result = f"当前心情: {label}\n"
        if sub_labels:
            result += f"副标签: {'、'.join(sub_labels)}\n"
        result += f"来源: {source}\n"
        if previous_mood:
            result += f"上一轮心情: {previous_mood.get('label', '未知')}\n"
        if reason:
            result += f"原因: {reason}\n"
        if updated_at:
            result += f"更新时间: {updated_at}\n"
        if style_text:
            result += f"\n风格影响:\n{style_text}"

        yield event.plain_result(result)

    @filter.command("今日心情")
    async def today_moods(self, event: AstrMessageEvent):
        if not self.scheduler:
            yield event.plain_result("调度器未初始化")
            return

        _, persona_name, _ = await self._resolve_event_persona(event)

        if not self.mood_manager or not self.mood_manager.is_mood_enabled(persona_name):
            yield event.plain_result("心情系统未启用")
            return

        moods = self.scheduler.get_today_moods_for_persona(persona_name, limit=10)

        if not moods:
            yield event.plain_result(f"当前人格 {persona_name or '（未识别）'} 今日暂无心情记录")
            return

        lines = [f"人格 {persona_name} 今日心情变化:"]
        for i, m in enumerate(moods, 1):
            label = m.get("label", "未知")
            sub_labels = m.get("sub_labels", []) or []
            prev_label = (m.get("previous_mood") or {}).get("label", "")
            updated_at = m.get("updated_at", "")
            time_part = updated_at.split("T")[1][:5] if "T" in updated_at else updated_at[:5] if updated_at else "未知时间"
            suffix = f"（承接 {prev_label}）" if prev_label and prev_label != label else ""
            sub_text = f"｜副标签：{'、'.join(sub_labels[:3])}" if sub_labels else ""
            lines.append(f"{i}. [{time_part}] {label}{suffix}{sub_text}")

        mood_counts = {}
        for m in moods:
            label = m.get("label", "未知")
            mood_counts[label] = mood_counts.get(label, 0) + 1

        lines.append(f"\n心情统计:")
        for label, count in sorted(mood_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- {label}: {count}次")

        yield event.plain_result("\n".join(lines))

    @filter.command("今日日记", alias={"查看今日日记", "daymind_diary_today"})
    async def today_diary(self, event: AstrMessageEvent):
        if not self.scheduler:
            yield event.plain_result("调度器未初始化")
            return

        _, persona_name, _ = await self._resolve_event_persona(event)
        normalized_persona = self._canonical_persona_name(persona_name)
        if not normalized_persona:
            yield event.plain_result("当前会话未识别到人格")
            return
        if not self.scheduler.is_persona_enabled(normalized_persona):
            yield event.plain_result(f"当前人格未启用 DayMind：{normalized_persona}")
            return

        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        diary_item = self.scheduler.get_diary_item(today_str, normalized_persona)
        if not diary_item:
            yield event.plain_result(f"当前人格 {normalized_persona} 今日日记尚未生成")
            return

        content = str(diary_item.get("content") or "").strip()
        if not content:
            yield event.plain_result(f"当前人格 {normalized_persona} 今日日记内容为空")
            return

        yield event.plain_result(f"人格：{normalized_persona}\n日期：{today_str}\n\n{content}")

    @filter.command("手动思考")
    async def manual_reflection(self, event: AstrMessageEvent):
        if not self.scheduler:
            yield event.plain_result("调度器未初始化")
            return

        session_id, persona_name, persona_desc = await self._resolve_event_persona(event)
        if not self.scheduler.is_persona_enabled(persona_name):
            yield event.plain_result(f"当前人格未启用 DayMind：{persona_name or '（未识别）'}")
            return

        yield event.plain_result("正在思考...")
        result = await self.scheduler.run_manual_reflection(session_id, persona_name, persona_desc)
        self._save_state()

        if result.get("status") == "duplicate":
            yield event.plain_result(f"思考完成，但与近期内容过于相似，未更新状态。\n结果：\n{result.get('text', '')}")
            return
        if result.get("status") == "success":
            mood_info = ""
            if result.get("mood"):
                mood = result["mood"]
                prev_label = (mood.get("previous_mood") or {}).get("label", "")
                sub_labels = mood.get("sub_labels", []) or []
                mood_info = f"\n当前心情: {mood.get('label', '未知')}"
                if sub_labels:
                    mood_info += f"\n副标签: {'、'.join(sub_labels)}"
                if prev_label and prev_label != mood.get("label"):
                    mood_info += f"\n上一轮心情: {prev_label}"
            yield event.plain_result(f"思考完成：\n{result.get('text', '')}{mood_info}")
            return
        yield event.plain_result(result.get("message") or "思考失败，请检查模型提供商配置")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("生成日记")
    async def manual_diary(self, event: AstrMessageEvent):
        if not self.scheduler:
            yield event.plain_result("调度器未初始化")
            return

        session_id, persona_name, persona_desc = await self._resolve_event_persona(event)
        if not self.scheduler.is_persona_enabled(persona_name):
            yield event.plain_result(f"当前人格未启用 DayMind：{persona_name or '（未识别）'}")
            return

        yield event.plain_result("正在生成日记...")

        result = await self.scheduler.run_manual_diary(session_id, persona_name, persona_desc)
        self._save_state()

        if result.get("status") == "failed_schedule":
            yield event.plain_result(f"今日日程不可用，已取消本次日记生成：{result.get('message') or '未知原因'}")
            return
        if result.get("status") == "exists":
            if result.get("schedule_generated_now"):
                data = result.get("schedule_data") or {}
                outfit = str(data.get("outfit") or "").strip() or "尚未生成"
                schedule = str(data.get("schedule") or "").strip() or "（无日程内容）"
                yield event.plain_result(
                    f"今日日程已自动补生成，但今日日记已存在，无需再次生成。\n"
                    f"👕 今日穿搭：{outfit}\n"
                    f"📝 日程安排：\n{schedule}"
                )
                return
            yield event.plain_result("今日日记已生成，如需重新生成请开启 allow_overwrite_today_diary 调试开关")
            return
        if result.get("status") == "success":
            extra = f"\n已标记旧记忆为删除: {result.get('marked_deleted', 0)} 条" if result.get("marked_deleted") else ""
            prefix = ""
            if result.get("schedule_generated_now"):
                data = result.get("schedule_data") or {}
                outfit = str(data.get("outfit") or "").strip() or "尚未生成"
                schedule = str(data.get("schedule") or "").strip() or "（无日程内容）"
                prefix = (
                    f"已自动补生成今日日程并继续生成日记。\n"
                    f"👕 今日穿搭：{outfit}\n"
                    f"📝 日程安排：\n{schedule}\n\n"
                )
            yield event.plain_result(f"{prefix}今日日记：\n\n{result.get('content', '')}{extra}")
            return
        yield event.plain_result(result.get("message") or "日记生成失败，请检查模型提供商配置")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("清除今日思考")
    async def clear_today_reflection(self, event: AstrMessageEvent):
        if not self.scheduler:
            yield event.plain_result("调度器未初始化")
            return

        _, persona_name, _ = await self._resolve_event_persona(event)
        if not self.scheduler.is_persona_enabled(persona_name):
            yield event.plain_result(f"当前人格未启用 DayMind：{persona_name or '（未识别）'}")
            return

        result = await self.scheduler.reset_today_reflections(persona_name)
        self._save_state()
        yield event.plain_result(
            f"已清空今日思考流。\n"
            f"人格: {result['persona_name']}\n"
            f"日期: {result['date']}\n"
            f"本地文件已删除: {result['removed_local_file']}\n"
            f"当前状态已重置为空白。"
        )
