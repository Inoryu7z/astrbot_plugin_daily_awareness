import asyncio
import datetime
import random
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger


class DreamOperations:
    async def _on_enter_sleep(self, persona_name: str, now: datetime.datetime):
        persona_name = self._canonical_persona_name(persona_name) or persona_name
        state = self._ensure_persona_state(persona_name)
        dream_state = state.setdefault("dream_state", {})

        count_range = str(self._persona_value(persona_name, "dream_count_range", "1-2") or "1-2").strip()
        try:
            parts = count_range.split("-")
            min_count = max(int(parts[0].strip()), 1)
            max_count = max(int(parts[-1].strip()), min_count)
            max_dreams = random.randint(min_count, max_count)
        except Exception:
            max_dreams = random.randint(1, 2)

        dream_state["tonight_dreams"] = []
        dream_state["dream_count"] = 0
        dream_state["max_dreams_tonight"] = max_dreams
        dream_state["sleep_start_time"] = now.isoformat()
        dream_state["last_dream_time"] = None
        dream_state["dream_memory"] = None
        dream_state["dream_shared"] = False
        dream_state["dream_aftereffect"] = None
        dream_state["was_silent_last_cycle"] = True

        logger.info(f"[Scheduler] 进入睡眠: persona={persona_name}, 今晚最多{max_dreams}个梦")
        self._persist_state()

    def _should_dream(self, persona_name: str, now: datetime.datetime) -> bool:
        persona_name = self._canonical_persona_name(persona_name) or persona_name
        state = self._ensure_persona_state(persona_name)
        dream_state = state.get("dream_state", {})

        if dream_state.get("dream_count", 0) >= dream_state.get("max_dreams_tonight", 1):
            return False

        sleep_start_str = dream_state.get("sleep_start_time")
        if not sleep_start_str:
            return False
        try:
            sleep_start = datetime.datetime.fromisoformat(sleep_start_str)
        except Exception:
            return False

        elapsed_since_sleep = (now - sleep_start).total_seconds()
        if elapsed_since_sleep < 900:
            return False

        last_dream_time_str = dream_state.get("last_dream_time")
        if last_dream_time_str:
            try:
                last_dream_time = datetime.datetime.fromisoformat(last_dream_time_str)
                elapsed_since_dream = (now - last_dream_time).total_seconds()
                if elapsed_since_dream < 1800:
                    return False
            except Exception:
                pass

        return True

    async def _do_dream(self, persona_name: str) -> dict[str, Any]:
        persona_name = self._canonical_persona_name(persona_name) or persona_name
        state = self._ensure_persona_state(persona_name)
        dream_state = state.setdefault("dream_state", {})
        try:
            if not self.dream_generator:
                return {"status": "skipped", "message": "梦境生成器未初始化"}

            now = datetime.datetime.now()
            current_time_str = now.strftime("%H:%M")

            selected_session_id = await self.select_reflection_session(persona_name)

            resolved_desc = None
            if selected_session_id:
                persona_ctx = await self.dependency_manager.resolve_persona_context(selected_session_id)
                resolved_desc = persona_ctx.get("persona_desc") if persona_ctx else None
                self._touch_session_persona(selected_session_id, persona_name)

            current_mood = self._simplify_mood(state.get("current_mood"))
            last_awareness = str(state.get("current_awareness_text") or "").strip() or None

            tonight_dreams = dream_state.get("tonight_dreams", [])
            previous_dream = tonight_dreams[-1] if tonight_dreams else None

            result = await self.dream_generator.generate(
                current_time=current_time_str,
                session_id=selected_session_id,
                persona_name=persona_name,
                persona_desc=resolved_desc,
                current_mood=current_mood,
                last_awareness_text=last_awareness,
                previous_dream=previous_dream,
            )

            if result:
                dream_state.setdefault("tonight_dreams", []).append(result)
                dream_state["dream_count"] = dream_state.get("dream_count", 0) + 1
                dream_state["last_dream_time"] = now.isoformat()

                await self._append_dream_history(now.strftime("%Y-%m-%d"), persona_name, result)

                self._persist_state()
                logger.info(f"[Scheduler] 梦境完成: persona={persona_name}, dream_count={dream_state['dream_count']}, result={result[:80]}...")
                return {"status": "success", "text": result}

            logger.warning(f"[Scheduler] 梦境生成失败: persona={persona_name}")
            return {"status": "failed", "message": "梦境生成结果为空"}
        except Exception as e:
            logger.error(f"[Scheduler] 梦境过程出错: {e}", exc_info=True)
            return {"status": "failed", "message": str(e)}

    async def _on_wake_up(self, persona_name: str):
        persona_name = self._canonical_persona_name(persona_name) or persona_name
        state = self._ensure_persona_state(persona_name)
        dream_state = state.get("dream_state", {})
        tonight_dreams = dream_state.get("tonight_dreams", [])

        if not tonight_dreams:
            logger.info(f"[Scheduler] 醒来: persona={persona_name}, 今晚无梦境")
            dream_state["was_silent_last_cycle"] = False
            return

        if self.dream_generator and self.mood_manager and self.mood_manager.is_mood_enabled(persona_name):
            dream_mood = self.dream_generator.generate_dream_mood(tonight_dreams, persona_name)
            dream_state["dream_aftereffect"] = dream_mood

            current_mood = state.get("current_mood")
            if not current_mood or not isinstance(current_mood, dict):
                state["previous_mood"] = None
                state["current_mood"] = dream_mood
            else:
                dream_label = dream_mood.get("label", "平静")
                current_label = current_mood.get("label", "平静")
                if dream_label != current_label:
                    state["previous_mood"] = self._simplify_mood(current_mood)
                    state["current_mood"] = dream_mood

            logger.info(f"[Scheduler] 梦境余韵: persona={persona_name}, mood={dream_mood.get('label')}")

        dream_state["dream_memory"] = tonight_dreams[-1] if tonight_dreams else None
        dream_state["dream_shared"] = False
        dream_state["was_silent_last_cycle"] = False

        logger.info(f"[Scheduler] 醒来: persona={persona_name}, 今晚{len(tonight_dreams)}个梦")
        self._persist_state()

    def get_dream_memory_for_session(self, session_id: str | None, mark_shared: bool = False) -> str | None:
        if not session_id:
            return None
        self._touch_session_persona(session_id)
        persona_name = self._canonical_persona_name(self.session_persona_map.get(session_id))
        if not persona_name:
            return None
        return self.get_dream_memory_for_persona(persona_name, mark_shared=mark_shared)

    def get_dream_memory_for_persona(self, persona_name: str | None, mark_shared: bool = False) -> str | None:
        normalized = self._canonical_persona_name(persona_name)
        if not normalized:
            return None
        state = self.persona_states.get(normalized)
        if not state:
            return None
        dream_state = state.get("dream_state", {})
        if dream_state.get("dream_shared"):
            return None
        memory = dream_state.get("dream_memory")
        if memory and mark_shared:
            dream_state["dream_shared"] = True
        return memory

    def mark_dream_shared(self, persona_name: str | None):
        normalized = self._canonical_persona_name(persona_name)
        if not normalized:
            return
        state = self.persona_states.get(normalized)
        if not state:
            return
        dream_state = state.get("dream_state", {})
        dream_state["dream_shared"] = True

    def get_dream_aftereffect_for_session(self, session_id: str | None) -> dict | None:
        if not session_id:
            return None
        self._touch_session_persona(session_id)
        persona_name = self._canonical_persona_name(self.session_persona_map.get(session_id))
        if not persona_name:
            return None
        normalized = self._canonical_persona_name(persona_name)
        if not normalized:
            return None
        state = self.persona_states.get(normalized)
        if not state:
            return None
        dream_state = state.get("dream_state", {})
        return dream_state.get("dream_aftereffect")

    def get_dream_aftereffect_for_persona(self, persona_name: str | None) -> dict | None:
        normalized = self._canonical_persona_name(persona_name)
        if not normalized:
            return None
        state = self.persona_states.get(normalized)
        if not state:
            return None
        dream_state = state.get("dream_state", {})
        return dream_state.get("dream_aftereffect")

    def get_dream_history(self, persona_name: str | None, date: str | None = None) -> list[dict]:
        canonical_persona = self._canonical_persona_name(persona_name)
        if not canonical_persona:
            return []
        if not date:
            date = datetime.date.today().isoformat()
        try:
            dream_dir = Path(self.data_dir) / "dreams" / self._sanitize_persona_path(canonical_persona)
            dream_file = dream_dir / f"{date}.json"
            items = self._load_json_file(dream_file, [])
            if isinstance(items, list):
                return items
            return []
        except Exception as e:
            logger.debug(f"[Scheduler] 读取梦境历史失败: {e}")
            return []

    async def _append_dream_history(self, date_str: str, persona_name: str, content: str):
        try:
            canonical_persona = self._canonical_persona_name(persona_name) or persona_name
            dream_dir = Path(self.data_dir) / "dreams" / self._sanitize_persona_path(canonical_persona)
            dream_dir.mkdir(parents=True, exist_ok=True)
            dream_file = dream_dir / f"{date_str}.json"
            items = self._load_json_file(dream_file, [])
            if not isinstance(items, list):
                items = []
            items.append({
                "time": datetime.datetime.now().strftime("%H:%M:%S"),
                "content": content,
                "created_at": datetime.datetime.now().isoformat(),
                "persona_name": canonical_persona,
            })
            self._write_json_file(dream_file, items)
        except Exception as e:
            logger.debug(f"[Scheduler] 保存梦境历史失败: {e}")
