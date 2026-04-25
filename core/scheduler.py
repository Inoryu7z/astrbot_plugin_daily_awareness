""" 调度器模块 负责周期性思考和日记生成的调度 """

import asyncio
import datetime
import json
import re
import hashlib
from typing import Optional, Any
from pathlib import Path
from astrbot.api import logger

from .reflection import ReflectionGenerator
from .diary import DiaryGenerator
from .dream import DreamGenerator
from .dependency import DependencyManager
from .message_cache import MessageCache
from .silent_hours import SilentHoursChecker
from .mood import MoodManager, extract_mood_baseline_from_diary_text, MOOD_DECAY_INTERVAL_MINUTES
from .persona_utils import PersonaConfigMixin
from .dream_ops import DreamOperations
from .diary_ops import DiaryOperations


class AwarenessScheduler(PersonaConfigMixin, DreamOperations, DiaryOperations):
    """自我感知调度器（按人格分桶）"""

    RUNTIME_CONFIG_KEYS = {
        "reflection_retention_days",
        "diary_retention_days",
        "webui_default_window_days",
        "webui_default_theme",
        "webui_default_mode",
    }

    def __init__(
        self,
        context,
        config: dict,
        data_dir: str,
        reflection_generator: ReflectionGenerator,
        diary_generator: DiaryGenerator,
        dependency_manager: DependencyManager,
        message_cache: MessageCache,
        silent_hours: SilentHoursChecker,
        session_persona_map: dict[str, str] | None = None,
        mood_manager: MoodManager | None = None,
        state_persist_callback=None,
        session_persona_activity_map: dict[str, str] | None = None,
        dream_generator: DreamGenerator | None = None,
        diary_renderer=None,
    ):
        self.context = context
        self.config = config
        self.data_dir = data_dir
        self.reflection_generator = reflection_generator
        self.diary_generator = diary_generator
        self.dream_generator = dream_generator
        self.diary_renderer = diary_renderer
        self.dependency_manager = dependency_manager
        self.message_cache = message_cache
        self.silent_hours = silent_hours
        self.session_persona_map = session_persona_map if session_persona_map is not None else {}
        self.session_persona_activity_map = session_persona_activity_map if session_persona_activity_map is not None else {}
        self.mood_manager = mood_manager
        self.state_persist_callback = state_persist_callback

        self.runtime_config: dict[str, Any] = {
            "reflection_retention_days": self._safe_retention_days(config.get("reflection_retention_days", 3), 3),
            "diary_retention_days": self._safe_retention_days(config.get("diary_retention_days", -1), -1),
            "webui_default_window_days": self._safe_window_days(config.get("webui_default_window_days", 3), 3),
            "webui_default_theme": str(config.get("webui_default_theme", "galaxy") or "galaxy"),
            "webui_default_mode": str(config.get("webui_default_mode", "overview") or "overview"),
        }

        self.is_running = False
        self.scheduler_task: Optional[asyncio.Task] = None
        self.persona_states: dict[str, dict[str, Any]] = {}
        self._smart_silent_cache: dict[str, dict[str, str | None]] = {}

    async def start(self):
        if self.is_running:
            return
        self.is_running = True
        self.scheduler_task = asyncio.create_task(self._run_scheduler())
        logger.info(
            f"[Scheduler] 调度器已启动，已管理人格数：{len(self._enabled_personas())}"
        )

    async def stop(self):
        self.is_running = False
        if self.scheduler_task:
            self.scheduler_task.cancel()
            try:
                await self.scheduler_task
            except asyncio.CancelledError:
                pass
        logger.info("[Scheduler] 调度器已停止")

    def _persist_state(self):
        callback = self.state_persist_callback
        if not callback:
            return
        try:
            result = callback()
            if asyncio.iscoroutine(result):
                asyncio.ensure_future(result)
        except Exception as e:
            logger.warning(f"[Scheduler] 持久化运行状态失败: {e}")

    def _config_get(self, key: str, default=None):
        if key in self.RUNTIME_CONFIG_KEYS:
            return self.runtime_config.get(key, default)
        return self.config.get(key, default)

    def _config_set(self, key: str, value):
        if key in self.RUNTIME_CONFIG_KEYS:
            self.runtime_config[key] = value
        else:
            self.config[key] = value

    def _is_debug_mode(self) -> bool:
        return bool(self.config.get("debug_mode", False))

    def _simplify_mood(self, mood: dict | None) -> dict | None:
        if not mood or not isinstance(mood, dict):
            return None
        item = dict(mood)
        item.pop("previous_mood", None)
        if self.mood_manager:
            item = self.mood_manager.validate_mood(item)
            item.pop("previous_mood", None)
        return item

    def _touch_session_persona(self, session_id: str | None, persona_name: str | None = None):
        session_key = str(session_id or "").strip()
        if not session_key:
            return
        if persona_name is not None:
            canonical_persona = self._canonical_persona_name(persona_name)
            if canonical_persona:
                self.session_persona_map[session_key] = canonical_persona
        if session_key in self.session_persona_map:
            self.session_persona_activity_map[session_key] = datetime.datetime.now().isoformat()

    def load_runtime_config(self, runtime_config: dict[str, Any] | None):
        runtime_config = runtime_config or {}
        if "reflection_retention_days" in runtime_config:
            self.runtime_config["reflection_retention_days"] = self._safe_retention_days(runtime_config.get("reflection_retention_days"), 3)
        if "diary_retention_days" in runtime_config:
            self.runtime_config["diary_retention_days"] = self._safe_retention_days(runtime_config.get("diary_retention_days"), -1)
        if "webui_default_window_days" in runtime_config:
            self.runtime_config["webui_default_window_days"] = self._safe_window_days(runtime_config.get("webui_default_window_days"), 3)
        if "webui_default_theme" in runtime_config:
            self.runtime_config["webui_default_theme"] = str(runtime_config.get("webui_default_theme") or "galaxy").strip() or "galaxy"
        if "webui_default_mode" in runtime_config:
            self.runtime_config["webui_default_mode"] = str(runtime_config.get("webui_default_mode") or "overview").strip() or "overview"

    def get_runtime_config(self) -> dict[str, Any]:
        return {
            "reflection_retention_days": self._safe_retention_days(self.runtime_config.get("reflection_retention_days", 3), 3),
            "diary_retention_days": self._safe_retention_days(self.runtime_config.get("diary_retention_days", -1), -1),
            "webui_default_window_days": self._safe_window_days(self.runtime_config.get("webui_default_window_days", 3), 3),
            "webui_default_theme": str(self.runtime_config.get("webui_default_theme", "galaxy") or "galaxy"),
            "webui_default_mode": str(self.runtime_config.get("webui_default_mode", "overview") or "overview"),
        }

    async def update_runtime_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        changed: dict[str, Any] = {}
        if "reflection_retention_days" in updates:
            value = self._safe_retention_days(updates.get("reflection_retention_days"), 3)
            self._config_set("reflection_retention_days", value)
            changed["reflection_retention_days"] = value
        if "diary_retention_days" in updates:
            value = self._safe_retention_days(updates.get("diary_retention_days"), -1)
            self._config_set("diary_retention_days", value)
            changed["diary_retention_days"] = value
        if "webui_default_window_days" in updates:
            value = self._safe_window_days(updates.get("webui_default_window_days"), 3)
            self._config_set("webui_default_window_days", value)
            changed["webui_default_window_days"] = value
        if "webui_default_theme" in updates:
            value = str(updates.get("webui_default_theme") or "galaxy").strip() or "galaxy"
            self._config_set("webui_default_theme", value)
            changed["webui_default_theme"] = value
        if "webui_default_mode" in updates:
            value = str(updates.get("webui_default_mode") or "overview").strip() or "overview"
            self._config_set("webui_default_mode", value)
            changed["webui_default_mode"] = value

        if "reflection_retention_days" in changed:
            await self._apply_reflection_retention()
        if "diary_retention_days" in changed:
            await self._apply_diary_retention()
        self._persist_state()
        return self.get_runtime_config()

    def _enabled_personas(self) -> list[str]:
        enabled: list[str] = []
        seen: set[str] = set()
        for item in self._persona_entries():
            name = self._canonical_persona_name(item.get("persona_name") or item.get("name") or item.get("select_persona"))
            if not name:
                continue
            token = self._normalize_persona_token(name)
            if token in seen:
                continue
            enabled.append(name)
            seen.add(token)
        return enabled

    def is_persona_enabled(self, persona_name: str | None) -> bool:
        canonical = self._canonical_persona_name(persona_name)
        if not canonical:
            return False
        return canonical in self._enabled_personas()

    def _ensure_persona_state(self, persona_name: str) -> dict[str, Any]:
        persona_name = self._canonical_persona_name(persona_name) or "未命名人格"
        if persona_name not in self.persona_states:
            now = datetime.datetime.now()
            self.persona_states[persona_name] = {
                "state_date": now.strftime("%Y-%m-%d"),
                "state_created_at": now.isoformat(),
                "current_awareness_text": "",
                "today_reflections": [],
                "last_reflection_time": None,
                "last_auto_reflection_time": None,
                "last_reflection_failure_time": None,
                "last_reflection_cooldown_until": None,
                "last_diary_date": "",
                "last_diary_check_minute": -1,
                "diary_generated_today": False,
                "last_auto_diary_trigger_key": "",
                "diary_memory_version_counter": {},
                "current_mood": None,
                "previous_mood": None,
                "today_moods": [],
                "consecutive_failures": 0,
                "last_reflection_error_code": None,
                "last_reflection_error_message": None,
                "last_reflection_error_time": None,
                "last_diary_error_code": None,
                "last_diary_error_message": None,
                "last_diary_error_time": None,
                "last_diary_failure_time": None,
                "last_diary_cooldown_until": None,
                "last_diary_failed_trigger_key": "",
                "last_dedupe_hit": False,
                "last_dedupe_mode": "none",
                "last_dedupe_source": None,
                "last_selected_session_id": None,
                "last_selected_session_source": "none",
                "mood_baseline": "平静",
                "last_mood_decay_time": None,
                "dream_state": {
                    "tonight_dreams": [],
                    "dream_count": 0,
                    "max_dreams_tonight": 1,
                    "sleep_start_time": None,
                    "last_dream_time": None,
                    "dream_memory": None,
                    "dream_shared": False,
                    "dream_aftereffect": None,
                    "was_silent_last_cycle": False,
                },
            }
        return self.persona_states[persona_name]

    def export_persona_states(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for persona_name, state in self.persona_states.items():
            canonical = self._canonical_persona_name(persona_name) or persona_name
            payload[canonical] = {
                "state_date": str(state.get("state_date") or ""),
                "state_created_at": str(state.get("state_created_at") or ""),
                "current_awareness_text": state.get("current_awareness_text", ""),
                "today_reflections": list(state.get("today_reflections", []) or []),
                "last_reflection_time": state.get("last_reflection_time").isoformat() if state.get("last_reflection_time") else None,
                "last_auto_reflection_time": state.get("last_auto_reflection_time").isoformat() if state.get("last_auto_reflection_time") else None,
                "last_reflection_failure_time": state.get("last_reflection_failure_time").isoformat() if state.get("last_reflection_failure_time") else None,
                "last_reflection_cooldown_until": state.get("last_reflection_cooldown_until").isoformat() if state.get("last_reflection_cooldown_until") else None,
                "last_diary_date": state.get("last_diary_date", ""),
                "last_diary_check_minute": int(state.get("last_diary_check_minute", -1) or -1),
                "diary_generated_today": bool(state.get("diary_generated_today", False)),
                "last_auto_diary_trigger_key": str(state.get("last_auto_diary_trigger_key") or ""),
                "diary_memory_version_counter": self._trim_diary_memory_version_counter(state.get("diary_memory_version_counter")),
                "current_mood": self._simplify_mood(state.get("current_mood")),
                "previous_mood": self._simplify_mood(state.get("previous_mood")),
                "today_moods": [self._simplify_mood(x) for x in (state.get("today_moods", []) or []) if self._simplify_mood(x)],
                "consecutive_failures": self._safe_non_negative_int(state.get("consecutive_failures", 0), default=0),
                "last_reflection_error_code": state.get("last_reflection_error_code"),
                "last_reflection_error_message": state.get("last_reflection_error_message"),
                "last_reflection_error_time": state.get("last_reflection_error_time"),
                "last_diary_error_code": state.get("last_diary_error_code"),
                "last_diary_error_message": state.get("last_diary_error_message"),
                "last_diary_error_time": state.get("last_diary_error_time"),
                "last_diary_failure_time": state.get("last_diary_failure_time").isoformat() if state.get("last_diary_failure_time") else None,
                "last_diary_cooldown_until": state.get("last_diary_cooldown_until").isoformat() if state.get("last_diary_cooldown_until") else None,
                "last_diary_failed_trigger_key": str(state.get("last_diary_failed_trigger_key") or ""),
                "last_dedupe_hit": bool(state.get("last_dedupe_hit", False)),
                "last_dedupe_mode": str(state.get("last_dedupe_mode") or "none"),
                "last_dedupe_source": state.get("last_dedupe_source"),
                "last_selected_session_id": state.get("last_selected_session_id"),
                "last_selected_session_source": str(state.get("last_selected_session_source") or "none"),
                "dream_state": state.get("dream_state", {}),
            }
        return payload

    def restore_persona_states(self, saved: dict[str, Any]):
        self.persona_states = {}
        enabled = set(self._enabled_personas())
        for persona_name, state in (saved or {}).items():
            if not isinstance(state, dict):
                continue
            canonical_name = self._canonical_persona_name(persona_name)
            if not canonical_name:
                continue
            if canonical_name not in enabled:
                logger.debug(f"[Scheduler] 跳过未启用人格的状态恢复: {canonical_name}")
                continue
            item = self._ensure_persona_state(canonical_name)
            state_date = str(state.get("state_date") or "").strip()
            item["state_date"] = state_date or datetime.datetime.now().strftime("%Y-%m-%d")
            state_created_at = str(state.get("state_created_at") or "").strip()
            if state_created_at:
                item["state_created_at"] = state_created_at
            item["current_awareness_text"] = str(state.get("current_awareness_text") or "")
            item["today_reflections"] = [str(x) for x in (state.get("today_reflections") or []) if str(x).strip()]
            for field in ["last_reflection_time", "last_auto_reflection_time", "last_reflection_failure_time", "last_reflection_cooldown_until"]:
                raw = state.get(field)
                try:
                    item[field] = datetime.datetime.fromisoformat(raw) if raw else None
                except Exception:
                    item[field] = None
            item["last_diary_date"] = str(state.get("last_diary_date") or "")
            item["last_diary_check_minute"] = self._safe_int(state.get("last_diary_check_minute"), -1)
            item["diary_generated_today"] = bool(state.get("diary_generated_today", False))
            item["last_auto_diary_trigger_key"] = str(state.get("last_auto_diary_trigger_key") or "")
            item["diary_memory_version_counter"] = self._trim_diary_memory_version_counter(state.get("diary_memory_version_counter"))
            item["current_mood"] = self._simplify_mood(state.get("current_mood"))
            item["previous_mood"] = self._simplify_mood(state.get("previous_mood"))
            item["today_moods"] = [self._simplify_mood(x) for x in (state.get("today_moods", []) or []) if self._simplify_mood(x)]
            item["consecutive_failures"] = self._safe_non_negative_int(state.get("consecutive_failures", 0), default=0)
            item["last_reflection_error_code"] = state.get("last_reflection_error_code")
            item["last_reflection_error_message"] = state.get("last_reflection_error_message")
            item["last_reflection_error_time"] = state.get("last_reflection_error_time")
            item["last_diary_error_code"] = state.get("last_diary_error_code")
            item["last_diary_error_message"] = state.get("last_diary_error_message")
            item["last_diary_error_time"] = state.get("last_diary_error_time")
            for field in ["last_diary_failure_time", "last_diary_cooldown_until"]:
                raw = state.get(field)
                try:
                    item[field] = datetime.datetime.fromisoformat(raw) if raw else None
                except Exception:
                    item[field] = None
            item["last_diary_failed_trigger_key"] = str(state.get("last_diary_failed_trigger_key") or "")
            item["last_dedupe_hit"] = bool(state.get("last_dedupe_hit", False))
            item["last_dedupe_mode"] = str(state.get("last_dedupe_mode") or "none")
            item["last_dedupe_source"] = state.get("last_dedupe_source")
            item["last_selected_session_id"] = state.get("last_selected_session_id")
            item["last_selected_session_source"] = str(state.get("last_selected_session_source") or "none")
            saved_dream_state = state.get("dream_state")
            if isinstance(saved_dream_state, dict):
                item["dream_state"] = saved_dream_state

    async def get_current_awareness_for_session(self, session_id: str | None) -> str:
        if not session_id:
            return ""
        self._touch_session_persona(session_id)
        persona_name = self._canonical_persona_name(self.session_persona_map.get(session_id))
        if not persona_name:
            return ""
        state = self.persona_states.get(persona_name) or {}
        return str(state.get("current_awareness_text") or "")

    def get_current_mood_for_session(self, session_id: str | None) -> dict | None:
        if not session_id:
            return None
        self._touch_session_persona(session_id)
        persona_name = self._canonical_persona_name(self.session_persona_map.get(session_id))
        if not persona_name:
            return None
        return self.get_current_mood_for_persona(persona_name)

    def get_current_mood_for_persona(self, persona_name: str | None) -> dict | None:
        normalized = self._canonical_persona_name(persona_name)
        if not normalized:
            return None
        state = self.persona_states.get(normalized)
        if not state:
            return None
        current = self._simplify_mood(state.get("current_mood"))
        if not current:
            return None
        previous = self._simplify_mood(state.get("previous_mood"))
        if previous:
            current["previous_mood"] = previous
        return current

    def get_previous_mood_for_persona(self, persona_name: str | None) -> dict | None:
        normalized = self._canonical_persona_name(persona_name)
        if not normalized:
            return None
        state = self.persona_states.get(normalized)
        if not state:
            return None
        return self._simplify_mood(state.get("previous_mood"))

    def get_today_moods_for_persona(self, persona_name: str | None, limit: int = 10) -> list[dict]:
        normalized = self._canonical_persona_name(persona_name)
        if not normalized:
            return []
        state = self.persona_states.get(normalized)
        if not state:
            return []
        moods = [self._simplify_mood(x) for x in (state.get("today_moods", []) or []) if self._simplify_mood(x)]
        if limit > 0:
            return moods[-limit:]
        return moods

    def get_mood_context(self, persona_name: str | None = None, session_id: str | None = None) -> dict | None:
        mood = None
        if session_id:
            mood = self.get_current_mood_for_session(session_id)
        if not mood and persona_name:
            mood = self.get_current_mood_for_persona(persona_name)
        return mood

    def _run_today_reset_for_persona(self, persona_name: str, today_str: str):
        state = self._ensure_persona_state(persona_name)
        state["state_date"] = today_str
        state["state_created_at"] = datetime.datetime.now().isoformat()
        state["today_reflections"] = []
        state["current_awareness_text"] = ""
        state["last_reflection_time"] = None
        state["last_auto_reflection_time"] = None
        state["last_reflection_failure_time"] = None
        state["last_reflection_cooldown_until"] = None
        state["last_diary_check_minute"] = -1
        state["diary_generated_today"] = False
        state["last_auto_diary_trigger_key"] = ""
        state["current_mood"] = None
        state["previous_mood"] = None
        state["today_moods"] = []
        state["consecutive_failures"] = 0
        state["last_dedupe_hit"] = False
        state["last_dedupe_mode"] = "none"
        state["last_dedupe_source"] = None
        self._clear_reflection_error(persona_name)
        self._clear_diary_error(persona_name)
        state["last_diary_failure_time"] = None
        state["last_diary_cooldown_until"] = None
        state["last_diary_failed_trigger_key"] = ""
        state["last_mood_decay_time"] = None
        state["dream_state"] = {
            "tonight_dreams": [],
            "dream_count": 0,
            "max_dreams_tonight": 1,
            "sleep_start_time": None,
            "last_dream_time": None,
            "dream_memory": None,
            "dream_shared": False,
            "dream_aftereffect": None,
            "was_silent_last_cycle": False,
        }
        baseline = "平静"
        try:
            yesterday = (datetime.datetime.strptime(today_str, "%Y-%m-%d") - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
            diary_item = self.get_diary_item(yesterday, persona_name)
            if diary_item and diary_item.get("content"):
                baseline = extract_mood_baseline_from_diary_text(diary_item["content"])
        except Exception as e:
            logger.debug(f"[Scheduler] 提取昨日日记基线失败: persona={persona_name}, error={e}")
        state["mood_baseline"] = baseline
        logger.info(f"[Scheduler] 心情基线初始化: persona={persona_name}, baseline={baseline}")

    async def reset_today_reflections(self, persona_name: str | None = None) -> dict[str, Any]:
        now = datetime.datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        target_persona = self._canonical_persona_name(persona_name)
        if not target_persona:
            enabled = self._enabled_personas()
            target_persona = enabled[0] if enabled else None
        if not target_persona:
            return {"date": today_str, "persona_name": "（未识别）", "removed_local_file": False, "today_reflections_count": 0, "current_awareness_text": ""}
        reflections_file = self._reflection_day_path(today_str, target_persona)
        removed_local_file = False
        if reflections_file.exists():
            reflections_file.unlink(missing_ok=True)
            removed_local_file = True
        state = self._ensure_persona_state(target_persona)
        state["state_date"] = today_str
        state["state_created_at"] = now.isoformat()
        state["today_reflections"] = []
        state["current_awareness_text"] = ""
        state["last_reflection_time"] = None
        state["last_auto_reflection_time"] = None
        state["last_reflection_failure_time"] = None
        state["last_reflection_cooldown_until"] = None
        state["current_mood"] = None
        state["previous_mood"] = None
        state["today_moods"] = []
        state["last_dedupe_hit"] = False
        state["last_dedupe_mode"] = "none"
        state["last_dedupe_source"] = None
        state["consecutive_failures"] = 0
        self._clear_reflection_error(target_persona)
        self._persist_state()
        return {"date": today_str, "persona_name": target_persona, "removed_local_file": removed_local_file, "today_reflections_count": 0, "current_awareness_text": ""}

    def _sanitize_persona_path(self, persona_name: str) -> str:
        canonical = self._canonical_persona_name(persona_name) or persona_name
        return re.sub(r'[\\/:*?"<>|]+', '_', canonical).strip() or '未命名人格'

    def _reflections_dir(self, persona_name: str | None = None) -> Path:
        base = Path(self.data_dir) / "reflections"
        if not persona_name:
            return base
        return base / self._sanitize_persona_path(persona_name)

    def _reflection_day_path(self, date_str: str, persona_name: str) -> Path:
        return self._reflections_dir(persona_name) / f"{date_str}.json"

    def _load_json_file(self, file_path: Path, default):
        if not file_path.exists():
            return default
        try:
            return json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def _write_json_file(self, file_path: Path, payload):
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _build_default_reflection_day_meta(self, date_str: str, persona_name: str) -> dict[str, Any]:
        return {"date": date_str, "persona_name": persona_name, "starred": False, "note": "", "updated_at": datetime.datetime.now().isoformat()}

    def _load_reflection_day_rows(self, date_str: str, persona_name: str) -> list[dict[str, Any]]:
        rows = self._load_json_file(self._reflection_day_path(date_str, persona_name), [])
        if not isinstance(rows, list):
            return []
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                normalized.append(row)
            elif isinstance(row, str):
                normalized.append({"time": "", "content": row, "created_at": ""})
        return normalized

    def _extract_reflection_day_meta(self, date_str: str, persona_name: str, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        rows = rows if rows is not None else self._load_reflection_day_rows(date_str, persona_name)
        meta = self._build_default_reflection_day_meta(date_str, persona_name)
        if rows:
            last = rows[-1]
            if isinstance(last, dict):
                if "day_meta" in last and isinstance(last.get("day_meta"), dict):
                    meta.update(last["day_meta"])
                elif "starred" in last or "note" in last:
                    meta["starred"] = bool(last.get("starred", False))
                    meta["note"] = str(last.get("note") or "")
        meta["starred"] = bool(meta.get("starred", False))
        meta["note"] = str(meta.get("note") or "")
        return meta

    def _save_reflection_day_rows_with_meta(self, date_str: str, persona_name: str, rows: list[dict[str, Any]], meta: dict[str, Any] | None = None):
        final_rows: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            item.pop("starred", None)
            item.pop("note", None)
            item.pop("day_meta", None)
            final_rows.append(item)
        final_meta = self._build_default_reflection_day_meta(date_str, persona_name)
        if meta:
            final_meta.update(meta)
        final_meta["updated_at"] = datetime.datetime.now().isoformat()
        if final_rows:
            final_rows[-1]["day_meta"] = {"starred": bool(final_meta.get("starred", False)), "note": str(final_meta.get("note") or ""), "updated_at": final_meta.get("updated_at"), "persona_name": persona_name}
        self._write_json_file(self._reflection_day_path(date_str, persona_name), final_rows)

    def _scan_persona_dirs(self, root: Path) -> list[tuple[str, Path]]:
        if not root.exists():
            return []
        pairs: list[tuple[str, Path]] = []
        for item in root.iterdir():
            if item.is_dir():
                pairs.append((item.name, item))
        return pairs

    def get_reflection_day_item(self, date_str: str, persona_name: str | None = None) -> dict[str, Any] | None:
        if persona_name:
            canonical_name = self._canonical_persona_name(persona_name) or persona_name
            fp = self._reflection_day_path(date_str, canonical_name)
            if not fp.exists():
                return None
            rows = self._load_reflection_day_rows(date_str, canonical_name)
            meta = self._extract_reflection_day_meta(date_str, canonical_name, rows)
            return {"date": date_str, "persona_name": canonical_name, "count": len(rows), "items": rows, "starred": bool(meta.get("starred", False)), "note": str(meta.get("note") or "")}
        for dir_name, _ in self._scan_persona_dirs(self._reflections_dir()):
            item = self.get_reflection_day_item(date_str, dir_name)
            if item:
                return item
        return None

    def list_reflection_days(self, days: int | None = None, starred_only: bool = False) -> list[dict[str, Any]]:
        root = self._reflections_dir()
        if not root.exists():
            return []
        window_days = self._safe_window_days(days if days is not None else self._config_get("webui_default_window_days", 3), 3)
        items: list[dict[str, Any]] = []
        for persona_name, persona_dir in self._scan_persona_dirs(root):
            for fp in persona_dir.glob("*.json"):
                date_str = fp.stem.strip()
                if not self._date_in_window(date_str, window_days):
                    continue
                rows = self._load_reflection_day_rows(date_str, persona_name)
                meta = self._extract_reflection_day_meta(date_str, persona_name, rows)
                if starred_only and not meta.get("starred", False):
                    continue
                preview = rows[-1].get("content", "") if rows else ""
                items.append({"date": date_str, "persona_name": persona_name, "count": len(rows), "preview": self._build_preview(preview, limit=90), "first_time": rows[0].get("time", "") if rows else "", "last_time": rows[-1].get("time", "") if rows else "", "starred": bool(meta.get("starred", False)), "note": str(meta.get("note") or "")})
        items.sort(key=lambda x: (x["date"], x.get("persona_name", "")), reverse=True)
        return items

    async def set_reflection_day_starred(self, date_str: str, starred: bool, persona_name: str | None = None) -> dict[str, Any] | None:
        item = self.get_reflection_day_item(date_str, persona_name)
        if not item:
            return None
        persona_name = item.get("persona_name")
        rows = self._load_reflection_day_rows(date_str, persona_name)
        meta = self._extract_reflection_day_meta(date_str, persona_name, rows)
        meta["starred"] = bool(starred)
        self._save_reflection_day_rows_with_meta(date_str, persona_name, rows, meta)
        return self.get_reflection_day_item(date_str, persona_name)

    async def set_reflection_day_note(self, date_str: str, note: str, persona_name: str | None = None) -> dict[str, Any] | None:
        item = self.get_reflection_day_item(date_str, persona_name)
        if not item:
            return None
        persona_name = item.get("persona_name")
        rows = self._load_reflection_day_rows(date_str, persona_name)
        meta = self._extract_reflection_day_meta(date_str, persona_name, rows)
        meta["note"] = str(note or "")
        self._save_reflection_day_rows_with_meta(date_str, persona_name, rows, meta)
        return self.get_reflection_day_item(date_str, persona_name)

    def _safe_window_days(self, value, default: int) -> int:
        try:
            parsed = int(value)
            if parsed == -1:
                return -1
            return max(parsed, 1)
        except Exception:
            return default

    def _safe_int(self, value, default: int = 0) -> int:
        try:
            return int(value)
        except Exception:
            return default

    def _safe_seconds(self, value, default: int) -> int:
        try:
            return max(int(value), 0)
        except Exception:
            return default

    def _get_reflection_failure_retry_limit(self, persona_name: str | None) -> int:
        return self._safe_non_negative_int(self._persona_value(persona_name, "reflection_failure_retry_limit", 10), default=10)

    def _get_reflection_failure_retry_delay_seconds(self, persona_name: str | None) -> int:
        interval_minutes = float(self._persona_value(persona_name, "thinking_interval_minutes", 30) or 30)
        interval_seconds = max(int(interval_minutes * 60), 1)
        configured = self._persona_value(persona_name, "reflection_failure_retry_delay_seconds", None)
        if configured is not None:
            return self._safe_seconds(configured, min(interval_seconds, 300))
        return min(interval_seconds, 300)

    def _get_reflection_generation_retry_count(self, persona_name: str | None) -> int:
        return self._safe_non_negative_int(self._persona_value(persona_name, "reflection_generation_retry_count", 2), default=2)

    def _get_reflection_generation_retry_delay_seconds(self, persona_name: str | None) -> float:
        try:
            return max(float(self._persona_value(persona_name, "reflection_generation_retry_delay_seconds", 2)), 0.0)
        except Exception:
            return 2.0

    async def _run_reflection_generation_with_retries(
        self,
        current_time_str: str,
        selected_session_id: str | None,
        persona_name: str,
        resolved_desc: str | None,
    ) -> str | None:
        max_retries = self._get_reflection_generation_retry_count(persona_name)
        retry_delay = self._get_reflection_generation_retry_delay_seconds(persona_name)
        last_result = None
        for attempt in range(max_retries + 1):
            last_result = await self.reflection_generator.generate(
                current_time_str,
                selected_session_id,
                self._build_recent_reflections_text(persona_name),
                persona_name,
                resolved_desc,
            )
            if last_result:
                if attempt > 0:
                    logger.info(f"[Scheduler] 思考生成重试成功: persona={persona_name}, attempt={attempt + 1}")
                return last_result
            if attempt < max_retries:
                logger.warning(f"[Scheduler] 思考生成失败，准备重试: persona={persona_name}, attempt={attempt + 1}/{max_retries + 1}, delay={retry_delay}s")
                if retry_delay > 0:
                    await asyncio.sleep(retry_delay)
        return last_result

    def _get_interval_seconds(self, persona_name: str | None) -> int:
        interval_minutes = float(self._persona_value(persona_name, "thinking_interval_minutes", 30) or 30)
        return max(int(interval_minutes * 60), 1)

    def _get_interval_jitter_seconds(self, persona_name: str | None) -> int:
        return self._safe_non_negative_int(self._persona_value(persona_name, "thinking_interval_jitter_seconds", 0), 0)

    def _get_effective_reflection_interval_seconds(self, persona_name: str | None, anchor_time: datetime.datetime | None = None) -> int:
        base_seconds = self._get_interval_seconds(persona_name)
        jitter_seconds = self._get_interval_jitter_seconds(persona_name)
        if jitter_seconds <= 0:
            return base_seconds

        normalized = self._canonical_persona_name(persona_name) or "default"
        if anchor_time is None:
            anchor_time = datetime.datetime.now()
        seed_text = f"{normalized}|{anchor_time.strftime('%Y-%m-%d %H:%M:%S')}|{base_seconds}|{jitter_seconds}"
        digest = hashlib.md5(seed_text.encode("utf-8")).hexdigest()
        value = int(digest[:8], 16)
        offset = (value % (jitter_seconds * 2 + 1)) - jitter_seconds
        effective = max(base_seconds + offset, 1)
        return effective

    def _date_in_window(self, date_str: str, days: int) -> bool:
        if days == -1:
            return True
        try:
            d = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
            delta = (datetime.date.today() - d).days
            return 0 <= delta < days
        except Exception:
            return False

    def _get_first_reflection_anchor_time(self, state: dict[str, Any]) -> datetime.datetime:
        raw = str(state.get("state_created_at") or "").strip()
        if raw:
            try:
                return datetime.datetime.fromisoformat(raw)
            except Exception:
                pass
        now = datetime.datetime.now()
        state["state_created_at"] = now.isoformat()
        self._persist_state()
        return now

    def _seconds_until_persona_reflection_due(self, persona_name: str, now: datetime.datetime) -> float | None:
        persona_name = self._canonical_persona_name(persona_name)
        if not persona_name:
            return None
        if not bool(self._persona_value(persona_name, "enable_auto_reflection", True)):
            return None
        if self._is_persona_silent(persona_name):
            return None

        state = self._ensure_persona_state(persona_name)
        cooldown_until = state.get("last_reflection_cooldown_until")
        if cooldown_until:
            remaining = (cooldown_until - now).total_seconds()
            if remaining > 0:
                return max(remaining, 1.0)
            state["last_reflection_cooldown_until"] = None
            state["consecutive_failures"] = 0
            state["last_reflection_failure_time"] = None
            self._persist_state()

        last_failure_time = state.get("last_reflection_failure_time")
        consecutive_failures = self._safe_non_negative_int(state.get("consecutive_failures", 0), default=0)
        if last_failure_time and consecutive_failures > 0:
            retry_delay = self._get_reflection_failure_retry_delay_seconds(persona_name)
            retry_remaining = retry_delay - (now - last_failure_time).total_seconds()
            if retry_remaining > 0:
                return max(retry_remaining, 1.0)

        last_auto_time = state.get("last_auto_reflection_time")
        if last_auto_time:
            anchor_time = last_auto_time
        else:
            anchor_time = self._get_first_reflection_anchor_time(state)
        effective_interval_seconds = float(self._get_effective_reflection_interval_seconds(persona_name, anchor_time))
        elapsed = (now - anchor_time).total_seconds()
        remaining = effective_interval_seconds - elapsed
        if remaining <= 0:
            return 1.0
        return max(remaining, 1.0)

    def _should_run_auto_reflection(self, persona_name: str, now: datetime.datetime) -> bool:
        due_in = self._seconds_until_persona_reflection_due(persona_name, now)
        return due_in is not None and due_in <= 1.0

    def _seconds_until_persona_diary_trigger(self, persona_name: str, now: datetime.datetime, today_str: str) -> float | None:
        persona_name = self._canonical_persona_name(persona_name)
        if not persona_name:
            return None
        if not bool(self._persona_value(persona_name, "enable_auto_diary", True)):
            return None
        state = self._ensure_persona_state(persona_name)
        diary_hour, diary_minute = self._get_persona_diary_time(persona_name)
        target_dt = now.replace(hour=diary_hour, minute=diary_minute, second=0, microsecond=0)
        trigger_key = f"{today_str}@{diary_hour:02d}:{diary_minute:02d}"
        failed_trigger_key = str(state.get("last_diary_failed_trigger_key") or "")
        cooldown_until = state.get("last_diary_cooldown_until")
        if failed_trigger_key == trigger_key and cooldown_until:
            remaining = (cooldown_until - now).total_seconds()
            if remaining > 0:
                return max(remaining, 1.0)
            state["last_diary_cooldown_until"] = None
            state["last_diary_failure_time"] = None
            state["last_diary_failed_trigger_key"] = ""
            self._persist_state()
        last_auto_trigger_key = str(state.get("last_auto_diary_trigger_key") or "")
        if last_auto_trigger_key == trigger_key:
            return None
        seconds = (target_dt - now).total_seconds()
        if seconds <= 0:
            return 1.0
        return max(seconds, 1.0)

    async def _run_scheduler(self):
        logger.info("[Scheduler] 调度循环已进入运行态")
        while self.is_running:
            try:
                now = datetime.datetime.now()
                today_str = now.strftime("%Y-%m-%d")
                enabled_personas = self._enabled_personas()

                reset_happened = False
                for persona_name in enabled_personas:
                    state = self._ensure_persona_state(persona_name)
                    if str(state.get("state_date") or "") != today_str:
                        self._run_today_reset_for_persona(persona_name, today_str)
                        reset_happened = True
                if reset_happened:
                    self._prune_all_diary_memory_version_counters()
                    self._persist_state()

                current_total_minutes = now.hour * 60 + now.minute

                for persona_name in enabled_personas:
                    await self._refresh_smart_silent_cache(persona_name)
                    state = self._ensure_persona_state(persona_name)
                    auto_diary_enabled = bool(self._persona_value(persona_name, "enable_auto_diary", True))
                    diary_hour, diary_minute = self._get_persona_diary_time(persona_name)
                    target_total_minutes = diary_hour * 60 + diary_minute
                    trigger_key = f"{today_str}@{diary_hour:02d}:{diary_minute:02d}"

                    if auto_diary_enabled:
                        has_reached_target = current_total_minutes >= target_total_minutes
                        failed_trigger_key = str(state.get("last_diary_failed_trigger_key") or "")
                        cooldown_until = state.get("last_diary_cooldown_until")
                        if failed_trigger_key == trigger_key and cooldown_until:
                            remaining = (cooldown_until - now).total_seconds()
                            if remaining > 0:
                                continue
                            state["last_diary_cooldown_until"] = None
                            state["last_diary_failure_time"] = None
                            state["last_diary_failed_trigger_key"] = ""
                            self._persist_state()
                        last_auto_trigger_key = str(state.get("last_auto_diary_trigger_key") or "")
                        if has_reached_target and last_auto_trigger_key != trigger_key:
                            state["last_diary_check_minute"] = current_total_minutes
                            state["last_auto_diary_trigger_key"] = trigger_key
                            self._persist_state()
                            if self._is_debug_mode():
                                logger.info(f"[Scheduler][debug] 触发自动日记: persona={persona_name}, now={now.strftime('%H:%M:%S')}, target={diary_hour:02d}:{diary_minute:02d}, trigger_key={trigger_key}, overwrite_today={bool(self._persona_value(persona_name, 'allow_overwrite_today_diary', False))}")
                            result = await self._generate_and_push_diary(today_str, persona_name)
                            status = str((result or {}).get("status") or "")
                            if status not in {"success", "exists"}:
                                failure_time = datetime.datetime.now()
                                cooldown_seconds = self._get_diary_failure_cooldown_seconds(persona_name)
                                state["last_diary_failure_time"] = failure_time
                                state["last_diary_failed_trigger_key"] = trigger_key
                                state["last_diary_cooldown_until"] = failure_time + datetime.timedelta(seconds=cooldown_seconds) if cooldown_seconds > 0 else failure_time
                                if str(state.get("last_auto_diary_trigger_key") or "") == trigger_key:
                                    state["last_auto_diary_trigger_key"] = ""
                                self._persist_state()
                                logger.warning(f"[Scheduler] 自动日记失败进入冷却: persona={persona_name}, trigger_key={trigger_key}, status={status}, cooldown={cooldown_seconds}s")
                            else:
                                state["last_diary_failure_time"] = None
                                state["last_diary_cooldown_until"] = None
                                state["last_diary_failed_trigger_key"] = ""
                                self._persist_state()
                                if self._is_debug_mode():
                                    logger.info(f"[Scheduler][debug] 自动日记处理完成: persona={persona_name}, trigger_key={trigger_key}, status={status}")

                    auto_reflection_enabled = bool(self._persona_value(persona_name, "enable_auto_reflection", True))
                    if auto_reflection_enabled:
                        if self._is_persona_silent(persona_name):
                            logger.debug(f"[Scheduler] 当前处于静默时段，跳过思考: persona={persona_name}")
                        elif self._should_run_auto_reflection(persona_name, now):
                            if self._is_debug_mode():
                                logger.info(f"[Scheduler][debug] 触发自动思考: persona={persona_name}, now={now.strftime('%H:%M:%S')}")
                            await self._do_reflection(persona_name)

                    if self._is_persona_silent(persona_name):
                        dream_enabled = bool(self._persona_value(persona_name, "enable_dream", True))
                        if dream_enabled and self.dream_generator:
                            dream_state = state.setdefault("dream_state", {})
                            if not dream_state.get("was_silent_last_cycle"):
                                await self._on_enter_sleep(persona_name, now)
                            if self._should_dream(persona_name, now):
                                if self._is_debug_mode():
                                    logger.info(f"[Scheduler][debug] 触发梦境: persona={persona_name}, now={now.strftime('%H:%M:%S')}")
                                await self._do_dream(persona_name)
                            dream_state["was_silent_last_cycle"] = True
                    else:
                        dream_state = state.get("dream_state")
                        if dream_state and dream_state.get("was_silent_last_cycle"):
                            await self._on_wake_up(persona_name)
                            dream_state["was_silent_last_cycle"] = False

                    if self.mood_manager and self.mood_manager.is_mood_enabled(persona_name):
                        self._try_mood_decay(persona_name, now)

                sleep_candidates = []
                for persona_name in enabled_personas:
                    reflection_candidate = self._seconds_until_persona_reflection_due(persona_name, now)
                    if reflection_candidate is not None:
                        sleep_candidates.append(reflection_candidate)
                    diary_candidate = self._seconds_until_persona_diary_trigger(persona_name, now, today_str)
                    if diary_candidate is not None:
                        sleep_candidates.append(diary_candidate)
                    silent_end_candidate = self._seconds_until_persona_silent_ends(persona_name)
                    if silent_end_candidate is not None:
                        sleep_candidates.append(silent_end_candidate)

                sleep_seconds = min(sleep_candidates) if sleep_candidates else 60.0
                if self._is_debug_mode():
                    logger.info(f"[Scheduler][debug] 本轮调度休眠 {sleep_seconds:.1f} 秒")
                await asyncio.sleep(max(sleep_seconds, 1.0))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Scheduler] 调度异常: {e}", exc_info=True)
                await asyncio.sleep(60)

    def _normalize_text_for_dedupe(self, text: str) -> str:
        if not text:
            return ""
        text = re.sub(r"^\s*\d{1,2}:\d{2}[，,：:]?\s*", "", text)
        text = text.lower()
        text = re.sub(r"[，。！？；：、,.!?;:\-—（）()\[\]{}\"'“”‘’…·]", " ", text)
        text = re.sub(r"\s+", "", text)
        return text

    def _extract_dedupe_tokens(self, text: str) -> set[str]:
        normalized = self._normalize_text_for_dedupe(text)
        if not normalized:
            return set()
        stop_tokens = {"现在", "此刻", "这会", "这会儿", "感觉", "有点", "一些", "正在", "就是", "还是", "似乎", "自己", "今天", "刚刚", "目前", "然后", "因为", "所以", "已经", "有些", "一种"}
        tokens: set[str] = set()
        for i in range(len(normalized) - 1):
            bg = normalized[i:i + 2]
            if bg and bg not in stop_tokens:
                tokens.add(bg)
        for i in range(len(normalized) - 2):
            tg = normalized[i:i + 3]
            if tg and tg not in stop_tokens:
                tokens.add(tg)
        return tokens

    def _calc_similarity(self, text_a: str, text_b: str) -> float:
        tokens_a = self._extract_dedupe_tokens(text_a)
        tokens_b = self._extract_dedupe_tokens(text_b)
        if not tokens_a or not tokens_b:
            return 0.0
        inter = len(tokens_a & tokens_b)
        union = len(tokens_a | tokens_b)
        if union == 0:
            return 0.0
        return inter / union

    def _get_similarity_threshold(self) -> float | None:
        return self._get_similarity_threshold_for_persona(None)

    def _get_similarity_threshold_for_persona(self, persona_name: str | None) -> float | None:
        mode = self._persona_value(persona_name, "reflection_dedupe_mode", self.config.get("reflection_dedupe_mode", "普通"))
        if mode == "严格":
            return 0.62
        if mode == "普通":
            return 0.72
        if mode == "无限制":
            return None
        return 0.72

    def _mark_dedupe(self, persona_name: str, hit: bool, mode: str = "none", source: Optional[str] = None):
        state = self._ensure_persona_state(persona_name)
        state["last_dedupe_hit"] = hit
        state["last_dedupe_mode"] = mode
        state["last_dedupe_source"] = source

    def _record_reflection_error(self, persona_name: str, code: str, message: str):
        state = self._ensure_persona_state(persona_name)
        state["last_reflection_error_code"] = code
        state["last_reflection_error_message"] = message
        state["last_reflection_error_time"] = datetime.datetime.now().strftime("%H:%M:%S")

    def _clear_reflection_error(self, persona_name: str):
        state = self._ensure_persona_state(persona_name)
        state["last_reflection_error_code"] = None
        state["last_reflection_error_message"] = None
        state["last_reflection_error_time"] = None

    def _record_diary_error(self, persona_name: str, code: str, message: str):
        state = self._ensure_persona_state(persona_name)
        state["last_diary_error_code"] = code
        state["last_diary_error_message"] = message
        state["last_diary_error_time"] = datetime.datetime.now().strftime("%H:%M:%S")

    def _clear_diary_error(self, persona_name: str):
        state = self._ensure_persona_state(persona_name)
        state["last_diary_error_code"] = None
        state["last_diary_error_message"] = None
        state["last_diary_error_time"] = None

    async def select_reflection_session(self, persona_name: str) -> str | None:
        canonical_persona = self._canonical_persona_name(persona_name)
        if not canonical_persona:
            return None
        state = self._ensure_persona_state(canonical_persona)

        last_selected_session_id = str(state.get("last_selected_session_id") or "").strip()
        if last_selected_session_id and self._canonical_persona_name(self.session_persona_map.get(last_selected_session_id)) == canonical_persona:
            state["last_selected_session_id"] = last_selected_session_id
            state["last_selected_session_source"] = "last_selected_session"
            self._touch_session_persona(last_selected_session_id)
            return last_selected_session_id

        recent_session_ids = await self.message_cache.get_recent_session_ids()
        for session_id in recent_session_ids:
            if self._canonical_persona_name(self.session_persona_map.get(session_id)) == canonical_persona:
                state["last_selected_session_id"] = session_id
                state["last_selected_session_source"] = "recent_message"
                self._touch_session_persona(session_id)
                return session_id
        session_ids = await self.message_cache.get_all_session_ids()
        for session_id in session_ids:
            if self._canonical_persona_name(self.session_persona_map.get(session_id)) == canonical_persona:
                state["last_selected_session_id"] = session_id
                state["last_selected_session_source"] = "message_cache"
                self._touch_session_persona(session_id)
                return session_id
        for sid, pname in self.session_persona_map.items():
            if self._canonical_persona_name(pname) == canonical_persona:
                state["last_selected_session_id"] = sid
                state["last_selected_session_source"] = "persona_map_fallback"
                self._touch_session_persona(sid)
                return sid
        state["last_selected_session_id"] = None
        state["last_selected_session_source"] = "none"
        return None

    def _is_duplicate_reflection(self, persona_name: str, new_text: str) -> bool:
        persona_name = self._canonical_persona_name(persona_name) or persona_name
        state = self._ensure_persona_state(persona_name)
        if not new_text:
            self._mark_dedupe(persona_name, False)
            return False
        normalized_new = self._normalize_text_for_dedupe(new_text)
        threshold = self._get_similarity_threshold_for_persona(persona_name)
        exact_prefix_guard = bool(self._persona_value(persona_name, "reflection_exact_prefix_guard", True))
        current_awareness_text = str(state.get("current_awareness_text") or "")
        today_reflections = list(state.get("today_reflections") or [])
        if current_awareness_text:
            current_normalized = self._normalize_text_for_dedupe(current_awareness_text)
            if current_normalized == normalized_new:
                self._mark_dedupe(persona_name, True, "exact", "current_awareness")
                return True
            if threshold is not None and self._calc_similarity(current_awareness_text, new_text) >= threshold:
                self._mark_dedupe(persona_name, True, "similar", "current_awareness")
                return True
            if exact_prefix_guard and current_normalized[:24] and current_normalized[:24] == normalized_new[:24]:
                self._mark_dedupe(persona_name, True, "prefix", "current_awareness")
                return True
        if today_reflections:
            reference_count = self._safe_non_negative_int(self._persona_value(persona_name, "reflection_reference_count", 2), default=2)
            recent_tail = today_reflections[-max(reference_count, 2):]
            for index, old in enumerate(recent_tail, start=1):
                old_normalized = self._normalize_text_for_dedupe(old)
                if old_normalized == normalized_new:
                    self._mark_dedupe(persona_name, True, "exact", f"recent_{index}")
                    return True
                if threshold is not None and self._calc_similarity(old, new_text) >= threshold:
                    self._mark_dedupe(persona_name, True, "similar", f"recent_{index}")
                    return True
                if exact_prefix_guard and old_normalized[:24] and old_normalized[:24] == normalized_new[:24]:
                    self._mark_dedupe(persona_name, True, "prefix", f"recent_{index}")
                    return True
        self._mark_dedupe(persona_name, False)
        return False

    def _build_recent_reflections_text(self, persona_name: str) -> str:
        persona_name = self._canonical_persona_name(persona_name) or persona_name
        reference_count = self._safe_non_negative_int(self._persona_value(persona_name, "reflection_reference_count", 2), default=2)
        if reference_count <= 0:
            return "（不参考最近思考）"
        recent = self._ensure_persona_state(persona_name).get("today_reflections", [])[-reference_count:]
        if not recent:
            return "（暂无最近思考）"
        return "\n".join([f"- {x}" for x in recent])

    @staticmethod
    def _extract_sleep_end_from_timeline(timeline: list) -> str | None:
        if not timeline or not isinstance(timeline, list):
            return None
        first = timeline[0]
        if not isinstance(first, dict):
            return None
        time_start = str(first.get("time_start", "")).strip()
        time_end = str(first.get("time_end", "")).strip()
        if not time_end:
            return None
        try:
            sh, sm = int(time_start.split(":")[0]), int(time_start.split(":")[1]) if ":" in time_start else 0
            eh, em = int(time_end.split(":")[0]), int(time_end.split(":")[1]) if ":" in time_end else 0
        except (ValueError, IndexError):
            return None
        start_minutes = sh * 60 + sm
        end_minutes = eh * 60 + em
        if start_minutes > 360:
            return None
        if end_minutes <= start_minutes:
            return None
        if end_minutes > 720:
            return None
        return time_end

    async def _refresh_smart_silent_cache(self, persona_name: str):
        persona_name = self._canonical_persona_name(persona_name)
        smart_enabled = bool(self._persona_value(persona_name, "smart_silent_hours", self.config.get("smart_silent_hours", False)))
        if not smart_enabled:
            self._smart_silent_cache.pop(persona_name, None)
            return
        today_str = datetime.date.today().isoformat()
        cached = self._smart_silent_cache.get(persona_name)
        if cached and cached.get("date") == today_str and cached.get("sleep_end") is not None:
            return
        sleep_end = None
        try:
            if self.dependency_manager and self.dependency_manager.has_life_scheduler:
                data = await self.dependency_manager.get_schedule_data(persona_name=persona_name)
                if isinstance(data, dict):
                    timeline = data.get("timeline", [])
                    sleep_end = self._extract_sleep_end_from_timeline(timeline)
        except Exception as e:
            logger.debug(f"[Scheduler] 智能静默提取失败: persona={persona_name}, error={e}")
        self._smart_silent_cache[persona_name] = {"date": today_str, "sleep_end": sleep_end}
        if sleep_end:
            logger.info(f"[Scheduler] 智能静默: persona={persona_name}, 从日程提取睡眠结束时间={sleep_end}")
        elif self._is_debug_mode():
            logger.debug(f"[Scheduler] 智能静默: persona={persona_name}, 未能提取睡眠结束时间，将使用兜底值")

    def _get_effective_silent_end(self, persona_name: str) -> str:
        persona_name = self._canonical_persona_name(persona_name)
        smart_enabled = bool(self._persona_value(persona_name, "smart_silent_hours", self.config.get("smart_silent_hours", False)))
        if smart_enabled:
            cached = self._smart_silent_cache.get(persona_name)
            if cached and cached.get("sleep_end"):
                return cached["sleep_end"]
        return str(self._persona_value(persona_name, "silent_hours_end", self.config.get("silent_hours_end", "06:00")) or "06:00")

    def _get_silent_checker(self, persona_name: str | None) -> SilentHoursChecker:
        persona_name = self._canonical_persona_name(persona_name)
        enabled = bool(self._persona_value(persona_name, "silent_hours_enabled", self.config.get("silent_hours_enabled", True)))
        start_time = str(self._persona_value(persona_name, "silent_hours_start", self.config.get("silent_hours_start", "00:00")) or "00:00")
        end_time = self._get_effective_silent_end(persona_name)
        return SilentHoursChecker(start_time=start_time, end_time=end_time, enabled=enabled)

    def _is_persona_silent(self, persona_name: str | None) -> bool:
        return self._get_silent_checker(persona_name).is_silent()

    def _seconds_until_persona_silent_ends(self, persona_name: str | None) -> float | None:
        return self._get_silent_checker(persona_name).seconds_until_silent_ends()

    def _get_persona_silent_status(self, persona_name: str | None) -> dict:
        checker = self._get_silent_checker(persona_name)
        status = checker.get_status()
        persona_name = self._canonical_persona_name(persona_name)
        smart_enabled = bool(self._persona_value(persona_name, "smart_silent_hours", self.config.get("smart_silent_hours", False)))
        if smart_enabled:
            cached = self._smart_silent_cache.get(persona_name)
            status["smart_silent_hours"] = True
            status["smart_sleep_end"] = cached.get("sleep_end") if cached else None
            status["fallback_end"] = str(self._persona_value(persona_name, "silent_hours_end", self.config.get("silent_hours_end", "06:00")) or "06:00")
        else:
            status["smart_silent_hours"] = False
        return status

    def _safe_non_negative_int(self, value, default: int = 2) -> int:
        try:
            return max(int(value), 0)
        except Exception:
            return default

    def _safe_retention_days(self, value, default: int) -> int:
        try:
            parsed = int(value)
            if parsed == -1:
                return -1
            return max(parsed, 0)
        except Exception:
            return default

    def _retention_cutoff_date(self, keep_days: int) -> datetime.date:
        today = datetime.date.today()
        if keep_days == -1:
            return today
        if keep_days <= 0:
            return today
        return today - datetime.timedelta(days=keep_days - 1)

    def _trim_today_moods(self, persona_name: str):
        persona_name = self._canonical_persona_name(persona_name) or persona_name
        max_history = self.mood_manager.get_mood_max_history(persona_name) if self.mood_manager else 24
        state = self._ensure_persona_state(persona_name)
        moods = [self._simplify_mood(x) for x in (state.get("today_moods", []) or []) if self._simplify_mood(x)]
        if len(moods) > max_history:
            moods = moods[-max_history:]
        state["today_moods"] = moods

    def _try_mood_decay(self, persona_name: str, now: datetime.datetime):
        if not self.mood_manager:
            return
        persona_name = self._canonical_persona_name(persona_name)
        if not persona_name:
            return
        state = self._ensure_persona_state(persona_name)
        current_mood = state.get("current_mood")
        if not current_mood or not isinstance(current_mood, dict):
            return
        baseline_label = str(state.get("mood_baseline") or "平静").strip()
        last_decay_time = state.get("last_mood_decay_time")
        if last_decay_time:
            try:
                elapsed = (now - last_decay_time).total_seconds() / 60.0
            except Exception:
                elapsed = MOOD_DECAY_INTERVAL_MINUTES + 1
        else:
            try:
                mood_updated_at = str(current_mood.get("updated_at") or "").strip()
                if mood_updated_at:
                    mood_time = datetime.datetime.fromisoformat(mood_updated_at)
                    elapsed = (now - mood_time).total_seconds() / 60.0
                else:
                    elapsed = MOOD_DECAY_INTERVAL_MINUTES + 1
            except Exception:
                elapsed = MOOD_DECAY_INTERVAL_MINUTES + 1
        if elapsed < MOOD_DECAY_INTERVAL_MINUTES:
            return
        decayed = self.mood_manager.decay_current_mood(current_mood, baseline_label)
        if not decayed:
            return
        state["previous_mood"] = self._simplify_mood(current_mood)
        state["current_mood"] = decayed
        state["last_mood_decay_time"] = now
        state.setdefault("today_moods", []).append(decayed)
        self._trim_today_moods(persona_name)
        self._persist_state()
        logger.info(f"[Scheduler] 心情衰减: persona={persona_name}, {current_mood.get('label')} → {decayed.get('label')}")

    async def run_manual_reflection(self, session_id: str, persona_name: str | None, persona_desc: str | None = None) -> dict[str, Any]:
        persona_name = self._canonical_persona_name(persona_name)
        if not persona_name or not self.is_persona_enabled(persona_name):
            return {"status": "skipped", "message": "当前人格未启用 DayMind"}
        self._touch_session_persona(session_id, persona_name)
        return await self._do_reflection(persona_name, session_id=session_id, persona_desc=persona_desc, manual=True)

    async def _do_reflection(self, persona_name: str, session_id: str | None = None, persona_desc: str | None = None, manual: bool = False) -> dict[str, Any]:
        persona_name = self._canonical_persona_name(persona_name) or persona_name
        state = self._ensure_persona_state(persona_name)
        try:
            if not self.is_persona_enabled(persona_name):
                return {"status": "skipped", "message": f"人格未启用: {persona_name}"}
            now = datetime.datetime.now()
            state["state_date"] = now.strftime("%Y-%m-%d")
            current_time_str = now.strftime("%H:%M")
            selected_session_id = session_id or await self.select_reflection_session(persona_name)
            if not selected_session_id and manual:
                logger.warning(f"[Scheduler] 手动思考跳过：人格 {persona_name} 暂无可用会话")
                return {"status": "skipped", "message": f"人格 {persona_name} 暂无可用会话"}
            if not selected_session_id:
                logger.warning(f"[Scheduler] 自动思考未找到可用会话，将以无会话模式继续：persona={persona_name}")

            resolved_desc = persona_desc
            if selected_session_id:
                persona_ctx = await self.dependency_manager.resolve_persona_context(selected_session_id)
                resolved_desc = persona_desc or (persona_ctx.get("persona_desc") if persona_ctx else None)
                self._touch_session_persona(selected_session_id, persona_name)

            if self._is_debug_mode():
                logger.info(
                    f"[Scheduler][debug] 开始执行思考: persona={persona_name}, manual={manual}, session={selected_session_id}, session_source={state.get('last_selected_session_source')}"
                )

            result = await self._run_reflection_generation_with_retries(
                current_time_str=current_time_str,
                selected_session_id=selected_session_id,
                persona_name=persona_name,
                resolved_desc=resolved_desc,
            )
            if result:
                if self._is_duplicate_reflection(persona_name, result):
                    state["last_reflection_time"] = now
                    if not manual:
                        state["last_auto_reflection_time"] = now
                    state["last_reflection_failure_time"] = None
                    state["last_reflection_cooldown_until"] = None
                    state["consecutive_failures"] = 0
                    self._clear_reflection_error(persona_name)
                    self._persist_state()
                    logger.info(f"[Scheduler] 思考结果命中去重，跳过更新: persona={persona_name}, mode={state.get('last_dedupe_mode')}, source={state.get('last_dedupe_source')}")
                    return {"status": "duplicate", "text": result}
                state["current_awareness_text"] = result
                state.setdefault("today_reflections", []).append(result)
                state["last_reflection_time"] = now
                if not manual:
                    state["last_auto_reflection_time"] = now
                state["last_reflection_failure_time"] = None
                state["last_reflection_cooldown_until"] = None
                state["consecutive_failures"] = 0
                self._clear_reflection_error(persona_name)
                await self._append_reflection_history(now.strftime("%Y-%m-%d"), persona_name, result)
                await self._apply_reflection_retention()
                mood_result = None
                if self.mood_manager and self.mood_manager.is_mood_enabled(persona_name):
                    try:
                        previous_mood = self._simplify_mood(state.get("current_mood"))
                        mood_result = await self.mood_manager.generate_mood(result, persona_name)
                        mood_result = self._simplify_mood(self.mood_manager.validate_mood(mood_result))
                        state["previous_mood"] = previous_mood
                        state["current_mood"] = mood_result
                        if mood_result:
                            state.setdefault("today_moods", []).append(mood_result)
                        self._trim_today_moods(persona_name)
                        logger.info(f"[Scheduler] 心情生成完成: persona={persona_name}, mood={(mood_result or {}).get('label')}")
                    except Exception as e:
                        logger.warning(f"[Scheduler] 心情生成失败: {e}")
                self._persist_state()
                logger.info(f"[Scheduler] 思考完成: persona={persona_name}, result={result}")
                return {"status": "success", "text": result, "mood": self.get_current_mood_for_persona(persona_name) if mood_result else None}

            state["consecutive_failures"] = self._safe_non_negative_int(state.get("consecutive_failures", 0), default=0) + 1
            state["last_reflection_failure_time"] = now
            self._record_reflection_error(persona_name, "reflection_empty", "思考结果为空，请检查模型提供商配置")
            limit = self._get_reflection_failure_retry_limit(persona_name)
            logger.warning(f"[Scheduler] 思考失败（连续失败：{state['consecutive_failures']}次） persona={persona_name}")
            if limit > 0 and state["consecutive_failures"] >= limit and not manual:
                cooldown_seconds = self._get_interval_seconds(persona_name)
                state["last_reflection_cooldown_until"] = now + datetime.timedelta(seconds=cooldown_seconds)
                state["consecutive_failures"] = 0
                logger.warning(f"[Scheduler] 达到失败重试上限，进入冷却: persona={persona_name}, cooldown={cooldown_seconds}s")
            self._persist_state()
            return {"status": "failed", "message": "思考结果为空，请检查模型提供商配置"}
        except Exception as e:
            logger.error(f"[Scheduler] 思考过程出错: {e}", exc_info=True)
            now = datetime.datetime.now()
            state["state_date"] = now.strftime("%Y-%m-%d")
            state["consecutive_failures"] = self._safe_non_negative_int(state.get("consecutive_failures", 0), default=0) + 1
            state["last_reflection_failure_time"] = now
            self._record_reflection_error(persona_name, "reflection_exception", str(e))
            limit = self._get_reflection_failure_retry_limit(persona_name)
            if limit > 0 and state["consecutive_failures"] >= limit and not manual:
                cooldown_seconds = self._get_interval_seconds(persona_name)
                state["last_reflection_cooldown_until"] = now + datetime.timedelta(seconds=cooldown_seconds)
                state["consecutive_failures"] = 0
                logger.warning(f"[Scheduler] 达到失败重试上限，进入冷却: persona={persona_name}, cooldown={cooldown_seconds}s")
            self._persist_state()
            return {"status": "failed", "message": str(e)}

    async def _append_reflection_history(self, date_str: str, persona_name: str, content: str):
        try:
            canonical_persona = self._canonical_persona_name(persona_name) or persona_name
            history_dir = self._reflections_dir(canonical_persona)
            history_dir.mkdir(parents=True, exist_ok=True)
            items = self._load_reflection_day_rows(date_str, canonical_persona)
            day_meta = self._extract_reflection_day_meta(date_str, canonical_persona, items)
            items.append({"time": datetime.datetime.now().strftime("%H:%M:%S"), "content": content, "created_at": datetime.datetime.now().isoformat(), "persona_name": canonical_persona})
            self._save_reflection_day_rows_with_meta(date_str, canonical_persona, items, day_meta)
        except Exception as e:
            logger.debug(f"[Scheduler] 保存思考流失败: {e}")

    async def _apply_reflection_retention(self):
        try:
            keep_days = self._safe_retention_days(self._config_get("reflection_retention_days", 3), default=3)
            if keep_days == -1:
                return
            root = self._reflections_dir()
            if not root.exists():
                return
            cutoff = self._retention_cutoff_date(keep_days)
            for persona_name, persona_dir in self._scan_persona_dirs(root):
                for fp in persona_dir.glob("*.json"):
                    try:
                        file_date = datetime.datetime.strptime(fp.stem, "%Y-%m-%d").date()
                    except Exception:
                        continue
                    if file_date >= cutoff:
                        continue
                    meta = self._extract_reflection_day_meta(fp.stem, persona_name, self._load_reflection_day_rows(fp.stem, persona_name))
                    if bool(meta.get("starred", False)):
                        continue
                    fp.unlink(missing_ok=True)
        except Exception as e:
            logger.debug(f"[Scheduler] 应用思考流轮换失败: {e}")

    def get_status(self, persona_name: str | None = None) -> dict:
        canonical_persona = self._canonical_persona_name(persona_name)
        silent_status = self._get_persona_silent_status(canonical_persona)
        reference_count = self._safe_non_negative_int(self._persona_value(canonical_persona, "reflection_reference_count", 2), default=2)
        runtime_config = self.get_runtime_config()
        state = self._ensure_persona_state(canonical_persona) if canonical_persona and self.is_persona_enabled(canonical_persona) else None
        current_mood = None
        previous_mood = None
        today_moods_count = 0
        diary_memory_version = 0
        if state:
            current_mood = self.get_current_mood_for_persona(canonical_persona)
            previous_mood = self.get_previous_mood_for_persona(canonical_persona)
            today_moods_count = len(state.get("today_moods", []) or [])
            date_key = datetime.datetime.now().strftime("%Y-%m-%d")
            diary_memory_version = self._safe_non_negative_int((state.get("diary_memory_version_counter") or {}).get(date_key, 0), default=0)
        return {
            "is_running": self.is_running,
            "enable_auto_reflection": bool(self._persona_value(canonical_persona, "enable_auto_reflection", True)),
            "enable_auto_diary": bool(self._persona_value(canonical_persona, "enable_auto_diary", True)),
            "enabled_personas": self._enabled_personas(),
            "reflection_reference_count": reference_count,
            "state_date": state.get("state_date") if state else "",
            "state_created_at": state.get("state_created_at") if state else "",
            "current_awareness_text": state.get("current_awareness_text") if state else "",
            "today_reflections_count": len(state.get("today_reflections", [])) if state else 0,
            "last_reflection_time": state.get("last_reflection_time").strftime("%H:%M") if state and state.get("last_reflection_time") else None,
            "last_auto_reflection_time": state.get("last_auto_reflection_time").strftime("%H:%M:%S") if state and state.get("last_auto_reflection_time") else None,
            "last_reflection_failure_time": state.get("last_reflection_failure_time").strftime("%H:%M:%S") if state and state.get("last_reflection_failure_time") else None,
            "last_reflection_cooldown_until": state.get("last_reflection_cooldown_until").strftime("%H:%M:%S") if state and state.get("last_reflection_cooldown_until") else None,
            "consecutive_failures": self._safe_non_negative_int(state.get("consecutive_failures", 0), default=0) if state else 0,
            "reflection_failure_retry_limit": self._get_reflection_failure_retry_limit(canonical_persona),
            "reflection_failure_retry_delay_seconds": self._get_reflection_failure_retry_delay_seconds(canonical_persona),
            "reflection_generation_retry_count": self._get_reflection_generation_retry_count(canonical_persona),
            "reflection_generation_retry_delay_seconds": self._get_reflection_generation_retry_delay_seconds(canonical_persona),
            "diary_generation_retry_count": self._get_diary_generation_retry_count(canonical_persona),
            "diary_generation_retry_delay_seconds": self._get_diary_generation_retry_delay_seconds(canonical_persona),
            "silent_hours": silent_status,
            "diary_generated_today": bool(state.get("diary_generated_today", False)) if state else False,
            "last_diary_date": state.get("last_diary_date") if state else "",
            "last_auto_diary_trigger_key": state.get("last_auto_diary_trigger_key", "") if state else "",
            "primary_memory_target": state.get("last_selected_session_id") if state else None,
            "primary_persona_id": self._get_primary_persona_id(canonical_persona),
            "allow_overwrite_today_diary": bool(self._persona_value(canonical_persona, "allow_overwrite_today_diary", False)),
            "recent_reflections_preview": (state.get("today_reflections", [])[-max(reference_count, 2):] if state else []),
            "next_reflection_in_minutes": self._persona_value(canonical_persona, "thinking_interval_minutes", 30),
            "thinking_interval_jitter_seconds": self._safe_non_negative_int(self._persona_value(canonical_persona, "thinking_interval_jitter_seconds", 0), 0),
            "reflection_dedupe_mode": self._persona_value(canonical_persona, "reflection_dedupe_mode", "普通") or "普通",
            "reflection_dedupe_similarity_threshold": self._get_similarity_threshold_for_persona(canonical_persona),
            "last_reflection_error_code": state.get("last_reflection_error_code") if state else None,
            "last_reflection_error_message": state.get("last_reflection_error_message") if state else None,
            "last_reflection_error_time": state.get("last_reflection_error_time") if state else None,
            "last_diary_error_code": state.get("last_diary_error_code") if state else None,
            "last_diary_error_message": state.get("last_diary_error_message") if state else None,
            "last_diary_error_time": state.get("last_diary_error_time") if state else None,
            "last_diary_failure_time": state.get("last_diary_failure_time").strftime("%H:%M:%S") if state and state.get("last_diary_failure_time") else None,
            "last_diary_cooldown_until": state.get("last_diary_cooldown_until").strftime("%H:%M:%S") if state and state.get("last_diary_cooldown_until") else None,
            "last_diary_failed_trigger_key": state.get("last_diary_failed_trigger_key", "") if state else "",
            "last_dedupe_hit": bool(state.get("last_dedupe_hit", False)) if state else False,
            "last_dedupe_mode": state.get("last_dedupe_mode", "none") if state else "none",
            "last_dedupe_source": state.get("last_dedupe_source") if state else None,
            "last_selected_session_id": state.get("last_selected_session_id") if state else None,
            "last_selected_session_source": state.get("last_selected_session_source", "none") if state else "none",
            "diary_memory_version": diary_memory_version,
            "reflection_retention_days": runtime_config["reflection_retention_days"],
            "diary_retention_days": runtime_config["diary_retention_days"],
            "webui_default_window_days": runtime_config["webui_default_window_days"],
            "webui_default_theme": runtime_config["webui_default_theme"],
            "webui_default_mode": runtime_config["webui_default_mode"],
            "enable_mood_system": self.mood_manager.is_mood_enabled(canonical_persona) if self.mood_manager else bool(self._persona_value(canonical_persona, "enable_mood_system", True)),
            "current_mood": current_mood,
            "previous_mood": previous_mood,
            "today_moods_count": today_moods_count,
        }
