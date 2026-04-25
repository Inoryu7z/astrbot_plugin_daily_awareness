import asyncio
import base64
import datetime
import re
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger
from astrbot.api.message_components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.message.components import Image as CompImage

from .mood import extract_mood_baseline_from_diary_text


class DiaryOperations:
    def _diaries_dir(self, persona_name: str | None = None) -> Path:
        base = Path(self.data_dir) / "diaries"
        if not persona_name:
            return base
        return base / self._sanitize_persona_path(persona_name)

    def _diary_text_path(self, date_str: str, persona_name: str) -> Path:
        return self._diaries_dir(persona_name) / f"{date_str}.txt"

    def _diary_meta_path(self, date_str: str, persona_name: str) -> Path:
        return self._diaries_dir(persona_name) / f"{date_str}.json"

    def _build_default_diary_meta(self, date_str: str, persona_name: str) -> dict[str, Any]:
        return {"date": date_str, "persona_name": persona_name, "memory_status": "unknown", "starred": False, "note": "", "updated_at": datetime.datetime.now().isoformat()}

    def _load_diary_meta(self, date_str: str, persona_name: str) -> dict[str, Any]:
        data = self._load_json_file(self._diary_meta_path(date_str, persona_name), {})
        if not isinstance(data, dict):
            data = {}
        base = self._build_default_diary_meta(date_str, persona_name)
        base.update(data)
        base["starred"] = bool(base.get("starred", False))
        base["note"] = str(base.get("note") or "")
        return base

    def _save_diary_meta_sync(self, date_str: str, persona_name: str, payload: dict[str, Any]):
        final_payload = self._build_default_diary_meta(date_str, persona_name)
        final_payload.update(payload or {})
        final_payload["updated_at"] = datetime.datetime.now().isoformat()
        self._write_json_file(self._diary_meta_path(date_str, persona_name), final_payload)

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

    def _scan_persona_dirs(self, root: Path) -> list[tuple[str, Path]]:
        if not root.exists():
            return []
        pairs: list[tuple[str, Path]] = []
        for item in root.iterdir():
            if item.is_dir():
                pairs.append((item.name, item))
        return pairs

    def get_diary_item(self, date_str: str, persona_name: str | None = None) -> dict[str, Any] | None:
        if persona_name:
            canonical_name = self._canonical_persona_name(persona_name) or persona_name
            txt_file = self._diary_text_path(date_str, canonical_name)
            if not txt_file.exists():
                return None
            content = txt_file.read_text(encoding="utf-8").strip()
            stat = txt_file.stat()
            meta = self._load_diary_meta(date_str, canonical_name)
            return {"date": date_str, "persona_name": canonical_name, "title": self._extract_title(content, date_str), "content": content, "updated_at": int(stat.st_mtime), "memory_status": meta.get("memory_status", "unknown"), "starred": bool(meta.get("starred", False)), "note": str(meta.get("note") or "")}
        for dir_name, _ in self._scan_persona_dirs(self._diaries_dir()):
            item = self.get_diary_item(date_str, dir_name)
            if item:
                return item
        return None

    def list_diaries(self, days: int | None = None, starred_only: bool = False) -> list[dict[str, Any]]:
        root = self._diaries_dir()
        if not root.exists():
            return []
        window_days = self._safe_window_days(days if days is not None else self._config_get("webui_default_window_days", 3), 3)
        items: list[dict[str, Any]] = []
        for persona_name, persona_dir in self._scan_persona_dirs(root):
            for txt_file in persona_dir.glob("*.txt"):
                date_str = txt_file.stem.strip()
                if not self._date_in_window(date_str, window_days):
                    continue
                try:
                    content = txt_file.read_text(encoding="utf-8").strip()
                except Exception:
                    content = ""
                meta = self._load_diary_meta(date_str, persona_name)
                if starred_only and not meta.get("starred", False):
                    continue
                stat = txt_file.stat()
                items.append({"date": date_str, "persona_name": persona_name, "title": self._extract_title(content, date_str), "preview": self._build_preview(content, limit=120), "length": len(content), "updated_at": int(stat.st_mtime), "memory_status": meta.get("memory_status", "unknown"), "starred": bool(meta.get("starred", False)), "note": str(meta.get("note") or "")})
        items.sort(key=lambda x: (x["date"], x.get("persona_name", "")), reverse=True)
        return items

    async def set_diary_starred(self, date_str: str, starred: bool, persona_name: str | None = None) -> dict[str, Any] | None:
        item = self.get_diary_item(date_str, persona_name)
        if not item:
            return None
        persona_name = item.get("persona_name")
        meta = self._load_diary_meta(date_str, persona_name)
        meta["starred"] = bool(starred)
        self._save_diary_meta_sync(date_str, persona_name, meta)
        return self.get_diary_item(date_str, persona_name)

    async def set_diary_note(self, date_str: str, note: str, persona_name: str | None = None) -> dict[str, Any] | None:
        item = self.get_diary_item(date_str, persona_name)
        if not item:
            return None
        persona_name = item.get("persona_name")
        meta = self._load_diary_meta(date_str, persona_name)
        meta["note"] = str(note or "")
        self._save_diary_meta_sync(date_str, persona_name, meta)
        return self.get_diary_item(date_str, persona_name)

    def _get_persona_diary_time(self, persona_name: str | None) -> tuple[int, int]:
        persona_name = self._canonical_persona_name(persona_name)
        diary_time_str = str(self._persona_value(persona_name, "diary_time", self.config.get("diary_time", "23:58")) or "23:58")
        try:
            hour, minute = map(int, diary_time_str.split(":"))
            return hour, minute
        except (ValueError, AttributeError):
            logger.warning(f"[Scheduler] 日记时间格式错误，persona={persona_name or 'default'}，使用默认值 23:58")
            return 23, 58

    def _get_diary_generation_retry_count(self, persona_name: str | None) -> int:
        return self._safe_non_negative_int(self._persona_value(persona_name, "diary_generation_retry_count", 2), default=2)

    def _get_diary_generation_retry_delay_seconds(self, persona_name: str | None) -> float:
        try:
            return max(float(self._persona_value(persona_name, "diary_generation_retry_delay_seconds", 2)), 0.0)
        except Exception:
            return 2.0

    def _get_diary_failure_cooldown_seconds(self, persona_name: str | None) -> int:
        return self._safe_seconds(self._persona_value(persona_name, "diary_failure_cooldown_seconds", 600), 600)

    async def _run_diary_generation_with_retries(
        self,
        date_str: str,
        persona_name: str,
        reflections: list[str],
        session_id: str | None,
        persona_desc: str | None,
        ensured_schedule: dict[str, Any] | None = None,
    ) -> str | None:
        max_retries = self._get_diary_generation_retry_count(persona_name)
        retry_delay = self._get_diary_generation_retry_delay_seconds(persona_name)
        last_result = None
        for attempt in range(max_retries + 1):
            last_result = await self.diary_generator.generate(
                date_str,
                reflections,
                session_id=session_id,
                persona_name=persona_name,
                persona_desc=persona_desc,
                ensured_schedule=ensured_schedule,
            )
            if last_result:
                if attempt > 0:
                    logger.info(f"[Scheduler] 日记生成重试成功: persona={persona_name}, date={date_str}, attempt={attempt + 1}")
                return last_result
            if attempt < max_retries:
                logger.warning(f"[Scheduler] 日记生成失败，准备重试: persona={persona_name}, date={date_str}, attempt={attempt + 1}/{max_retries + 1}, delay={retry_delay}s")
                if retry_delay > 0:
                    await asyncio.sleep(retry_delay)
        return last_result

    async def run_manual_diary(self, session_id: str, persona_name: str | None, persona_desc: str | None = None) -> dict[str, Any]:
        persona_name = self._canonical_persona_name(persona_name)
        if not persona_name or not self.is_persona_enabled(persona_name):
            return {"status": "skipped", "message": "当前人格未启用 DayMind"}
        self._touch_session_persona(session_id, persona_name)
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        state = self._ensure_persona_state(persona_name)
        schedule_result = await self.dependency_manager.ensure_today_schedule(
            session_id=session_id,
            persona_name=persona_name,
            persona_desc=persona_desc,
            target_date=today_str,
            debug=self._is_debug_mode(),
        )
        if schedule_result.get("status") == "failed":
            return {
                "status": "failed_schedule",
                "message": schedule_result.get("message") or "目标日期日程不可用",
                "schedule_data": schedule_result.get("data") or {},
                "schedule_generated_now": False,
            }
        if state.get("diary_generated_today") and not bool(self._persona_value(persona_name, "allow_overwrite_today_diary", False)):
            return {
                "status": "exists",
                "schedule_data": schedule_result.get("data") or {},
                "schedule_generated_now": bool(schedule_result.get("generated_now")),
            }
        return await self._generate_and_push_diary(today_str, persona_name, primary_target=session_id, persona_desc=persona_desc, manual=True, ensured_schedule=schedule_result)

    async def _generate_and_push_diary(self, date_str: str, persona_name: str, primary_target: str | None = None, persona_desc: str | None = None, manual: bool = False, ensured_schedule: dict[str, Any] | None = None) -> dict[str, Any]:
        persona_name = self._canonical_persona_name(persona_name) or persona_name
        memory_status = "skipped"
        state = self._ensure_persona_state(persona_name)
        try:
            state["state_date"] = date_str
            if state.get("diary_generated_today") and not bool(self._persona_value(persona_name, "allow_overwrite_today_diary", False)):
                logger.info(f"[Scheduler] 跳过日记生成：{date_str} persona={persona_name} 今日日记已生成")
                self._persist_state()
                return {"status": "exists"}
            target = primary_target or await self.select_reflection_session(persona_name)
            primary_persona_desc = persona_desc
            persona_ctx = None
            resolved_persona_id = None
            if target:
                persona_ctx = await self.dependency_manager.resolve_persona_context(target)
                if persona_ctx and not primary_persona_desc:
                    primary_persona_desc = persona_ctx.get("persona_desc")
                resolved_persona_id = persona_ctx.get("persona_id") if persona_ctx else None
                self._touch_session_persona(target, persona_name)
            if not isinstance(ensured_schedule, dict):
                ensured_schedule = await self.dependency_manager.ensure_today_schedule(
                    session_id=target,
                    persona_name=persona_name,
                    persona_desc=primary_persona_desc,
                    target_date=date_str,
                    debug=self._is_debug_mode(),
                )
            diary_content = await self._run_diary_generation_with_retries(
                date_str=date_str,
                persona_name=persona_name,
                reflections=list(state.get("today_reflections", []) or []),
                session_id=target,
                persona_desc=primary_persona_desc,
                ensured_schedule=ensured_schedule,
            )
            if not diary_content:
                self._record_diary_error(persona_name, "diary_empty", "日记生成结果为空，请检查模型提供商配置")
                await self._save_diary_meta(date_str, persona_name, memory_status="failed")
                self._persist_state()
                return {"status": "failed", "message": "日记生成结果为空，请检查模型提供商配置"}
            overwrite = bool(self._persona_value(persona_name, "allow_overwrite_today_diary", False))
            regeneration_info = {"matched": 0, "updated": 0, "ids": []}
            if overwrite and bool(self._persona_value(persona_name, "store_diary_to_memory", True)) and self.dependency_manager.has_livingmemory:
                regeneration_info = await self.dependency_manager.mark_daymind_diary_memories_deleted(
                    date_str=date_str,
                    session_id=target,
                    persona_id=resolved_persona_id,
                    persona_name=persona_name,
                )
            local_saved = await self._save_diary_local(date_str, persona_name, diary_content)
            if not local_saved:
                await self._save_diary_meta(date_str, persona_name, memory_status="failed")
                self._persist_state()
                return {"status": "failed", "message": "日记保存到本地失败"}
            if bool(self._persona_value(persona_name, "store_diary_to_memory", True)) and self.dependency_manager.has_livingmemory:
                memory_metadata = self._build_diary_memory_metadata(date_str, persona_name)
                memory_metadata["replaces_memory_ids"] = regeneration_info.get("ids", [])
                memory_metadata["persona_name"] = persona_name
                memory_metadata["diary_identity"] = f"daymind:{persona_name}:{date_str}"
                stored = await self.dependency_manager.store_to_memory(
                    date_str=date_str,
                    content=diary_content,
                    session_id=target,
                    persona_id=resolved_persona_id,
                    metadata=memory_metadata,
                )
                if not stored:
                    memory_status = "failed"
                    self._record_diary_error(persona_name, "memory_store_failed", "日记写入记忆系统失败")
                    await self._save_diary_meta(date_str, persona_name, memory_status=memory_status)
                    self._persist_state()
                    return {"status": "failed", "message": "日记写入记忆系统失败"}
                memory_status = "stored"
            else:
                memory_status = "skipped"
            await self._save_diary_meta(date_str, persona_name, memory_status=memory_status)
            await self._apply_diary_retention()
            await self._apply_dream_retention()
            if not manual:
                await self._push_diary_to_targets(diary_content, persona_name)
            state["diary_generated_today"] = True
            state["last_diary_date"] = date_str
            state["last_diary_failure_time"] = None
            state["last_diary_cooldown_until"] = None
            state["last_diary_failed_trigger_key"] = ""
            self._clear_diary_error(persona_name)
            self._persist_state()
            return {"status": "success", "content": diary_content, "marked_deleted": int(regeneration_info.get('updated', 0) or 0), "schedule_data": (ensured_schedule or {}).get("data") or {}, "schedule_generated_now": bool((ensured_schedule or {}).get("generated_now"))}
        except Exception as e:
            logger.error(f"[Scheduler] 日记生成流程出错: {e}", exc_info=True)
            self._record_diary_error(persona_name, "diary_exception", str(e))
            await self._save_diary_meta(date_str, persona_name, memory_status="failed")
            self._persist_state()
            return {"status": "failed", "message": str(e)}

    def _build_diary_memory_metadata(self, date_str: str, persona_name: str) -> dict:
        persona_name = self._canonical_persona_name(persona_name) or persona_name
        state = self._ensure_persona_state(persona_name)
        overwrite = bool(self._persona_value(persona_name, "allow_overwrite_today_diary", False))
        counter = self._trim_diary_memory_version_counter(state.get("diary_memory_version_counter"))
        version = int(counter.get(date_str, 0) or 0) + 1
        counter[date_str] = version
        state["diary_memory_version_counter"] = counter
        return {"type": "diary", "source": "daymind", "date": date_str, "persona_name": persona_name, "version": version, "is_regenerated": overwrite and version > 1, "overwrite_of_date": date_str if overwrite and version > 1 else "", "status": "active"}

    def _get_primary_persona_id(self, persona_name: str | None) -> str | None:
        return self._canonical_persona_name(persona_name)

    async def _save_diary_local(self, date_str: str, persona_name: str, content: str) -> bool:
        try:
            canonical_persona = self._canonical_persona_name(persona_name) or persona_name
            diary_file = self._diary_text_path(date_str, canonical_persona)
            diary_file.parent.mkdir(parents=True, exist_ok=True)
            with open(diary_file, 'w', encoding='utf-8') as f:
                f.write(content)
            if not self._diary_meta_path(date_str, canonical_persona).exists():
                self._save_diary_meta_sync(date_str, canonical_persona, self._build_default_diary_meta(date_str, canonical_persona))
            logger.info(f"[Scheduler] 日记已保存到本地: persona={canonical_persona}, path={diary_file}")
            return True
        except Exception as e:
            logger.error(f"[Scheduler] 保存日记到本地失败: {e}")
            self._record_diary_error(persona_name, "local_save_failed", str(e))
            return False

    async def _save_diary_meta(self, date_str: str, persona_name: str, memory_status: str = "unknown"):
        try:
            canonical_persona = self._canonical_persona_name(persona_name) or persona_name
            current = self._load_diary_meta(date_str, canonical_persona)
            current["memory_status"] = memory_status
            self._save_diary_meta_sync(date_str, canonical_persona, current)
        except Exception as e:
            logger.debug(f"[Scheduler] 保存日记元信息失败: {e}")

    def _trim_diary_memory_version_counter(self, counter: dict | None, keep_days: int | None = None) -> dict[str, int]:
        if not isinstance(counter, dict):
            return {}
        if keep_days is None:
            keep_days = self._safe_retention_days(self._config_get("diary_retention_days", -1), default=-1)
        if keep_days == -1:
            keep_days = 7
        cutoff = self._retention_cutoff_date(keep_days)
        trimmed: dict[str, int] = {}
        for date_str, version in counter.items():
            try:
                parsed_date = datetime.datetime.strptime(str(date_str), "%Y-%m-%d").date()
            except Exception:
                continue
            if parsed_date < cutoff:
                continue
            trimmed[str(date_str)] = self._safe_non_negative_int(version, default=0)
        return trimmed

    def _prune_all_diary_memory_version_counters(self):
        for persona_name in list(self.persona_states.keys()):
            state = self._ensure_persona_state(persona_name)
            state["diary_memory_version_counter"] = self._trim_diary_memory_version_counter(state.get("diary_memory_version_counter"))

    async def _apply_diary_retention(self):
        try:
            keep_days = self._safe_retention_days(self._config_get("diary_retention_days", -1), default=-1)
            if keep_days == -1:
                return
            root = self._diaries_dir()
            if not root.exists():
                return
            cutoff = self._retention_cutoff_date(keep_days)
            for persona_name, persona_dir in self._scan_persona_dirs(root):
                for txt_fp in persona_dir.glob("*.txt"):
                    date_str = txt_fp.stem
                    try:
                        file_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
                    except Exception:
                        continue
                    if file_date >= cutoff:
                        continue
                    meta = self._load_diary_meta(date_str, persona_name)
                    if bool(meta.get("starred", False)):
                        continue
                    txt_fp.unlink(missing_ok=True)
                    self._diary_meta_path(date_str, persona_name).unlink(missing_ok=True)
        except Exception as e:
            logger.debug(f"[Scheduler] 应用日记轮换失败: {e}")

    async def _apply_dream_retention(self):
        try:
            keep_days = self._safe_retention_days(self._config_get("diary_retention_days", -1), default=-1)
            if keep_days == -1:
                return
            dream_root = Path(self.data_dir) / "dreams"
            if not dream_root.exists():
                return
            cutoff = self._retention_cutoff_date(keep_days)
            for persona_dir in dream_root.iterdir():
                if not persona_dir.is_dir():
                    continue
                for dream_fp in persona_dir.glob("*.json"):
                    date_str = dream_fp.stem
                    try:
                        file_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
                    except Exception:
                        continue
                    if file_date >= cutoff:
                        continue
                    dream_fp.unlink(missing_ok=True)
        except Exception as e:
            logger.debug(f"[Scheduler] 应用梦境历史轮换失败: {e}")

    async def _push_diary_to_targets(self, content: str, persona_name: str | None = None):
        targets = self._persona_value(persona_name, "diary_push_targets", self.config.get("diary_push_targets", []))
        if not targets:
            logger.debug("[Scheduler] 未配置推送目标")
            return

        enable_image = bool(self._persona_value(persona_name, "enable_diary_image", False))
        image_bytes = None
        if enable_image and self.diary_renderer:
            date_str = datetime.datetime.now().strftime("%Y-%m-%d")
            image_bytes = await asyncio.to_thread(
                self.diary_renderer.render, content, date_str, persona_name or ""
            )

        for target in targets:
            try:
                if image_bytes:
                    await self._send_image_to_target(target, image_bytes)
                else:
                    await self._send_message_to_target(target, content)
            except Exception as e:
                self._record_diary_error(persona_name or "未命名人格", "push_failed", f"target={target}, error={e}")
                self._persist_state()
                logger.error(f"[Scheduler] 推送日记到 {target} 失败: {e}", exc_info=True)

    async def _send_image_to_target(self, target: str, image_bytes: bytes):
        b64_str = base64.b64encode(image_bytes).decode()
        chain = MessageChain(chain=[CompImage.fromBase64(b64_str)])
        await self.context.send_message(target, chain)
        logger.info(f"[Scheduler] 日记图片已推送到: {target}")

    async def _send_message_to_target(self, target: str, content: str):
        chain = MessageChain(chain=[Plain(text=str(content or ""))])
        await self.context.send_message(target, chain)
        logger.info(f"[Scheduler] 日记已推送到: {target}")
