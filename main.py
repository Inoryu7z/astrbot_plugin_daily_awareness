"""
astrbot_plugin_daymind - 心智手记
"""

import json
import datetime
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
)


LEGACY_THINKING_TEMPLATE = """你是一个正在体验真实生活的AI角色。

## 当前身份
- 当前人格名称：{persona_name}
- 当前人格设定：
{persona_desc}

## 当前信息
- 时间：{time}（{weekday}）
- 当前状态：
{state_info}

## 最近对话
{recent_messages}

## 上一条思考
{last_awareness}

## 重要规则
1. 必须以日程为准
2. 语气与表达优先贴合人格设定本体，而不是仅参考人格名称
3. 若与上一条思考高度相似，只写细微变化

## 要求
请用{mode_desc}描述你现在的状态：
{length_hint}

直接输出状态描述，不要有任何额外说明。"""

LEGACY_DIARY_TEMPLATE = """你是一个善于记录生活的日记写作者。

## 当前身份
- 当前人格名称：{persona_name}
- 当前人格设定：
{persona_desc}

## 基本信息
- 当前日期时间：{current_date}
- 记录日期：{date}
- 今日状态：
{state_info}

## 今日状态记录
{reflections}

## 最近历史日记（仅辅助连续性）
{recent_diaries}

## 重要规则
1. 以日程为准
2. 开头必须写记录日期（例如：2026年3月30日 星期一）
3. 涉及今天/昨晚/明早等相对时间时，需结合当前日期时间转换为明确日期/时段
4. 叙述语气优先贴合人格设定本体
5. 今天的信息永远优先，历史日记只用于保持叙事连续性、情绪延续和生活节奏一致
6. 不要机械复述历史日记内容，不要让历史内容盖过今天的经历

## 要求
请写一篇{mode_desc}的日记：
{length_hint}

直接输出日记内容，不要有任何额外说明。"""


@register(
    "astrbot_plugin_daymind",
    "Inoryu7z",
    PLUGIN_DESCRIPTION,
    PLUGIN_VERSION,
    PLUGIN_REPO,
)
class DayMindPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config or {}

        self.data_dir = str(StarTools.get_data_dir())
        self.state_file = Path(self.data_dir) / "awareness_state.json"

        self.dependency_manager = DependencyManager(context)
        self.message_cache = MessageCache(max_rounds=10)
        self.session_persona_map: dict[str, str] = {}

        self.silent_hours = SilentHoursChecker(
            start_time=self.config.get("silent_hours_start", "00:00"),
            end_time=self.config.get("silent_hours_end", "06:00"),
            enabled=self.config.get("silent_hours_enabled", True),
        )

        self.reflection_generator: Optional[ReflectionGenerator] = None
        self.diary_generator: Optional[DiaryGenerator] = None
        self.scheduler: Optional[AwarenessScheduler] = None
        self.webui: Optional[DayMindWebUI] = None

    async def initialize(self):
        version_time = f"{PLUGIN_VERSION} - runtime_config_isolated"
        logger.info(f"[DayMind] ========== 版本 {version_time} 已加载 ==========")

        self._migrate_legacy_prompt_templates()
        self.dependency_manager.check_dependencies()

        self.reflection_generator = ReflectionGenerator(
            self.context, self.config, self.dependency_manager, self.message_cache
        )
        self.diary_generator = DiaryGenerator(
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

    def _migrate_legacy_prompt_templates(self):
        try:
            migrated = []

            current_thinking = (self.config.get("thinking_prompt_template", "") or "").strip()
            current_diary = (self.config.get("diary_prompt_template", "") or "").strip()

            reflection_default = ReflectionGenerator(
                self.context, self.config, self.dependency_manager, self.message_cache
            )._get_default_template().strip()
            diary_default = DiaryGenerator(
                self.context, self.config, self.dependency_manager
            )._get_default_template().strip()

            if not current_thinking or current_thinking == LEGACY_THINKING_TEMPLATE.strip():
                self.config["thinking_prompt_template"] = reflection_default
                migrated.append("thinking_prompt_template")

            if not current_diary or current_diary == LEGACY_DIARY_TEMPLATE.strip():
                self.config["diary_prompt_template"] = diary_default
                migrated.append("diary_prompt_template")

            if migrated:
                logger.info(f"[DayMind] 已自动迁移旧版提示词模板: {', '.join(migrated)}")
            elif self.config.get("debug_mode", False):
                logger.info("[DayMind] 未触发提示词迁移，当前使用现有配置模板")
        except Exception as e:
            logger.warning(f"[DayMind] 提示词迁移失败: {e}")

    def _load_state(self):
        try:
            if self.state_file.exists():
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)

                saved_date = data.get("date", "")
                today_str = datetime.datetime.now().strftime("%Y-%m-%d")

                self.session_persona_map = data.get("session_persona_map", {}) or {}
                if self.scheduler is not None:
                    self.scheduler.session_persona_map = self.session_persona_map

                runtime_config = data.get("runtime_config", {}) or {}
                if self.scheduler is not None:
                    self.scheduler.load_runtime_config(runtime_config)

                if saved_date == today_str and self.scheduler:
                    self.scheduler.today_reflections = data.get("reflections", [])
                    self.scheduler.current_awareness_text = data.get("current_text", "")
                    self.scheduler.diary_generated_today = data.get("diary_generated_today", False)
                    self.scheduler.last_diary_date = data.get("last_diary_date", today_str)
                    if "message_cache" in data:
                        self.message_cache.restore_state(data["message_cache"])
        except Exception as e:
            logger.warning(f"[DayMind] 加载状态失败: {e}")

    def _save_state(self):
        try:
            Path(self.data_dir).mkdir(parents=True, exist_ok=True)
            runtime_config = self.scheduler.get_runtime_config() if self.scheduler else {}
            data = {
                "date": datetime.datetime.now().strftime("%Y-%m-%d"),
                "reflections": self.scheduler.today_reflections if self.scheduler else [],
                "current_text": self.scheduler.current_awareness_text if self.scheduler else "",
                "message_cache": self.message_cache.get_state(),
                "last_update": datetime.datetime.now().isoformat(),
                "diary_generated_today": self.scheduler.diary_generated_today if self.scheduler else False,
                "last_diary_date": self.scheduler.last_diary_date if self.scheduler else "",
                "session_persona_map": self.session_persona_map,
                "runtime_config": runtime_config,
            }
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[DayMind] 保存状态失败: {e}")

    def save_runtime_state(self):
        self._save_state()

    def persist_runtime_config(self, updates: dict):
        if self.scheduler:
            self.scheduler.load_runtime_config(updates or {})
        self._save_state()

    async def _resolve_persona_name_for_session(self, session_id: str) -> str | None:
        try:
            ctx = await self.dependency_manager.resolve_persona_context(session_id)
            persona_name = ctx.get("persona_name") or ctx.get("persona_id")
            if persona_name:
                self.session_persona_map[session_id] = persona_name
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

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.config.get("enable_auto_reflection", True):
            return
        try:
            session_id = event.unified_msg_origin
            if event.message_str:
                await self.message_cache.add_message(
                    session_id,
                    "user",
                    event.message_str,
                    sender_id=self._get_sender_id(event),
                    sender_name=self._get_sender_name(event),
                    group_id=self._get_group_id(event),
                )
            persona_name = await self._resolve_persona_name_for_session(session_id)
            if persona_name:
                logger.debug(f"[DayMind] 已缓存会话人格: {session_id} -> {persona_name}")
        except Exception as e:
            logger.debug(f"[DayMind] on_llm_request 处理失败: {e}")

        if self.scheduler and self.scheduler.current_awareness_text:
            req.system_prompt += f"\n\n###本日状态（截止到目前）\n{self.scheduler.current_awareness_text}"

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp):
        if not self.config.get("enable_auto_reflection", True):
            return
        try:
            session_id = event.unified_msg_origin
            if resp and resp.completion_text:
                await self.message_cache.add_message(
                    session_id,
                    "assistant",
                    resp.completion_text,
                    sender_id=getattr(event, "get_self_id", lambda: None)(),
                    sender_name="AstrBot",
                    group_id=self._get_group_id(event),
                )
            await self._resolve_persona_name_for_session(session_id)
        except Exception as e:
            logger.debug(f"[DayMind] on_llm_response 处理失败: {e}")

    @filter.command("daymind_status")
    async def daymind_status(self, event: AstrMessageEvent):
        if not self.scheduler:
            yield event.plain_result("调度器未初始化")
            return
        status = self.scheduler.get_status()
        preview = status.get("recent_reflections_preview", [])
        preview_text = "\n".join([f"- {x}" for x in preview]) if preview else "（暂无）"
        webui_url = f"http://{self.config.get('webui_host', '127.0.0.1')}:{self.config.get('webui_port', 8899)}" if self.config.get("enable_webui", True) else "（未启用）"
        yield event.plain_result(
            f"DayMind状态\n"
            f"运行中: {status['is_running']}\n"
            f"自动思考: {status.get('enable_auto_reflection')}\n"
            f"自动日记: {status.get('enable_auto_diary')}\n"
            f"思考参考条数: {status.get('reflection_reference_count')}\n"
            f"今日思考次数: {status['today_reflections_count']}\n"
            f"上次思考时间: {status.get('last_reflection_time')}\n"
            f"思考周期(分钟): {status.get('next_reflection_in_minutes')}\n"
            f"思考去重档位: {status.get('reflection_dedupe_mode')}\n"
            f"记忆绑定人格: {status.get('primary_persona_id')}\n"
            f"思考保留天数: {status.get('reflection_retention_days')}\n"
            f"日记保留天数: {status.get('diary_retention_days')}\n"
            f"默认主题: {status.get('webui_default_theme')}\n"
            f"默认模式: {status.get('webui_default_mode')}\n"
            f"WebUI: {webui_url}\n"
            f"最近思考预览:\n{preview_text}"
        )

    @filter.command("手动思考")
    async def manual_reflection(self, event: AstrMessageEvent):
        if not self.scheduler:
            yield event.plain_result("调度器未初始化")
            return

        yield event.plain_result("正在思考...")

        session_id = event.unified_msg_origin
        persona_ctx = await self.dependency_manager.resolve_persona_context(session_id)
        persona_name = persona_ctx.get("persona_name")
        persona_desc = persona_ctx.get("persona_desc")
        if persona_name:
            self.session_persona_map[session_id] = persona_name

        reference_count = max(int(self.config.get("reflection_reference_count", 2) or 2), 1)
        recent_reflections = self.scheduler.today_reflections[-reference_count:] if self.scheduler else []
        recent_awareness_text = "\n".join([f"- {x}" for x in recent_reflections]) if recent_reflections else "（暂无最近思考）"

        current_time = datetime.datetime.now().strftime("%H:%M")
        result = await self.reflection_generator.generate(
            current_time,
            session_id,
            recent_awareness_text,
            persona_name,
            persona_desc,
        )

        if result:
            if self.scheduler._is_duplicate_reflection(result):
                self.scheduler.last_reflection_time = datetime.datetime.now()
                yield event.plain_result(f"思考完成，但与近期内容过于相似，未更新状态。\n结果：\n{result}")
                return
            self.scheduler.current_awareness_text = result
            self.scheduler.today_reflections.append(result)
            self.scheduler.last_reflection_time = datetime.datetime.now()
            await self.scheduler._append_reflection_history(datetime.datetime.now().strftime("%Y-%m-%d"), result)
            self._save_state()
            yield event.plain_result(f"思考完成：\n{result}")
        else:
            yield event.plain_result("思考失败，请检查模型提供商配置")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("生成日记")
    async def manual_diary(self, event: AstrMessageEvent):
        if not self.scheduler:
            yield event.plain_result("调度器未初始化")
            return

        session_id = event.unified_msg_origin
        persona_ctx = await self.dependency_manager.resolve_persona_context(session_id)
        persona_name = persona_ctx.get("persona_name")
        persona_desc = persona_ctx.get("persona_desc")
        if persona_name:
            self.session_persona_map[session_id] = persona_name

        if self.scheduler.diary_generated_today and not self.config.get("allow_overwrite_today_diary", False):
            yield event.plain_result("今日日记已生成，如需重新生成请开启 allow_overwrite_today_diary 调试开关")
            return

        yield event.plain_result("正在生成日记...")

        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        diary_content = await self.diary_generator.generate(
            today_str,
            self.scheduler.today_reflections,
            session_id,
            persona_name,
            persona_desc,
        )

        if diary_content:
            overwrite = bool(self.config.get("allow_overwrite_today_diary", False))
            marked_deleted = 0
            if overwrite and self.config.get("store_diary_to_memory", True) and self.dependency_manager.has_livingmemory:
                result = await self.dependency_manager.mark_daymind_diary_memories_deleted(today_str, session_id)
                marked_deleted = int(result.get("updated", 0) or 0)

            await self.scheduler._save_diary_local(today_str, diary_content)
            if self.config.get("store_diary_to_memory", True) and self.dependency_manager.has_livingmemory:
                target = session_id
                persona = self.session_persona_map.get(target) or self.session_persona_map.get(session_id)
                metadata = self.scheduler._build_diary_memory_metadata(today_str)
                metadata["replaces_memory_ids"] = []
                await self.dependency_manager.store_to_memory(
                    date_str=today_str,
                    content=diary_content,
                    session_id=target,
                    persona_id=persona,
                    metadata=metadata,
                )
            self.scheduler.diary_generated_today = True
            self._save_state()
            extra = f"\n已标记旧记忆为删除: {marked_deleted} 条" if marked_deleted else ""
            yield event.plain_result(f"今日日记：\n\n{diary_content}{extra}")
        else:
            yield event.plain_result("日记生成失败，请检查模型提供商配置")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("清除今日思考")
    async def clear_today_reflection(self, event: AstrMessageEvent):
        if not self.scheduler:
            yield event.plain_result("调度器未初始化")
            return
        result = await self.scheduler.reset_today_reflections()
        self._save_state()
        yield event.plain_result(
            f"已清空今日思考流。\n"
            f"日期: {result['date']}\n"
            f"本地文件已删除: {result['removed_local_file']}\n"
            f"当前状态已重置为空白。"
        )
