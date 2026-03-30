"""
调度器模块
负责周期性思考和日记生成的调度
"""

import asyncio
import datetime
import json
import re
from typing import Optional, Any
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import MessageChain

from .reflection import ReflectionGenerator
from .diary import DiaryGenerator
from .dependency import DependencyManager
from .message_cache import MessageCache
from .silent_hours import SilentHoursChecker


class AwarenessScheduler:
    """自我感知调度器"""

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
    ):
        self.context = context
        self.config = config
        self.data_dir = data_dir
        self.reflection_generator = reflection_generator
        self.diary_generator = diary_generator
        self.dependency_manager = dependency_manager
        self.message_cache = message_cache
        self.silent_hours = silent_hours
        self.session_persona_map = session_persona_map if session_persona_map is not None else {}

        self.runtime_config: dict[str, Any] = {
            "reflection_retention_days": self._safe_retention_days(config.get("reflection_retention_days", 3), 3),
            "diary_retention_days": self._safe_retention_days(config.get("diary_retention_days", -1), -1),
            "webui_default_window_days": self._safe_window_days(config.get("webui_default_window_days", 3), 3),
            "webui_default_theme": str(config.get("webui_default_theme", "galaxy") or "galaxy"),
            "webui_default_mode": str(config.get("webui_default_mode", "overview") or "overview"),
        }

        self.is_running = False
        self.scheduler_task: Optional[asyncio.Task] = None

        self.current_awareness_text: str = ""
        self.today_reflections: list[str] = []
        self.last_reflection_time: Optional[datetime.datetime] = None

        self.last_diary_date: str = ""
        self.last_diary_check_minute: int = -1
        self.diary_generated_today: bool = False

        self.consecutive_failures = 0
        self.max_consecutive_failures = 3

        self.last_reflection_error_code: Optional[str] = None
        self.last_reflection_error_message: Optional[str] = None
        self.last_reflection_error_time: Optional[str] = None
        self.last_diary_error_code: Optional[str] = None
        self.last_diary_error_message: Optional[str] = None
        self.last_diary_error_time: Optional[str] = None

        self.last_dedupe_hit: bool = False
        self.last_dedupe_mode: str = "none"
        self.last_dedupe_source: Optional[str] = None
        self.last_selected_session_id: Optional[str] = None
        self.last_selected_session_source: str = "none"
        self.diary_memory_version_counter: dict[str, int] = {}

    async def start(self):
        if self.is_running:
            return

        self.is_running = True
        self.scheduler_task = asyncio.create_task(self._run_scheduler())
        logger.info(f"[Scheduler] 调度器已启动，思考间隔：{self.config.get('thinking_interval_minutes', 30)}分钟")

    async def stop(self):
        self.is_running = False

        if self.scheduler_task:
            self.scheduler_task.cancel()
            try:
                await self.scheduler_task
            except asyncio.CancelledError:
                pass

        logger.info("[Scheduler] 调度器已停止")

    def _config_get(self, key: str, default=None):
        if key in self.RUNTIME_CONFIG_KEYS:
            return self.runtime_config.get(key, default)
        return self.config.get(key, default)

    def _config_set(self, key: str, value):
        if key in self.RUNTIME_CONFIG_KEYS:
            self.runtime_config[key] = value
        else:
            self.config[key] = value

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

        return self.get_runtime_config()

    def _run_today_reset(self, today_str: str):
        self.today_reflections = []
        self.current_awareness_text = ""
        self.last_diary_date = today_str
        self.last_diary_check_minute = -1
        self.diary_generated_today = False

    async def reset_today_reflections(self) -> dict[str, Any]:
        now = datetime.datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        reflections_file = Path(self.data_dir) / "reflections" / f"{today_str}.json"

        removed_local_file = False
        if reflections_file.exists():
            reflections_file.unlink(missing_ok=True)
            removed_local_file = True

        self.today_reflections = []
        self.current_awareness_text = ""
        self.last_reflection_time = None
        self.last_dedupe_hit = False
        self.last_dedupe_mode = "none"
        self.last_dedupe_source = None
        self.consecutive_failures = 0
        self._clear_reflection_error()

        return {
            "date": today_str,
            "removed_local_file": removed_local_file,
            "today_reflections_count": 0,
            "current_awareness_text": "",
        }

    def _diaries_dir(self) -> Path:
        return Path(self.data_dir) / "diaries"

    def _reflections_dir(self) -> Path:
        return Path(self.data_dir) / "reflections"

    def _diary_text_path(self, date_str: str) -> Path:
        return self._diaries_dir() / f"{date_str}.txt"

    def _diary_meta_path(self, date_str: str) -> Path:
        return self._diaries_dir() / f"{date_str}.json"

    def _reflection_day_path(self, date_str: str) -> Path:
        return self._reflections_dir() / f"{date_str}.json"

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

    def _build_default_diary_meta(self, date_str: str) -> dict[str, Any]:
        return {
            "date": date_str,
            "memory_status": "unknown",
            "starred": False,
            "note": "",
            "updated_at": datetime.datetime.now().isoformat(),
        }

    def _build_default_reflection_day_meta(self, date_str: str) -> dict[str, Any]:
        return {
            "date": date_str,
            "starred": False,
            "note": "",
            "updated_at": datetime.datetime.now().isoformat(),
        }

    def _load_diary_meta(self, date_str: str) -> dict[str, Any]:
        meta_file = self._diary_meta_path(date_str)
        data = self._load_json_file(meta_file, {})
        if not isinstance(data, dict):
            data = {}
        base = self._build_default_diary_meta(date_str)
        base.update(data)
        base["starred"] = bool(base.get("starred", False))
        base["note"] = str(base.get("note") or "")
        return base

    def _save_diary_meta_sync(self, date_str: str, payload: dict[str, Any]):
        final_payload = self._build_default_diary_meta(date_str)
        final_payload.update(payload or {})
        final_payload["updated_at"] = datetime.datetime.now().isoformat()
        self._write_json_file(self._diary_meta_path(date_str), final_payload)

    def _load_reflection_day_rows(self, date_str: str) -> list[dict[str, Any]]:
        rows = self._load_json_file(self._reflection_day_path(date_str), [])
        if not isinstance(rows, list):
            return []
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                normalized.append(row)
            elif isinstance(row, str):
                normalized.append({
                    "time": "",
                    "content": row,
                    "created_at": "",
                })
        return normalized

    def _extract_reflection_day_meta(self, date_str: str, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        rows = rows if rows is not None else self._load_reflection_day_rows(date_str)
        meta = self._build_default_reflection_day_meta(date_str)
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

    def _save_reflection_day_rows_with_meta(self, date_str: str, rows: list[dict[str, Any]], meta: dict[str, Any] | None = None):
        final_rows: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            item.pop("starred", None)
            item.pop("note", None)
            item.pop("day_meta", None)
            final_rows.append(item)

        final_meta = self._build_default_reflection_day_meta(date_str)
        if meta:
            final_meta.update(meta)
        final_meta["updated_at"] = datetime.datetime.now().isoformat()

        if final_rows:
            final_rows[-1]["day_meta"] = {
                "starred": bool(final_meta.get("starred", False)),
                "note": str(final_meta.get("note") or ""),
                "updated_at": final_meta.get("updated_at"),
            }

        self._write_json_file(self._reflection_day_path(date_str), final_rows)

    def get_diary_item(self, date_str: str) -> dict[str, Any] | None:
        txt_file = self._diary_text_path(date_str)
        if not txt_file.exists():
            return None
        content = txt_file.read_text(encoding="utf-8").strip()
        stat = txt_file.stat()
        meta = self._load_diary_meta(date_str)
        return {
            "date": date_str,
            "title": self._extract_title(content, date_str),
            "content": content,
            "updated_at": int(stat.st_mtime),
            "memory_status": meta.get("memory_status", "unknown"),
            "starred": bool(meta.get("starred", False)),
            "note": str(meta.get("note") or ""),
        }

    def list_diaries(self, days: int | None = None, starred_only: bool = False) -> list[dict[str, Any]]:
        diaries_dir = self._diaries_dir()
        if not diaries_dir.exists():
            return []

        window_days = self._safe_window_days(days if days is not None else self._config_get("webui_default_window_days", 3), 3)
        items: list[dict[str, Any]] = []
        for txt_file in diaries_dir.glob("*.txt"):
            date_str = txt_file.stem.strip()
            if not self._date_in_window(date_str, window_days):
                continue
            try:
                content = txt_file.read_text(encoding="utf-8").strip()
            except Exception:
                content = ""
            meta = self._load_diary_meta(date_str)
            if starred_only and not meta.get("starred", False):
                continue
            stat = txt_file.stat()
            items.append(
                {
                    "date": date_str,
                    "title": self._extract_title(content, date_str),
                    "preview": self._build_preview(content, limit=120),
                    "length": len(content),
                    "updated_at": int(stat.st_mtime),
                    "memory_status": meta.get("memory_status", "unknown"),
                    "starred": bool(meta.get("starred", False)),
                    "note": str(meta.get("note") or ""),
                }
            )

        items.sort(key=lambda x: x["date"], reverse=True)
        return items

    def get_reflection_day_item(self, date_str: str) -> dict[str, Any] | None:
        fp = self._reflection_day_path(date_str)
        if not fp.exists():
            return None
        rows = self._load_reflection_day_rows(date_str)
        meta = self._extract_reflection_day_meta(date_str, rows)
        return {
            "date": date_str,
            "count": len(rows),
            "items": rows,
            "starred": bool(meta.get("starred", False)),
            "note": str(meta.get("note") or ""),
        }

    def list_reflection_days(self, days: int | None = None, starred_only: bool = False) -> list[dict[str, Any]]:
        reflections_dir = self._reflections_dir()
        if not reflections_dir.exists():
            return []

        window_days = self._safe_window_days(days if days is not None else self._config_get("webui_default_window_days", 3), 3)
        items: list[dict[str, Any]] = []
        for fp in reflections_dir.glob("*.json"):
            date_str = fp.stem.strip()
            if not self._date_in_window(date_str, window_days):
                continue
            rows = self._load_reflection_day_rows(date_str)
            meta = self._extract_reflection_day_meta(date_str, rows)
            if starred_only and not meta.get("starred", False):
                continue
            preview = rows[-1].get("content", "") if rows else ""
            items.append(
                {
                    "date": date_str,
                    "count": len(rows),
                    "preview": self._build_preview(preview, limit=90),
                    "first_time": rows[0].get("time", "") if rows else "",
                    "last_time": rows[-1].get("time", "") if rows else "",
                    "starred": bool(meta.get("starred", False)),
                    "note": str(meta.get("note") or ""),
                }
            )

        items.sort(key=lambda x: x["date"], reverse=True)
        return items

    async def set_diary_starred(self, date_str: str, starred: bool) -> dict[str, Any] | None:
        item = self.get_diary_item(date_str)
        if not item:
            return None
        meta = self._load_diary_meta(date_str)
        meta["starred"] = bool(starred)
        self._save_diary_meta_sync(date_str, meta)
        return self.get_diary_item(date_str)

    async def set_diary_note(self, date_str: str, note: str) -> dict[str, Any] | None:
        item = self.get_diary_item(date_str)
        if not item:
            return None
        meta = self._load_diary_meta(date_str)
        meta["note"] = str(note or "")
        self._save_diary_meta_sync(date_str, meta)
        return self.get_diary_item(date_str)

    async def set_reflection_day_starred(self, date_str: str, starred: bool) -> dict[str, Any] | None:
        item = self.get_reflection_day_item(date_str)
        if not item:
            return None
        rows = self._load_reflection_day_rows(date_str)
        meta = self._extract_reflection_day_meta(date_str, rows)
        meta["starred"] = bool(starred)
        self._save_reflection_day_rows_with_meta(date_str, rows, meta)
        return self.get_reflection_day_item(date_str)

    async def set_reflection_day_note(self, date_str: str, note: str) -> dict[str, Any] | None:
        item = self.get_reflection_day_item(date_str)
        if not item:
            return None
        rows = self._load_reflection_day_rows(date_str)
        meta = self._extract_reflection_day_meta(date_str, rows)
        meta["note"] = str(note or "")
        self._save_reflection_day_rows_with_meta(date_str, rows, meta)
        return self.get_reflection_day_item(date_str)

    def _safe_window_days(self, value, default: int) -> int:
        try:
            parsed = int(value)
            if parsed == -1:
                return -1
            return max(parsed, 1)
        except Exception:
            return default

    def _date_in_window(self, date_str: str, days: int) -> bool:
        if days == -1:
            return True
        try:
            d = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
            delta = (datetime.date.today() - d).days
            return 0 <= delta < days
        except Exception:
            return False

    def _extract_title(self, content: str, fallback: str) -> str:
        if not content:
            return fallback
        first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
        return first_line or fallback

    def _build_preview(self, content: str, limit: int = 120) -> str:
        compact = " ".join(line.strip() for line in str(content).splitlines() if line.strip())
        if len(compact) <= limit:
            return compact
        return compact[:limit].rstrip() + "……"

    async def _run_scheduler(self):
        interval_minutes = self.config.get("thinking_interval_minutes", 30)
        diary_time_str = self.config.get("diary_time", "23:58")

        try:
            diary_hour, diary_minute = map(int, diary_time_str.split(":"))
        except (ValueError, AttributeError):
            diary_hour, diary_minute = 23, 58
            logger.warning("[Scheduler] 日记时间格式错误，使用默认值 23:58")

        self.last_diary_date = datetime.datetime.now().strftime("%Y-%m-%d")

        logger.info(f"[Scheduler] 日记生成时间设置为：{diary_hour:02d}:{diary_minute:02d}")

        while self.is_running:
            try:
                now = datetime.datetime.now()
                today_str = now.strftime("%Y-%m-%d")

                if today_str != self.last_diary_date:
                    logger.info(f"[Scheduler] 新的一天开始：{today_str}")
                    self._run_today_reset(today_str)

                auto_diary_enabled = self.config.get("enable_auto_diary", True)
                current_total_minutes = now.hour * 60 + now.minute
                if auto_diary_enabled and now.hour == diary_hour and now.minute == diary_minute:
                    if self.last_diary_check_minute != current_total_minutes:
                        self.last_diary_check_minute = current_total_minutes
                        if self.diary_generated_today and not self.config.get("allow_overwrite_today_diary", False):
                            logger.info(f"[Scheduler] 跳过日记生成：{today_str} 今日日记已生成")
                        else:
                            logger.info(f"[Scheduler] 到达日记生成时间：{diary_time_str}")
                            await self._generate_and_push_diary(today_str)

                auto_reflection_enabled = self.config.get("enable_auto_reflection", True)
                if auto_reflection_enabled:
                    if self.silent_hours.is_silent():
                        logger.debug("[Scheduler] 当前处于静默时段，跳过思考")
                    else:
                        await self._do_reflection()
                else:
                    logger.debug("[Scheduler] 自动思考已关闭，跳过本轮思考")

                await asyncio.sleep(interval_minutes * 60)

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

        stop_tokens = {
            "现在", "此刻", "这会", "这会儿", "感觉", "有点", "一些", "正在", "就是", "还是", "似乎",
            "自己", "今天", "刚刚", "目前", "然后", "因为", "所以", "已经", "有些", "一种",
        }

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
        mode = self.config.get("reflection_dedupe_mode", "普通")
        if mode == "严格":
            return 0.62
        if mode == "普通":
            return 0.72
        if mode == "无限制":
            return None
        return 0.72

    def _mark_dedupe(self, hit: bool, mode: str = "none", source: Optional[str] = None):
        self.last_dedupe_hit = hit
        self.last_dedupe_mode = mode
        self.last_dedupe_source = source

    def _record_reflection_error(self, code: str, message: str):
        self.last_reflection_error_code = code
        self.last_reflection_error_message = message
        self.last_reflection_error_time = datetime.datetime.now().strftime("%H:%M:%S")

    def _clear_reflection_error(self):
        self.last_reflection_error_code = None
        self.last_reflection_error_message = None
        self.last_reflection_error_time = None

    def _record_diary_error(self, code: str, message: str):
        self.last_diary_error_code = code
        self.last_diary_error_message = message
        self.last_diary_error_time = datetime.datetime.now().strftime("%H:%M:%S")

    def _clear_diary_error(self):
        self.last_diary_error_code = None
        self.last_diary_error_message = None
        self.last_diary_error_time = None

    async def select_reflection_session(self) -> str | None:
        recent_session_id = await self.message_cache.get_most_recent_session_id()
        if recent_session_id:
            self.last_selected_session_id = recent_session_id
            self.last_selected_session_source = "recent_message"
            return recent_session_id

        targets = self.config.get("diary_push_targets", []) or []
        if targets:
            self.last_selected_session_id = str(targets[0]).strip()
            self.last_selected_session_source = "push_target"
            return self.last_selected_session_id

        session_ids = await self.message_cache.get_all_session_ids()
        if session_ids:
            self.last_selected_session_id = session_ids[0]
            self.last_selected_session_source = "message_cache"
            return session_ids[0]

        self.last_selected_session_id = None
        self.last_selected_session_source = "none"
        return None

    def _is_duplicate_reflection(self, new_text: str) -> bool:
        if not new_text:
            self._mark_dedupe(False)
            return False

        normalized_new = self._normalize_text_for_dedupe(new_text)
        threshold = self._get_similarity_threshold()
        exact_prefix_guard = self.config.get("reflection_exact_prefix_guard", True)

        if self.current_awareness_text:
            current_normalized = self._normalize_text_for_dedupe(self.current_awareness_text)
            if current_normalized == normalized_new:
                self._mark_dedupe(True, "exact", "current_awareness")
                return True

            if threshold is not None:
                similarity = self._calc_similarity(self.current_awareness_text, new_text)
                if similarity >= threshold:
                    self._mark_dedupe(True, "similar", "current_awareness")
                    return True

            if exact_prefix_guard:
                current_tail = current_normalized[:24]
                new_tail = normalized_new[:24]
                if current_tail and new_tail and current_tail == new_tail:
                    self._mark_dedupe(True, "prefix", "current_awareness")
                    return True

        if self.today_reflections:
            reference_count = self._safe_non_negative_int(self.config.get("reflection_reference_count", 2), default=2)
            tail_size = max(reference_count, 2)
            recent_tail = self.today_reflections[-tail_size:]
            for index, old in enumerate(recent_tail, start=1):
                old_normalized = self._normalize_text_for_dedupe(old)
                if old_normalized == normalized_new:
                    self._mark_dedupe(True, "exact", f"recent_{index}")
                    return True

                if threshold is not None:
                    similarity = self._calc_similarity(old, new_text)
                    if similarity >= threshold:
                        self._mark_dedupe(True, "similar", f"recent_{index}")
                        return True

                if exact_prefix_guard:
                    old_tail = old_normalized[:24]
                    new_tail = normalized_new[:24]
                    if old_tail and new_tail and old_tail == new_tail:
                        self._mark_dedupe(True, "prefix", f"recent_{index}")
                        return True

        self._mark_dedupe(False)
        return False

    def _build_recent_reflections_text(self) -> str:
        reference_count = self._safe_non_negative_int(self.config.get("reflection_reference_count", 2), default=2)
        if reference_count <= 0:
            return "（不参考最近思考）"
        recent = self.today_reflections[-reference_count:]
        if not recent:
            return "（暂无最近思考）"
        return "\n".join([f"- {x}" for x in recent])

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

    async def _do_reflection(self):
        try:
            now = datetime.datetime.now()
            current_time_str = now.strftime("%H:%M")

            logger.debug(f"[Scheduler] 开始思考... 时间：{current_time_str}")

            session_id = await self.select_reflection_session()
            persona_ctx = await self.dependency_manager.resolve_persona_context(session_id) if session_id else {}
            persona_name = persona_ctx.get("persona_name") if persona_ctx else None
            persona_desc = persona_ctx.get("persona_desc") if persona_ctx else None
            if session_id and persona_name:
                self.session_persona_map[session_id] = persona_name

            result = await self.reflection_generator.generate(
                current_time_str,
                session_id,
                self._build_recent_reflections_text(),
                persona_name,
                persona_desc,
            )

            if result:
                if self._is_duplicate_reflection(result):
                    logger.info(
                        f"[Scheduler] 思考结果命中去重，跳过更新: mode={self.last_dedupe_mode}, source={self.last_dedupe_source}"
                    )
                    self.consecutive_failures = 0
                    self._clear_reflection_error()
                    return

                self.current_awareness_text = result
                self.today_reflections.append(result)
                self.last_reflection_time = now
                self.consecutive_failures = 0
                self._clear_reflection_error()
                await self._append_reflection_history(now.strftime("%Y-%m-%d"), result)
                await self._apply_reflection_retention()

                logger.info(f"[Scheduler] 思考完成: {result}")
            else:
                self.consecutive_failures += 1
                self._record_reflection_error("reflection_empty", "思考结果为空，请检查模型提供商配置")
                logger.warning(f"[Scheduler] 思考失败（连续失败：{self.consecutive_failures}次）")

                if self.consecutive_failures >= self.max_consecutive_failures:
                    logger.warning(f"[Scheduler] 连续{self.consecutive_failures}次思考失败，请检查模型提供商配置！")

        except Exception as e:
            logger.error(f"[Scheduler] 思考过程出错: {e}", exc_info=True)
            self.consecutive_failures += 1
            self._record_reflection_error("reflection_exception", str(e))

    async def _generate_and_push_diary(self, date_str: str):
        memory_status = "skipped"
        try:
            if self.diary_generated_today and not self.config.get("allow_overwrite_today_diary", False):
                logger.info(f"[Scheduler] 跳过日记生成：{date_str} 今日日记已生成")
                return

            primary_target = self._get_primary_memory_target()
            persona_ctx = await self.dependency_manager.resolve_persona_context(primary_target) if primary_target else {}
            primary_persona = persona_ctx.get("persona_name") if persona_ctx else None
            primary_persona_desc = persona_ctx.get("persona_desc") if persona_ctx else None
            if primary_target and primary_persona:
                self.session_persona_map[primary_target] = primary_persona

            diary_content = await self.diary_generator.generate(
                date_str,
                self.today_reflections,
                primary_target,
                primary_persona,
                primary_persona_desc,
            )

            if not diary_content:
                self._record_diary_error("diary_empty", "日记生成结果为空，请检查模型提供商配置")
                logger.warning("[Scheduler] 日记生成失败")
                await self._save_diary_meta(date_str, memory_status="failed")
                return

            overwrite = bool(self.config.get("allow_overwrite_today_diary", False))
            regeneration_info = {"matched": 0, "updated": 0, "ids": []}
            if overwrite and self.config.get("store_diary_to_memory", True) and self.dependency_manager.has_livingmemory and primary_target:
                regeneration_info = await self.dependency_manager.mark_daymind_diary_memories_deleted(
                    date_str=date_str,
                    session_id=primary_target,
                )

            await self._save_diary_local(date_str, diary_content)

            if self.config.get("store_diary_to_memory", True) and self.dependency_manager.has_livingmemory:
                memory_metadata = self._build_diary_memory_metadata(date_str)
                memory_metadata["replaces_memory_ids"] = regeneration_info.get("ids", [])
                stored = await self.dependency_manager.store_to_memory(
                    date_str=date_str,
                    content=diary_content,
                    session_id=primary_target,
                    persona_id=primary_persona,
                    metadata=memory_metadata,
                )
                if not stored:
                    memory_status = "failed"
                    self._record_diary_error("memory_store_failed", "日记写入记忆系统失败")
                    logger.warning("[Scheduler] 日记存入记忆系统失败")
                    await self._save_diary_meta(date_str, memory_status=memory_status)
                    return
                memory_status = "stored"
            else:
                memory_status = "skipped"

            await self._save_diary_meta(date_str, memory_status=memory_status)
            await self._apply_diary_retention()
            await self._push_diary_to_targets(diary_content)

            self.diary_generated_today = True
            self.today_reflections = []
            self.current_awareness_text = ""
            self._clear_diary_error()

        except Exception as e:
            logger.error(f"[Scheduler] 日记生成流程出错: {e}", exc_info=True)
            self._record_diary_error("diary_exception", str(e))
            await self._save_diary_meta(date_str, memory_status="failed")

    def _build_diary_memory_metadata(self, date_str: str) -> dict:
        overwrite = self.config.get("allow_overwrite_today_diary", False)
        version = self.diary_memory_version_counter.get(date_str, 0) + 1
        self.diary_memory_version_counter[date_str] = version
        return {
            "type": "diary",
            "source": "daymind",
            "date": date_str,
            "version": version,
            "is_regenerated": overwrite and version > 1,
            "overwrite_of_date": date_str if overwrite and version > 1 else "",
            "status": "active",
        }

    def _get_primary_memory_target(self) -> str | None:
        targets = self.config.get("diary_push_targets", []) or []
        if targets:
            return str(targets[0]).strip()
        return self.last_selected_session_id

    def _get_primary_persona_id(self, target: str | None) -> str | None:
        if not target:
            return None
        return self.session_persona_map.get(target)

    async def _save_diary_local(self, date_str: str, content: str):
        try:
            diary_file = self._diary_text_path(date_str)
            diary_file.parent.mkdir(parents=True, exist_ok=True)

            with open(diary_file, 'w', encoding='utf-8') as f:
                f.write(content)

            if not self._diary_meta_path(date_str).exists():
                self._save_diary_meta_sync(date_str, self._build_default_diary_meta(date_str))

            logger.info(f"[Scheduler] 日记已保存到本地: {diary_file}")

        except Exception as e:
            logger.error(f"[Scheduler] 保存日记到本地失败: {e}")
            self._record_diary_error("local_save_failed", str(e))

    async def _save_diary_meta(self, date_str: str, memory_status: str = "unknown"):
        try:
            current = self._load_diary_meta(date_str)
            current["memory_status"] = memory_status
            self._save_diary_meta_sync(date_str, current)
        except Exception as e:
            logger.debug(f"[Scheduler] 保存日记元信息失败: {e}")

    async def _append_reflection_history(self, date_str: str, content: str):
        try:
            history_dir = self._reflections_dir()
            history_dir.mkdir(parents=True, exist_ok=True)
            history_file = self._reflection_day_path(date_str)
            items = self._load_reflection_day_rows(date_str)
            day_meta = self._extract_reflection_day_meta(date_str, items)
            items.append(
                {
                    "time": datetime.datetime.now().strftime("%H:%M:%S"),
                    "content": content,
                    "created_at": datetime.datetime.now().isoformat(),
                }
            )
            self._save_reflection_day_rows_with_meta(date_str, items, day_meta)
        except Exception as e:
            logger.debug(f"[Scheduler] 保存思考流失败: {e}")

    async def _apply_reflection_retention(self):
        try:
            keep_days = self._safe_retention_days(self._config_get("reflection_retention_days", 3), default=3)
            if keep_days == -1:
                return
            history_dir = self._reflections_dir()
            if not history_dir.exists():
                return
            cutoff = datetime.date.today() - datetime.timedelta(days=keep_days - 1) if keep_days > 0 else datetime.date.today() + datetime.timedelta(days=1)
            for fp in history_dir.glob("*.json"):
                try:
                    file_date = datetime.datetime.strptime(fp.stem, "%Y-%m-%d").date()
                except Exception:
                    continue
                if file_date >= cutoff:
                    continue
                meta = self._extract_reflection_day_meta(fp.stem, self._load_reflection_day_rows(fp.stem))
                if bool(meta.get("starred", False)):
                    continue
                fp.unlink(missing_ok=True)
        except Exception as e:
            logger.debug(f"[Scheduler] 应用思考流轮换失败: {e}")

    async def _apply_diary_retention(self):
        try:
            keep_days = self._safe_retention_days(self._config_get("diary_retention_days", -1), default=-1)
            if keep_days == -1:
                return
            diaries_dir = self._diaries_dir()
            if not diaries_dir.exists():
                return
            cutoff = datetime.date.today() - datetime.timedelta(days=keep_days - 1) if keep_days > 0 else datetime.date.today() + datetime.timedelta(days=1)
            for txt_fp in diaries_dir.glob("*.txt"):
                date_str = txt_fp.stem
                try:
                    file_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
                except Exception:
                    continue
                if file_date >= cutoff:
                    continue
                meta = self._load_diary_meta(date_str)
                if bool(meta.get("starred", False)):
                    continue
                txt_fp.unlink(missing_ok=True)
                self._diary_meta_path(date_str).unlink(missing_ok=True)
        except Exception as e:
            logger.debug(f"[Scheduler] 应用日记轮换失败: {e}")

    async def _push_diary_to_targets(self, content: str):
        targets = self.config.get("diary_push_targets", [])

        if not targets:
            logger.debug("[Scheduler] 未配置推送目标")
            return

        for target in targets:
            max_retries = int(self.config.get("push_retry_times", 3))
            retry_delay = float(self.config.get("push_retry_delay_seconds", 2))

            success = False
            last_error = None
            for attempt in range(1, max_retries + 1):
                try:
                    await self._send_message_to_target(target, content)
                    success = True
                    break
                except Exception as e:
                    last_error = e
                    logger.warning(f"[Scheduler] 推送失败，第{attempt}/{max_retries}次: target={target}, error={e}")
                    if attempt < max_retries:
                        await asyncio.sleep(retry_delay)

            if not success:
                self._record_diary_error("push_failed", f"target={target}, error={last_error}")
                logger.error(f"[Scheduler] 推送日记到 {target} 最终失败: {last_error}", exc_info=True)

    async def _send_message_to_target(self, target: str, content: str):
        parts = target.split(":")
        if len(parts) != 3:
            logger.warning(f"[Scheduler] 无效的推送目标格式: {target}")
            return

        message_chain = MessageChain().message(content)
        await self.context.send_message(target, message_chain)
        logger.info(f"[Scheduler] 日记已推送到: {target}")

    def get_status(self) -> dict:
        silent_status = self.silent_hours.get_status()
        reference_count = self._safe_non_negative_int(self.config.get("reflection_reference_count", 2), default=2)
        runtime_config = self.get_runtime_config()

        return {
            "is_running": self.is_running,
            "enable_auto_reflection": self.config.get("enable_auto_reflection", True),
            "enable_auto_diary": self.config.get("enable_auto_diary", True),
            "reflection_reference_count": reference_count,
            "current_awareness_text": self.current_awareness_text,
            "today_reflections_count": len(self.today_reflections),
            "last_reflection_time": self.last_reflection_time.strftime("%H:%M") if self.last_reflection_time else None,
            "consecutive_failures": self.consecutive_failures,
            "silent_hours": silent_status,
            "diary_generated_today": self.diary_generated_today,
            "last_diary_date": self.last_diary_date,
            "primary_memory_target": self._get_primary_memory_target(),
            "primary_persona_id": self._get_primary_persona_id(self._get_primary_memory_target()),
            "allow_overwrite_today_diary": self.config.get("allow_overwrite_today_diary", False),
            "recent_reflections_preview": self.today_reflections[-max(reference_count, 2):],
            "next_reflection_in_minutes": self.config.get("thinking_interval_minutes", 30),
            "reflection_dedupe_mode": self.config.get("reflection_dedupe_mode", "普通") or "普通",
            "reflection_dedupe_similarity_threshold": self._get_similarity_threshold(),
            "last_reflection_error_code": self.last_reflection_error_code,
            "last_reflection_error_message": self.last_reflection_error_message,
            "last_reflection_error_time": self.last_reflection_error_time,
            "last_diary_error_code": self.last_diary_error_code,
            "last_diary_error_message": self.last_diary_error_message,
            "last_diary_error_time": self.last_diary_error_time,
            "last_dedupe_hit": self.last_dedupe_hit,
            "last_dedupe_mode": self.last_dedupe_mode,
            "last_dedupe_source": self.last_dedupe_source,
            "last_selected_session_id": self.last_selected_session_id,
            "last_selected_session_source": self.last_selected_session_source,
            "diary_memory_version": self.diary_memory_version_counter.get(self.last_diary_date, 0),
            "reflection_retention_days": runtime_config["reflection_retention_days"],
            "diary_retention_days": runtime_config["diary_retention_days"],
            "webui_default_window_days": runtime_config["webui_default_window_days"],
            "webui_default_theme": runtime_config["webui_default_theme"],
            "webui_default_mode": runtime_config["webui_default_mode"],
        }
