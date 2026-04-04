"""
DayMind WebUI
- 提供 DayMind 自管理所需的后端接口
- 当前前端仍可继续独立开发
"""

import asyncio
from datetime import date, datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from astrbot.api import logger


class ConfigUpdatePayload(BaseModel):
    reflection_retention_days: int | None = None
    diary_retention_days: int | None = None
    webui_default_window_days: int | None = None
    webui_default_theme: str | None = None
    webui_default_mode: str | None = None


class MetaUpdatePayload(BaseModel):
    starred: bool | None = None
    note: str | None = None


class DayMindWebUI:
    def __init__(self, data_dir: str, config: dict[str, Any], scheduler=None, dependency_manager=None, plugin=None):
        self.data_dir = Path(data_dir)
        self.config = config or {}
        self.scheduler = scheduler
        self.dependency_manager = dependency_manager
        self.plugin = plugin

        self.host = str(self.config.get("webui_host", "127.0.0.1"))
        self.port = int(self.config.get("webui_port", 8899))

        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task | None = None
        self._app = FastAPI(title="DayMind WebUI", version="1.1.0")
        self._setup_routes()

    async def start(self):
        if self._server_task and not self._server_task.done():
            logger.warning("[DayMindWebUI] WebUI 已在运行")
            return

        config = uvicorn.Config(
            app=self._app,
            host=self.host,
            port=self.port,
            log_level="warning",
            loop="asyncio",
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(self._server.serve())

        for _ in range(50):
            if getattr(self._server, "started", False):
                logger.info(f"[DayMindWebUI] 已启动: http://{self.host}:{self.port}")
                return
            if self._server_task.done():
                error = self._server_task.exception()
                raise RuntimeError(f"DayMind WebUI 启动失败: {error}") from error
            await asyncio.sleep(0.1)

        logger.warning("[DayMindWebUI] 启动耗时较长，仍在后台继续启动")

    async def stop(self):
        if self._server:
            self._server.should_exit = True
        if self._server_task:
            await self._server_task
        self._server = None
        self._server_task = None
        logger.info("[DayMindWebUI] 已停止")

    def _normalize_persona_name(self, persona_name: str | None) -> str | None:
        if not self.plugin or not hasattr(self.plugin, "_canonical_persona_name"):
            value = str(persona_name or "").strip()
            return value or None
        return self.plugin._canonical_persona_name(persona_name)

    def _resolve_persona_query(self, persona_name: str | None) -> str | None:
        normalized = self._normalize_persona_name(persona_name)
        if normalized:
            return normalized
        if self.scheduler:
            personas = list(self.scheduler.get_status().get("enabled_personas", []) or [])
            if len(personas) == 1:
                return personas[0]
        return None

    def _setup_routes(self):
        @self._app.get("/", response_class=HTMLResponse)
        async def index():
            return HTMLResponse(self._build_index_html())

        @self._app.get("/api/health")
        async def health():
            return {"status": "ok", "plugin": "daymind", "version": "1.1.0"}

        @self._app.get("/api/status")
        async def status(persona_name: str | None = None):
            scheduler_status = self.scheduler.get_status() if self.scheduler else {}
            available_personas = list(scheduler_status.get("enabled_personas", []) or [])
            requested_persona = str(persona_name or "").strip() or None
            effective_persona = self._resolve_persona_query(requested_persona)
            persona_status = self.scheduler.get_status(effective_persona) if self.scheduler and effective_persona else {}
            persona_status_map = {}
            if self.scheduler:
                for name in available_personas:
                    item = self.scheduler.get_status(name)
                    persona_status_map[name] = {
                        "today_reflections_count": item.get("today_reflections_count", 0),
                        "diary_generated_today": item.get("diary_generated_today", False),
                        "last_reflection_time": item.get("last_reflection_time"),
                        "current_mood": item.get("current_mood"),
                        "current_awareness_text": item.get("current_awareness_text", ""),
                    }
            base_status = persona_status or scheduler_status
            return {
                "success": True,
                "data": {
                    "webui_url": f"http://{self.host}:{self.port}",
                    "diaries_dir": str(self.data_dir / "diaries"),
                    "reflections_dir": str(self.data_dir / "reflections"),
                    "enable_auto_reflection": self.config.get("enable_auto_reflection", True),
                    "enable_auto_diary": self.config.get("enable_auto_diary", True),
                    "store_diary_to_memory": self.config.get("store_diary_to_memory", True),
                    "livingmemory_available": bool(getattr(self.dependency_manager, "has_livingmemory", False)),
                    "requested_persona": requested_persona,
                    "effective_persona": effective_persona,
                    "available_personas": available_personas,
                    "persona_status_map": persona_status_map,
                    "today_reflections_count": base_status.get("today_reflections_count", 0),
                    "diary_generated_today": base_status.get("diary_generated_today", False),
                    "last_reflection_time": base_status.get("last_reflection_time"),
                    "reflection_retention_days": scheduler_status.get("reflection_retention_days", 3),
                    "diary_retention_days": scheduler_status.get("diary_retention_days", -1),
                    "webui_default_window_days": scheduler_status.get("webui_default_window_days", 3),
                    "webui_default_theme": scheduler_status.get("webui_default_theme", "galaxy"),
                    "webui_default_mode": scheduler_status.get("webui_default_mode", "overview"),
                    "reflection_reference_count": base_status.get("reflection_reference_count", scheduler_status.get("reflection_reference_count", 2)),
                    "current_mood": base_status.get("current_mood"),
                    "current_awareness_text": base_status.get("current_awareness_text", ""),
                },
            }

        @self._app.get("/api/config")
        async def get_config():
            if not self.scheduler:
                raise HTTPException(status_code=500, detail="scheduler unavailable")
            return {"success": True, "data": self.scheduler.get_runtime_config()}

        @self._app.post("/api/config")
        async def update_config(payload: ConfigUpdatePayload):
            if not self.scheduler:
                raise HTTPException(status_code=500, detail="scheduler unavailable")
            updates = payload.model_dump(exclude_none=True)
            data = await self.scheduler.update_runtime_config(updates)
            if self.plugin and hasattr(self.plugin, "persist_runtime_config"):
                self.plugin.persist_runtime_config(data)
            return {"success": True, "data": data}

        @self._app.post("/api/reflections/today/reset")
        async def reset_today_reflections(persona_name: str | None = Query(default=None)):
            if not self.scheduler:
                raise HTTPException(status_code=500, detail="scheduler unavailable")
            effective_persona = self._resolve_persona_query(persona_name)
            data = await self.scheduler.reset_today_reflections(effective_persona)
            if self.plugin and hasattr(self.plugin, "save_runtime_state"):
                self.plugin.save_runtime_state()
            return {"success": True, "data": data}

        @self._app.get("/api/diaries")
        async def list_diaries(days: int | None = None, starred_only: bool = False):
            if self.scheduler:
                return {"success": True, "data": self.scheduler.list_diaries(days, starred_only=starred_only)}
            return {"success": True, "data": self._list_diaries(days)}

        @self._app.get("/api/diaries/{date_str}")
        async def get_diary(date_str: str, persona_name: str | None = Query(default=None)):
            effective_persona = self._resolve_persona_query(persona_name)
            item = self.scheduler.get_diary_item(date_str, effective_persona) if self.scheduler else self._read_diary(date_str, effective_persona)
            if not item:
                raise HTTPException(status_code=404, detail="日记不存在")
            return {"success": True, "data": item}

        @self._app.patch("/api/diaries/{date_str}")
        async def patch_diary(date_str: str, payload: MetaUpdatePayload, persona_name: str | None = Query(default=None)):
            if not self.scheduler:
                raise HTTPException(status_code=500, detail="scheduler unavailable")
            effective_persona = self._resolve_persona_query(persona_name)
            data = None
            if payload.starred is not None:
                data = await self.scheduler.set_diary_starred(date_str, payload.starred, effective_persona)
            if payload.note is not None:
                data = await self.scheduler.set_diary_note(date_str, payload.note, effective_persona)
            if not data:
                raise HTTPException(status_code=404, detail="日记不存在")
            if self.plugin and hasattr(self.plugin, "save_runtime_state"):
                self.plugin.save_runtime_state()
            return {"success": True, "data": data}

        @self._app.get("/api/reflections")
        async def list_reflections(days: int | None = None, starred_only: bool = False):
            if self.scheduler:
                return {"success": True, "data": self.scheduler.list_reflection_days(days, starred_only=starred_only)}
            return {"success": True, "data": self._list_reflection_days(days)}

        @self._app.get("/api/reflections/{date_str}")
        async def get_reflections(date_str: str, persona_name: str | None = Query(default=None)):
            effective_persona = self._resolve_persona_query(persona_name)
            item = self.scheduler.get_reflection_day_item(date_str, effective_persona) if self.scheduler else self._read_reflection_day(date_str, effective_persona)
            if not item:
                raise HTTPException(status_code=404, detail="思考流不存在")
            return {"success": True, "data": item}

        @self._app.patch("/api/reflections/{date_str}")
        async def patch_reflections(date_str: str, payload: MetaUpdatePayload, persona_name: str | None = Query(default=None)):
            if not self.scheduler:
                raise HTTPException(status_code=500, detail="scheduler unavailable")
            effective_persona = self._resolve_persona_query(persona_name)
            data = None
            if payload.starred is not None:
                data = await self.scheduler.set_reflection_day_starred(date_str, payload.starred, effective_persona)
            if payload.note is not None:
                data = await self.scheduler.set_reflection_day_note(date_str, payload.note, effective_persona)
            if not data:
                raise HTTPException(status_code=404, detail="思考流不存在")
            if self.plugin and hasattr(self.plugin, "save_runtime_state"):
                self.plugin.save_runtime_state()
            return {"success": True, "data": data}

    def _diaries_dir(self) -> Path:
        return self.data_dir / "diaries"

    def _reflections_dir(self) -> Path:
        return self.data_dir / "reflections"

    def _scan_persona_dirs(self, root: Path) -> list[tuple[str, Path]]:
        if not root.exists():
            return []
        pairs: list[tuple[str, Path]] = []
        for item in root.iterdir():
            if item.is_dir():
                pairs.append((item.name, item))
        return pairs

    def _safe_days(self, days: int | None) -> int:
        if days is None:
            try:
                return int(self.config.get("webui_default_window_days", 3) or 3)
            except Exception:
                return 3
        try:
            parsed = int(days)
            return parsed if parsed == -1 else max(parsed, 1)
        except Exception:
            return 3

    def _date_in_window(self, date_str: str, days: int) -> bool:
        if days == -1:
            return True
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
            delta = (date.today() - d).days
            return 0 <= delta < days
        except Exception:
            return False

    def _list_diaries(self, days: int | None = None) -> list[dict[str, Any]]:
        diaries_dir = self._diaries_dir()
        if not diaries_dir.exists():
            return []

        window_days = self._safe_days(days)
        items: list[dict[str, Any]] = []
        for persona_name, persona_dir in self._scan_persona_dirs(diaries_dir):
            for txt_file in persona_dir.glob("*.txt"):
                date_str = txt_file.stem.strip()
                if not self._date_in_window(date_str, window_days):
                    continue
                try:
                    content = txt_file.read_text(encoding="utf-8").strip()
                except Exception:
                    content = ""
                stat = txt_file.stat()
                items.append(
                    {
                        "date": date_str,
                        "persona_name": persona_name,
                        "title": self._extract_title(content, date_str),
                        "preview": self._build_preview(content, limit=120),
                        "length": len(content),
                        "updated_at": int(stat.st_mtime),
                        "memory_status": self._read_memory_status(date_str, persona_name),
                        "starred": False,
                        "note": "",
                    }
                )

        items.sort(key=lambda x: (x["date"], x.get("persona_name", "")), reverse=True)
        return items

    def _read_diary(self, date_str: str, persona_name: str | None = None) -> dict[str, Any] | None:
        normalized_persona = self._normalize_persona_name(persona_name)
        if normalized_persona:
            txt_file = self._diaries_dir() / normalized_persona / f"{date_str}.txt"
            if not txt_file.exists():
                return None
            content = txt_file.read_text(encoding="utf-8").strip()
            stat = txt_file.stat()
            return {
                "date": date_str,
                "persona_name": normalized_persona,
                "title": self._extract_title(content, date_str),
                "content": content,
                "updated_at": int(stat.st_mtime),
                "memory_status": self._read_memory_status(date_str, normalized_persona),
                "starred": False,
                "note": "",
            }
        for current_persona, persona_dir in self._scan_persona_dirs(self._diaries_dir()):
            txt_file = persona_dir / f"{date_str}.txt"
            if not txt_file.exists():
                continue
            content = txt_file.read_text(encoding="utf-8").strip()
            stat = txt_file.stat()
            return {
                "date": date_str,
                "persona_name": current_persona,
                "title": self._extract_title(content, date_str),
                "content": content,
                "updated_at": int(stat.st_mtime),
                "memory_status": self._read_memory_status(date_str, current_persona),
                "starred": False,
                "note": "",
            }
        return None

    def _list_reflection_days(self, days: int | None = None) -> list[dict[str, Any]]:
        reflections_dir = self._reflections_dir()
        if not reflections_dir.exists():
            return []

        window_days = self._safe_days(days)
        items: list[dict[str, Any]] = []
        for persona_name, persona_dir in self._scan_persona_dirs(reflections_dir):
            for fp in persona_dir.glob("*.json"):
                date_str = fp.stem.strip()
                if not self._date_in_window(date_str, window_days):
                    continue
                try:
                    import json
                    rows = json.loads(fp.read_text(encoding="utf-8"))
                    if not isinstance(rows, list):
                        rows = []
                except Exception:
                    rows = []
                preview = rows[-1].get("content", "") if rows else ""
                items.append(
                    {
                        "date": date_str,
                        "persona_name": persona_name,
                        "count": len(rows),
                        "preview": self._build_preview(preview, limit=90),
                        "first_time": rows[0].get("time", "") if rows else "",
                        "last_time": rows[-1].get("time", "") if rows else "",
                        "starred": False,
                        "note": "",
                    }
                )

        items.sort(key=lambda x: (x["date"], x.get("persona_name", "")), reverse=True)
        return items

    def _read_reflection_day(self, date_str: str, persona_name: str | None = None) -> dict[str, Any] | None:
        normalized_persona = self._normalize_persona_name(persona_name)
        if normalized_persona:
            fp = self._reflections_dir() / normalized_persona / f"{date_str}.json"
            if not fp.exists():
                return None
            import json
            rows = json.loads(fp.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                rows = []
            return {
                "date": date_str,
                "persona_name": normalized_persona,
                "count": len(rows),
                "items": rows,
                "starred": False,
                "note": "",
            }
        for current_persona, persona_dir in self._scan_persona_dirs(self._reflections_dir()):
            fp = persona_dir / f"{date_str}.json"
            if not fp.exists():
                continue
            import json
            rows = json.loads(fp.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                rows = []
            return {
                "date": date_str,
                "persona_name": current_persona,
                "count": len(rows),
                "items": rows,
                "starred": False,
                "note": "",
            }
        return None

    def _read_memory_status(self, date_str: str, persona_name: str | None = None) -> str:
        if persona_name:
            meta_file = self._diaries_dir() / persona_name / f"{date_str}.json"
            if not meta_file.exists():
                return "unknown"
            try:
                import json
                data = json.loads(meta_file.read_text(encoding="utf-8"))
                status = str(data.get("memory_status") or "unknown").strip() or "unknown"
                return status
            except Exception:
                return "unknown"
        for name, _ in self._scan_persona_dirs(self._diaries_dir()):
            status = self._read_memory_status(date_str, name)
            if status != "unknown":
                return status
        return "unknown"

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

    def _build_index_html(self) -> str:
        return r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>DayMind · Archive & Starfield</title>
  <style>
    :root {
      --bg: #070a13;
      --bg-2: #0d1324;
      --panel: rgba(13,19,35,.82);
      --panel-2: rgba(18,24,42,.82);
      --line: rgba(154,176,255,.14);
      --line-2: rgba(154,176,255,.22);
      --text: #edf2ff;
      --muted: #95a2c6;
      --gold: #f1cb81;
      --cyan: #8fd8ff;
      --violet: #a88dff;
      --rose: #ff9cc8;
      --ok: #8fe0ae;
      --danger: #ff9a9a;
      --shadow: 0 28px 80px rgba(0,0,0,.42);
      --display: Georgia, "Times New Roman", serif;
      --body: "Microsoft YaHei", "Segoe UI", sans-serif;
      --numeric: "Trebuchet MS", "Segoe UI", "Microsoft YaHei", sans-serif;
      --r-xl: 30px;
      --r-lg: 22px;
      --r-md: 18px;
      --pill: 999px;
    }

    * { box-sizing: border-box; }
    html, body { margin: 0; min-height: 100%; }
    body {
      font-family: var(--body);
      color: var(--text);
      background:
        radial-gradient(circle at 14% 16%, rgba(143,216,255,.08), transparent 18%),
        radial-gradient(circle at 86% 18%, rgba(168,141,255,.10), transparent 22%),
        radial-gradient(circle at 62% 84%, rgba(255,156,200,.08), transparent 20%),
        linear-gradient(180deg, #060912 0%, #0a1121 46%, #050811 100%);
      overflow-x: hidden;
    }

    body::before,
    body::after {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      z-index: 0;
      background-repeat: repeat;
      opacity: .5;
      mix-blend-mode: screen;
    }

    body::before {
      background-image:
        radial-gradient(4px 4px at 10% 20%, rgba(255,255,255,.95), transparent 62%),
        radial-gradient(3px 3px at 28% 72%, rgba(143,216,255,.95), transparent 62%),
        radial-gradient(4px 4px at 78% 14%, rgba(241,203,129,.92), transparent 62%),
        radial-gradient(3.4px 3.4px at 70% 78%, rgba(168,141,255,.9), transparent 62%),
        radial-gradient(3px 3px at 18% 88%, rgba(255,255,255,.86), transparent 62%),
        radial-gradient(3.2px 3.2px at 88% 58%, rgba(143,216,255,.88), transparent 62%),
        radial-gradient(4.4px 4.4px at 54% 10%, rgba(255,255,255,.96), transparent 62%),
        radial-gradient(2.4px 2.4px at 42% 38%, rgba(255,255,255,.72), transparent 62%),
        radial-gradient(2.8px 2.8px at 62% 54%, rgba(241,203,129,.66), transparent 62%),
        radial-gradient(2.6px 2.6px at 84% 86%, rgba(168,141,255,.72), transparent 62%);
      opacity: .82;
      animation: twinkleA 5.8s ease-in-out infinite alternate;
    }

    body::after {
      background-image:
        radial-gradient(2.8px 2.8px at 16% 34%, rgba(255,255,255,.82), transparent 62%),
        radial-gradient(3.8px 3.8px at 42% 82%, rgba(255,255,255,.84), transparent 62%),
        radial-gradient(3px 3px at 64% 28%, rgba(241,203,129,.8), transparent 62%),
        radial-gradient(2.8px 2.8px at 82% 76%, rgba(168,141,255,.84), transparent 62%),
        radial-gradient(3.2px 3.2px at 92% 22%, rgba(143,216,255,.82), transparent 62%),
        radial-gradient(2.3px 2.3px at 34% 12%, rgba(255,255,255,.74), transparent 62%),
        radial-gradient(2.9px 2.9px at 58% 60%, rgba(255,255,255,.8), transparent 62%),
        radial-gradient(2.2px 2.2px at 8% 66%, rgba(143,216,255,.66), transparent 62%),
        radial-gradient(2.4px 2.4px at 73% 8%, rgba(255,255,255,.7), transparent 62%),
        radial-gradient(2.2px 2.2px at 51% 92%, rgba(241,203,129,.62), transparent 62%);
      opacity: .64;
      animation: twinkleB 7.4s ease-in-out infinite alternate;
    }

    .app {
      position: relative;
      z-index: 1;
      max-width: 1760px;
      min-height: 100vh;
      margin: 0 auto;
      padding: 18px;
      display: grid;
      grid-template-columns: 290px minmax(0, 1fr);
      gap: 18px;
    }

    .glass {
      background: linear-gradient(180deg, rgba(14,20,35,.88), rgba(9,13,24,.78));
      border: 1px solid var(--line);
      box-shadow: var(--shadow);
      backdrop-filter: blur(16px) saturate(118%);
      -webkit-backdrop-filter: blur(16px) saturate(118%);
    }

    .sidebar {
      position: sticky;
      top: 18px;
      align-self: start;
      border-radius: var(--r-xl);
      padding: 20px;
      display: grid;
      gap: 12px;
    }

    .eyebrow {
      display: inline-flex;
      width: fit-content;
      padding: 7px 12px;
      border-radius: var(--pill);
      font-size: 11px;
      letter-spacing: .14em;
      text-transform: uppercase;
      color: var(--gold);
      border: 1px solid rgba(241,203,129,.24);
      background: rgba(241,203,129,.08);
    }

    .brand-title {
      margin-top: 6px;
      font-family: var(--display);
      font-size: 34px;
      line-height: 1.02;
    }

    .module {
      padding: 14px;
      border-radius: 22px;
      border: 1px solid rgba(255,255,255,.06);
      background: rgba(255,255,255,.03);
    }

    .module h3 {
      margin: 0 0 10px;
      color: var(--muted);
      font-size: 11px;
      letter-spacing: .14em;
      text-transform: uppercase;
    }

    .row { display: flex; flex-wrap: wrap; gap: 8px; }

    button, select, input, textarea {
      font: inherit;
    }

    button {
      appearance: none;
      border: 1px solid rgba(255,255,255,.08);
      background: rgba(255,255,255,.05);
      color: var(--text);
      border-radius: var(--pill);
      padding: 10px 14px;
      font-weight: 700;
      cursor: pointer;
      transition: .18s ease;
    }

    button:hover { transform: translateY(-1px); }

    button.active {
      border-color: transparent;
      background: linear-gradient(135deg, var(--gold), var(--cyan));
      color: #08111e;
      box-shadow: 0 10px 28px rgba(143,216,255,.16);
    }

    .btn-soft { background: rgba(255,255,255,.04); }
    .btn-danger { border-color: rgba(255,154,154,.18); color: #ffd6d6; background: rgba(255,120,120,.08); }
    .btn-block { width: 100%; justify-content: center; display: inline-flex; }

    .mini-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }

    .mini {
      border-radius: 18px;
      padding: 12px;
      border: 1px solid rgba(255,255,255,.06);
      background: rgba(255,255,255,.03);
    }

    .mini .k { color: var(--muted); font-size: 11px; }
    .mini .v { margin-top: 8px; font-size: 18px; font-weight: 800; }

    .main {
      min-width: 0;
      display: grid;
      gap: 18px;
      min-height: calc(100vh - 36px);
      align-content: start;
    }

    .hero {
      border-radius: var(--r-xl);
      padding: 22px 24px;
      min-height: 210px;
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) 320px;
      gap: 16px;
      align-items: center;
      overflow: hidden;
      position: relative;
    }

    body.star-view .hero {
      min-height: 132px;
      padding: 16px 20px;
      grid-template-columns: minmax(0, 1fr) 220px;
      gap: 12px;
    }

    body.star-view .hero-visual {
      min-height: 96px;
    }

    body.star-view .main {
      gap: 14px;
    }

    .hero::before {
      content: "";
      position: absolute;
      width: 440px;
      height: 440px;
      right: -140px;
      top: -200px;
      background: radial-gradient(circle, rgba(168,141,255,.24), transparent 64%);
      filter: blur(10px);
      pointer-events: none;
    }

    .hero-title {
      margin: 8px 0 0;
      font-family: var(--display);
      font-size: clamp(34px, 3.2vw, 52px);
      line-height: .98;
      text-wrap: balance;
      letter-spacing: -.01em;
    }

    .hero-copy {
      margin-top: 12px;
      color: var(--muted);
      line-height: 1.84;
      max-width: 56ch;
      font-size: 13px;
      min-height: 22px;
    }

    .hero-visual {
      border-radius: 24px;
      border: 1px solid rgba(255,255,255,.08);
      background:
        radial-gradient(circle at center, rgba(143,216,255,.16), transparent 34%),
        radial-gradient(circle at center, rgba(241,203,129,.12), transparent 54%),
        linear-gradient(180deg, rgba(255,255,255,.05), rgba(255,255,255,.02));
      position: relative;
      overflow: hidden;
      min-height: 164px;
    }

    .hero-visual::before,
    .hero-visual::after {
      content: "";
      position: absolute;
      border-radius: 50%;
      inset: 14px;
      border: 1px dashed rgba(255,255,255,.16);
    }

    .hero-visual::after {
      inset: 36px;
      border-style: solid;
      border-color: rgba(241,203,129,.2);
    }

    .hero-dot {
      position: absolute;
      border-radius: 50%;
      box-shadow: 0 0 18px currentColor;
    }

    .hero-dot.a { width: 18px; height: 18px; left: 58%; top: 18%; color: var(--gold); background: var(--gold); }
    .hero-dot.b { width: 14px; height: 14px; left: 22%; top: 66%; color: var(--violet); background: var(--violet); }
    .hero-dot.c { width: 26px; height: 26px; left: 42%; top: 42%; color: var(--cyan); background: var(--cyan); }

    .workspace {
      display: grid;
      grid-template-columns: 380px minmax(0, 1fr) 320px;
      gap: 18px;
      min-width: 0;
    }

    .panel {
      border-radius: var(--r-xl);
      overflow: hidden;
      min-width: 0;
    }

    .panel-head { padding: 18px 18px 0; }
    .panel-body { padding: 16px 18px 18px; }

    .panel-title {
      font-family: var(--display);
      font-size: 28px;
      line-height: 1;
    }

    .panel-sub {
      margin-top: 8px;
      color: var(--muted);
      line-height: 1.7;
      font-size: 12px;
    }

    .searchbar { margin: 14px 18px 0; position: relative; }
    .searchbar input,
    .field input,
    .field select,
    .field textarea {
      width: 100%;
      border-radius: 16px;
      border: 1px solid rgba(255,255,255,.08);
      background: rgba(255,255,255,.05);
      color: var(--text);
      padding: 12px 14px;
      outline: none;
    }

    .field textarea {
      min-height: 160px;
      resize: vertical;
      line-height: 1.8;
    }

    .searchbar input { padding-left: 44px; }

    .searchbar::before {
      content: "⌕";
      position: absolute;
      left: 16px;
      top: 9px;
      color: var(--muted);
      font-size: 18px;
    }

    .filter-row {
      padding: 12px 18px 0;
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }

    .stream-list {
      padding: 16px 18px 18px;
      display: grid;
      gap: 10px;
      max-height: calc(100vh - 320px);
      overflow: auto;
    }

    .entry {
      border-radius: 22px;
      padding: 14px;
      border: 1px solid rgba(255,255,255,.08);
      background:
        linear-gradient(180deg, rgba(255,255,255,.05), rgba(255,255,255,.025)),
        radial-gradient(circle at right top, rgba(168,141,255,.1), transparent 40%);
      cursor: pointer;
      transition: .2s ease;
    }

    .entry:hover { transform: translateY(-2px); border-color: var(--line-2); }
    .entry.active {
      border-color: rgba(241,203,129,.28);
      box-shadow: inset 0 0 0 1px rgba(241,203,129,.2);
    }

    .entry-top {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: start;
    }

    .entry-title {
      font-size: 15px;
      line-height: 1.5;
      font-weight: 800;
    }

    .entry-date {
      color: var(--gold);
      font-size: 11px;
      letter-spacing: .12em;
      text-transform: uppercase;
      white-space: nowrap;
      font-family: var(--numeric);
      font-variant-numeric: tabular-nums;
    }

    .entry-preview {
      margin-top: 8px;
      color: var(--muted);
      line-height: 1.76;
      font-size: 13px;
      display: -webkit-box;
      -webkit-line-clamp: 3;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }

    .chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }

    .chip {
      display: inline-flex;
      align-items: center;
      padding: 7px 10px;
      border-radius: var(--pill);
      font-size: 12px;
      border: 1px solid rgba(255,255,255,.06);
      background: rgba(255,255,255,.04);
      color: var(--text);
    }

    .chip.starred { border-color: rgba(241,203,129,.22); color: var(--gold); }

    .reader {
      min-height: 800px;
      display: grid;
      grid-template-rows: auto 1fr;
    }

    .reader-stage {
      padding: 18px 20px 22px;
      overflow: auto;
      min-width: 0;
    }

    .empty {
      min-height: 300px;
      display: grid;
      place-items: center;
      text-align: center;
      padding: 22px;
      border-radius: 24px;
      border: 1px dashed rgba(255,255,255,.12);
      color: var(--muted);
      line-height: 1.9;
      background: rgba(255,255,255,.02);
    }

    .folio {
      border-radius: 24px;
      overflow: hidden;
      border: 1px solid rgba(255,255,255,.08);
      background: linear-gradient(180deg, rgba(255,255,255,.05), rgba(255,255,255,.022));
      animation: pageIn .42s cubic-bezier(.2,.8,.2,1);
    }

    .folio-head {
      padding: 18px 18px 16px;
      border-bottom: 1px solid rgba(255,255,255,.08);
      background: radial-gradient(circle at right top, rgba(241,203,129,.08), transparent 30%);
    }

    .folio-tag {
      color: var(--gold);
      font-size: 11px;
      letter-spacing: .14em;
      text-transform: uppercase;
    }

    .folio-date { margin-top: 8px; color: var(--muted); font-size: 12px; font-family: var(--numeric); font-variant-numeric: tabular-nums; }

    .folio-title {
      margin-top: 8px;
      font-family: var(--display);
      font-size: clamp(24px, 2vw, 34px);
      line-height: 1.15;
      max-width: 22ch;
    }

    .folio-subtitle {
      margin-top: 8px;
      color: var(--cyan);
      font-size: 12px;
      letter-spacing: .16em;
      text-transform: uppercase;
      opacity: .92;
    }

    .title-date,
    .numeric {
      font-family: var(--numeric);
      font-variant-numeric: tabular-nums;
      letter-spacing: .02em;
    }

    .title-sep {
      opacity: .85;
      margin: 0 .08em;
    }

    .meta-grid {
      margin-top: 14px;
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }

    .meta {
      border-radius: 16px;
      padding: 10px;
      border: 1px solid rgba(255,255,255,.06);
      background: rgba(255,255,255,.04);
    }

    .meta .k { color: var(--muted); font-size: 11px; }
    .meta .v { margin-top: 6px; font-size: 15px; font-weight: 800; }

    .paper {
      position: relative;
      padding: 26px 26px 30px;
      background:
        linear-gradient(180deg, rgba(255,255,255,.028), rgba(255,255,255,.012)),
        radial-gradient(circle at 18% 12%, rgba(255,255,255,.035), transparent 24%),
        linear-gradient(180deg, rgba(10,14,26,.80), rgba(8,12,22,.72));
    }

    .paper::before {
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      opacity: .3;
      background:
        repeating-linear-gradient(
          to bottom,
          transparent 0,
          transparent 35px,
          rgba(173,192,255,.3) 36px,
          transparent 37px
        );
      mask-image: linear-gradient(180deg, rgba(0,0,0,.82), rgba(0,0,0,.98));
    }

    .content {
      position: relative;
      z-index: 1;
      white-space: pre-wrap;
      word-break: break-word;
      line-height: 2.08;
      font-size: 18px;
      letter-spacing: .01em;
      color: rgba(237,242,255,.96);
      text-rendering: optimizeLegibility;
    }

    .timeline { display: grid; gap: 10px; }

    .pulse {
      position: relative;
      border-radius: 18px;
      border: 1px solid rgba(255,255,255,.07);
      background: rgba(255,255,255,.035);
      padding: 14px 14px 14px 16px;
      animation: pageIn .36s cubic-bezier(.2,.8,.2,1);
    }

    .pulse::before {
      content: "";
      position: absolute;
      left: 0;
      top: 0;
      bottom: 0;
      width: 3px;
      background: linear-gradient(180deg, var(--violet), var(--cyan), var(--gold));
    }

    .pulse-time {
      margin-left: 6px;
      color: var(--gold);
      font-size: 11px;
      letter-spacing: .12em;
      text-transform: uppercase;
      font-family: var(--numeric);
      font-variant-numeric: tabular-nums;
    }

    .pulse-text {
      margin-top: 8px;
      margin-left: 6px;
      line-height: 1.92;
      white-space: pre-wrap;
      font-size: 14px;
    }

    .side-stack {
      display: grid;
      gap: 14px;
      align-content: start;
      min-height: 800px;
    }

    .card {
      border-radius: 24px;
      padding: 16px;
      border: 1px solid rgba(255,255,255,.08);
      background: linear-gradient(180deg, rgba(255,255,255,.05), rgba(255,255,255,.025));
    }

    .card h4 {
      margin: 0 0 12px;
      color: var(--muted);
      font-size: 11px;
      letter-spacing: .14em;
      text-transform: uppercase;
    }

    .detail-title {
      font-family: var(--display);
      font-size: 24px;
      line-height: 1.15;
      margin: 0 0 8px;
    }

    .side-row {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }

    .stack { display: grid; gap: 10px; }

    .info-line {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.7;
    }

    .info-line strong { color: var(--text); font-weight: 700; }

    .hint {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.75;
    }

    .status {
      padding: 10px 12px;
      border-radius: 14px;
      font-size: 13px;
      line-height: 1.6;
      border: 1px solid rgba(255,255,255,.08);
      background: rgba(255,255,255,.04);
    }

    .status.ok { color: #d6ffe2; border-color: rgba(143,224,174,.14); background: rgba(143,224,174,.08); }
    .status.warn { color: #ffe6c3; border-color: rgba(241,203,129,.16); background: rgba(241,203,129,.08); }
    .status.err { color: #ffd7d7; border-color: rgba(255,154,154,.16); background: rgba(255,120,120,.08); }

    .star-mode {
      border-radius: var(--r-xl);
      overflow: hidden;
      min-height: 700px;
      display: none;
      position: relative;
    }

    .star-mode.active { display: block; }

    body.star-view .star-mode,
    body.star-view .star-stage {
      min-height: calc(100vh - 182px);
    }

    .star-stage {
      position: relative;
      min-height: 700px;
      overflow: hidden;
      background:
        radial-gradient(circle at center, rgba(143,216,255,.08), transparent 10%),
        radial-gradient(circle at center, rgba(241,203,129,.08), transparent 18%),
        radial-gradient(circle at center, rgba(168,141,255,.12), transparent 28%),
        linear-gradient(180deg, rgba(9,14,26,.92), rgba(6,10,18,.98));
      transform-origin: center center;
    }

    .star-stage::before {
      content: "";
      position: absolute;
      inset: 0;
      background:
        linear-gradient(rgba(255,255,255,.016) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,.016) 1px, transparent 1px);
      background-size: 40px 40px;
      opacity: .24;
      mask-image: radial-gradient(circle at center, rgba(0,0,0,1), transparent 94%);
      pointer-events: none;
    }

    .star-stage::after {
      content: "";
      position: absolute;
      inset: 0;
      pointer-events: none;
      background-image:
        radial-gradient(4px 4px at 18% 18%, rgba(255,255,255,.88), transparent 62%),
        radial-gradient(3px 3px at 30% 70%, rgba(143,216,255,.9), transparent 62%),
        radial-gradient(3.6px 3.6px at 78% 30%, rgba(241,203,129,.86), transparent 62%),
        radial-gradient(3.2px 3.2px at 68% 82%, rgba(168,141,255,.84), transparent 62%),
        radial-gradient(2.8px 2.8px at 86% 62%, rgba(255,255,255,.74), transparent 62%),
        radial-gradient(2.6px 2.6px at 8% 42%, rgba(255,255,255,.66), transparent 62%),
        radial-gradient(2.8px 2.8px at 92% 12%, rgba(143,216,255,.72), transparent 62%),
        radial-gradient(2.4px 2.4px at 48% 88%, rgba(241,203,129,.62), transparent 62%);
      opacity: .72;
      animation: twinkleStage 5.6s ease-in-out infinite alternate;
    }

    .star-head {
      position: absolute;
      left: 20px;
      top: 20px;
      right: 20px;
      z-index: 12;
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      transition: opacity .2s ease, filter .2s ease;
    }

    .star-stage.jumping .star-head,
    .star-stage.jumping .jump-btn {
      opacity: 0;
      filter: blur(3px);
    }

    .star-chip {
      padding: 9px 11px;
      border-radius: 16px;
      background: rgba(8,13,24,.68);
      border: 1px solid rgba(255,255,255,.08);
      color: var(--muted);
      font-size: 12px;
      line-height: 1.6;
    }

    .star-chip strong { color: var(--text); }

    .core {
      position: absolute;
      left: 50%;
      top: 50%;
      width: 116px;
      height: 116px;
      transform: translate(-50%, -50%);
      border-radius: 50%;
      background:
        radial-gradient(circle at 34% 32%, rgba(255,255,255,.98) 0%, rgba(255,255,255,.96) 12%, #dff0ff 26%, #9ad9ff 52%, rgba(154,217,255,.22) 68%, rgba(154,217,255,.06) 78%, transparent 82%),
        radial-gradient(circle at 58% 62%, rgba(241,203,129,.18), transparent 36%);
      box-shadow:
        0 0 104px rgba(143,216,255,.34),
        0 0 172px rgba(241,203,129,.18),
        inset 0 0 22px rgba(255,255,255,.35);
      z-index: 4;
      pointer-events: none;
      animation: corePulse 5.2s ease-in-out infinite;
    }

    .core::before,
    .core::after,
    .core i {
      content: "";
      position: absolute;
      border-radius: 50%;
    }

    .core::before {
      inset: -34px;
      border: 1px solid rgba(241,203,129,.24);
      box-shadow: 0 0 30px rgba(241,203,129,.08);
      animation: haloFloat 6s ease-in-out infinite;
    }

    .core::after {
      inset: -70px;
      border: 1px solid rgba(143,216,255,.18);
      box-shadow: 0 0 42px rgba(143,216,255,.06);
      animation: haloFloat 7.4s ease-in-out infinite reverse;
    }

    .core i {
      inset: -108px;
      border: 1px dashed rgba(255,255,255,.12);
      opacity: .5;
    }

    .orbit-field {
      position: absolute;
      inset: 0;
      z-index: 6;
      transition: transform .44s cubic-bezier(.18,.78,.2,1), filter .44s cubic-bezier(.18,.78,.2,1), opacity .44s cubic-bezier(.18,.78,.2,1);
    }

    .star-stage.jumping .orbit-field {
      transform: translate(var(--tx, 0px), var(--ty, 0px)) scale(2.08);
      filter: blur(6px) saturate(116%);
      opacity: .58;
    }

    .orbit {
      position: absolute;
      left: 50%;
      top: 50%;
      width: var(--size);
      height: var(--size);
      transform: translate(-50%, -50%);
      border-radius: 50%;
      border: 1.35px solid rgba(255,255,255,.16);
      box-shadow: inset 0 0 0 1px rgba(255,255,255,.025), 0 0 22px rgba(255,255,255,.02);
      animation: spin var(--speed) linear infinite;
    }

    .orbit.reverse { animation-direction: reverse; }
    .orbit.glow {
      border-color: rgba(241,203,129,.24);
      box-shadow: inset 0 0 18px rgba(241,203,129,.04), 0 0 18px rgba(241,203,129,.05);
    }

    .planet {
      position: absolute;
      left: 50%;
      top: 0;
      width: var(--planet-size);
      height: var(--planet-size);
      border: 0;
      border-radius: 50%;
      cursor: pointer;
      background: radial-gradient(circle at 32% 32%, #fff, var(--planet-color) 44%, rgba(255,255,255,.08) 80%);
      box-shadow: 0 0 24px color-mix(in srgb, var(--planet-color) 32%, transparent);
      transition: transform .18s ease, box-shadow .18s ease, filter .18s ease, opacity .22s ease;
    }

    .planet:hover,
    .planet.active {
      filter: saturate(122%);
      box-shadow: 0 0 0 8px color-mix(in srgb, var(--planet-color) 7%, transparent), 0 0 32px color-mix(in srgb, var(--planet-color) 30%, transparent);
    }

    .planet.starred-planet {
      box-shadow: 0 0 0 10px color-mix(in srgb, var(--planet-color) 7%, transparent), 0 0 38px color-mix(in srgb, var(--planet-color) 36%, transparent);
      filter: brightness(1.08) saturate(132%);
    }

    .planet.focused {
      z-index: 9;
      transform: var(--planet-transform) scale(1.9) !important;
      filter: brightness(1.08) saturate(132%);
      box-shadow: 0 0 0 12px color-mix(in srgb, var(--planet-color) 10%, transparent), 0 0 36px color-mix(in srgb, var(--planet-color) 36%, transparent);
    }

    .planet.ringed::before {
      content: "";
      position: absolute;
      left: 50%;
      top: 50%;
      width: 180%;
      height: 62%;
      transform: translate(-50%, -50%) rotate(24deg);
      border-radius: 50%;
      border: 1px solid color-mix(in srgb, var(--planet-color) 26%, transparent);
      opacity: .64;
      pointer-events: none;
    }

    .jump-btn {
      position: absolute;
      right: 22px;
      bottom: 18px;
      z-index: 12;
      padding: 11px 16px;
      border-radius: var(--pill);
      border: 1px solid rgba(255,255,255,.1);
      background: rgba(8,13,24,.78);
      color: var(--text);
      font-weight: 800;
      transition: opacity .2s ease, filter .2s ease;
    }

    .jump-flash {
      position: absolute;
      inset: 0;
      pointer-events: none;
      z-index: 14;
      opacity: 0;
      background: radial-gradient(circle at var(--x,50%) var(--y,50%), rgba(255,255,255,.56) 0%, rgba(143,216,255,.26) 6%, rgba(143,216,255,.1) 14%, rgba(6,10,18,0) 26%);
      transition: opacity .14s ease;
    }

    .star-stage.jumping .jump-flash {
      opacity: .92;
    }

    .hidden { display: none !important; }

    .stream-list::-webkit-scrollbar,
    .reader-stage::-webkit-scrollbar { width: 10px; }
    .stream-list::-webkit-scrollbar-thumb,
    .reader-stage::-webkit-scrollbar-thumb {
      background: rgba(255,255,255,.12);
      border-radius: 999px;
    }

    @keyframes spin {
      from { transform: translate(-50%, -50%) rotate(0deg); }
      to { transform: translate(-50%, -50%) rotate(360deg); }
    }

    @keyframes pageIn {
      0% { opacity: 0; transform: translateY(14px) scale(.992); filter: blur(6px); }
      100% { opacity: 1; transform: translateY(0) scale(1); filter: blur(0); }
    }

    @keyframes corePulse {
      0%, 100% { box-shadow: 0 0 100px rgba(143,216,255,.3), 0 0 156px rgba(241,203,129,.15), inset 0 0 22px rgba(255,255,255,.34); filter: brightness(1); }
      50% { box-shadow: 0 0 130px rgba(143,216,255,.42), 0 0 210px rgba(241,203,129,.2), inset 0 0 26px rgba(255,255,255,.38); filter: brightness(1.05); }
    }

    @keyframes haloFloat {
      0%, 100% { transform: scale(1); opacity: .62; }
      50% { transform: scale(1.045); opacity: .86; }
    }

    @keyframes twinkleA {
      0% { opacity: .48; filter: brightness(.92); }
      25% { opacity: .78; filter: brightness(1.1); }
      50% { opacity: .62; filter: brightness(1.28); }
      100% { opacity: .92; filter: brightness(1.16); }
    }

    @keyframes twinkleB {
      0% { opacity: .34; transform: scale(1) translateY(0); }
      45% { opacity: .58; transform: scale(1.02) translateY(-1px); }
      100% { opacity: .78; transform: scale(1.04) translateY(1px); }
    }

    @keyframes twinkleStage {
      0% { opacity: .42; }
      50% { opacity: .82; }
      100% { opacity: .56; }
    }

    @media (max-width: 1500px) {
      .workspace { grid-template-columns: 360px minmax(0, 1fr); }
      .side-stack { grid-column: 1 / -1; min-height: auto; grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }

    @media (max-width: 1380px) {
      .app { grid-template-columns: 1fr; }
      .sidebar { position: static; }
      .workspace { grid-template-columns: 1fr; }
      .stream-list { max-height: none; }
      .hero { grid-template-columns: 1fr; }
      .star-stage, .star-mode { min-height: 640px; }
      .side-stack { grid-template-columns: 1fr; }
      body.star-view .star-mode,
      body.star-view .star-stage { min-height: 640px; }
    }

    @media (max-width: 820px) {
      .mini-grid, .meta-grid { grid-template-columns: 1fr; }
      .hero-title { font-size: 42px; }
      .star-stage, .star-mode { min-height: 560px; }
      .paper { padding: 22px 18px 26px; }
      .content { font-size: 16px; line-height: 2; }
      body.star-view .star-mode,
      body.star-view .star-stage { min-height: 560px; }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside class="sidebar glass">
      <div class="eyebrow">DayMind · WebUI</div>
      <div class="brand-title">观测与星图</div>

      <div class="module">
        <h3>当前人格</h3>
        <div class="field">
          <select id="personaSelect"></select>
        </div>
      </div>

      <div class="module">
        <h3>界面模式</h3>
        <div class="row">
          <button id="viewOverview">观测</button>
          <button id="viewStar">星图</button>
        </div>
      </div>

      <div class="module">
        <h3>内容模式</h3>
        <div class="row">
          <button id="tabDiary">日记档案</button>
          <button id="tabReflection">思考脉冲</button>
        </div>
      </div>

      <div class="module">
        <h3>时间窗口</h3>
        <div class="row">
          <button data-days="1">1 天</button>
          <button data-days="3">3 天</button>
          <button data-days="7">7 天</button>
          <button data-days="-1">全部</button>
        </div>
      </div>

      <div class="module">
        <h3>快速状态</h3>
        <div class="mini-grid">
          <div class="mini"><div class="k">今日思考</div><div class="v" id="summaryReflection">0</div></div>
          <div class="mini"><div class="k">自动日记</div><div class="v" id="summaryDiary">-</div></div>
          <div class="mini"><div class="k">最近记录</div><div class="v" id="summaryLastReflection">-</div></div>
          <div class="mini"><div class="k">窗口</div><div class="v" id="summaryWindow">3天</div></div>
        </div>
      </div>
    </aside>

    <main class="main">
      <section class="hero glass">
        <div>
          <div class="eyebrow" id="heroBadge">Observation</div>
          <div class="hero-title" id="heroTitle">观测</div>
          <div class="hero-copy" id="heroQuote">把散落的记录放回时间里。</div>
        </div>
        <div class="hero-visual" aria-hidden="true">
          <span class="hero-dot a"></span>
          <span class="hero-dot b"></span>
          <span class="hero-dot c"></span>
        </div>
      </section>

      <section class="workspace" id="overviewMode">
        <section class="panel glass">
          <div class="panel-head">
            <div class="panel-title">档案索引</div>
            <div class="panel-sub" id="panelSub">按日期浏览日记档案。</div>
          </div>
          <div class="searchbar">
            <input id="searchInput" placeholder="搜索日期、标题、关键词…" />
          </div>
          <div class="filter-row">
            <button id="starredOnlyBtn" class="btn-soft">只看星标</button>
          </div>
          <div class="stream-list" id="streamList"></div>
        </section>

        <section class="panel glass reader">
          <div class="panel-head">
            <div class="panel-title">主阅读台</div>
          </div>
          <div class="reader-stage" id="detailPanel">
            <div class="empty">先从左侧选择一天，或切到星图模式随机进入。</div>
          </div>
        </section>

        <aside class="side-stack" id="sidePanel"></aside>
      </section>

      <section class="star-mode glass" id="starMode">
        <div class="star-stage" id="starStage">
          <div class="star-head">
            <div class="star-chip">人格：<strong id="starPersonaLabel">-</strong></div>
            <div class="star-chip">内容：<strong id="starModeLabel">Diary</strong></div>
            <div class="star-chip">窗口：<strong id="starWindowLabel">3 天</strong></div>
            <div class="star-chip">星球数：<strong id="starCountLabel">0</strong></div>
          </div>
          <div class="core"><i></i></div>
          <div class="orbit-field" id="orbitField"></div>
          <button class="jump-btn" id="randomEnter">随机跃迁</button>
          <div class="jump-flash" id="jumpFlash"></div>
        </div>
      </section>
    </main>
  </div>

  <script>
    const MAX_STARS = 15;

    const state = {
      mode: 'diary',
      view: localStorage.getItem('daymind-view-mode') || 'overview',
      days: Number(localStorage.getItem('daymind-window-days') || 3),
      diaries: [],
      reflections: [],
      selectedDate: null,
      selectedPersona: localStorage.getItem('daymind-selected-persona') || '',
      status: null,
      config: null,
      activeDetail: null,
      activeStarDate: null,
      galaxyBuiltFor: '',
      jumping: false,
      starredOnly: false,
      savingNote: false,
    };

    const $ = (id) => document.getElementById(id);
    const esc = (v) => String(v ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
    const fmt = (ts) => ts ? new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false }) : '未知';
    const pick = (arr) => arr[Math.floor(Math.random() * arr.length)];

    function currentPersona() {
      return (state.selectedPersona || '').trim();
    }

    function personaQuery() {
      const p = currentPersona();
      return p ? `persona_name=${encodeURIComponent(p)}` : '';
    }

    function appendPersona(url) {
      const q = personaQuery();
      if (!q) return url;
      return url.includes('?') ? `${url}&${q}` : `${url}?${q}`;
    }

    function modeLabel(mode) {
      return mode === 'diary' ? 'Diary' : 'Pulse';
    }

    function modePrimaryLabel(mode) {
      return mode === 'diary' ? '日记档案' : '思考脉冲';
    }

    function formatComposedTitle(date, label) {
      return `<span class="title-date">${esc(date)}</span><span class="title-sep"> · </span>${esc(label)}`;
    }

    async function api(url, options = {}) {
      const res = await fetch(url, {
        headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
        ...options,
      });
      if (!res.ok) {
        let msg = url;
        try {
          const err = await res.json();
          msg = err.detail || JSON.stringify(err);
        } catch (_) {}
        throw new Error(msg);
      }
      return await res.json();
    }

    function currentItems() {
      const src = state.mode === 'diary' ? state.diaries : state.reflections;
      return src.slice(0, MAX_STARS);
    }

    function getCurrentCollection() {
      return state.mode === 'diary' ? state.diaries : state.reflections;
    }

    function setToast(msg, kind = 'ok') {
      $('summaryDiary').textContent = msg.length > 6 ? msg.slice(0, 6) + '…' : msg;
    }

    function setView(view) {
      state.view = view;
      localStorage.setItem('daymind-view-mode', view);
      document.body.classList.toggle('star-view', view === 'star');
      $('viewOverview').classList.toggle('active', view === 'overview');
      $('viewStar').classList.toggle('active', view === 'star');
      $('overviewMode').classList.toggle('hidden', view !== 'overview');
      $('starMode').classList.toggle('active', view === 'star');
      $('heroBadge').textContent = view === 'overview' ? 'Observation' : 'Star Map';
      $('heroTitle').textContent = view === 'overview' ? '观测' : '星图';
      $('heroQuote').textContent = view === 'overview'
        ? '把散落的记录放回时间里。'
        : '从轨道中随机坠入。';
    }

    function setMode(mode) {
      state.mode = mode;
      state.selectedDate = null;
      state.activeDetail = null;
      $('tabDiary').classList.toggle('active', mode === 'diary');
      $('tabReflection').classList.toggle('active', mode === 'reflection');
      $('panelSub').textContent = mode === 'diary' ? '按日期浏览日记档案。' : '按日期浏览某一天的思考脉冲。';
      $('starModeLabel').textContent = modeLabel(mode);
      state.activeStarDate = null;
      state.galaxyBuiltFor = '';
      renderList();
      renderEmpty();
      renderSidePanel();
      buildGalaxyIfNeeded(true);
    }

    function setDays(days) {
      state.days = Number(days);
      localStorage.setItem('daymind-window-days', String(state.days));
      document.querySelectorAll('[data-days]').forEach(btn => {
        btn.classList.toggle('active', Number(btn.dataset.days) === state.days);
      });
      const text = state.days === -1 ? '全部' : `${state.days} 天`;
      $('starWindowLabel').textContent = text;
      $('summaryWindow').textContent = text;
      loadStreams();
    }

    function renderPersonaSelector() {
      const status = state.status?.data || {};
      const personas = status.available_personas || [];
      const effective = status.effective_persona || currentPersona() || personas[0] || '';
      const select = $('personaSelect');
      select.innerHTML = personas.map(name => `<option value="${esc(name)}" ${name === effective ? 'selected' : ''}>${esc(name)}</option>`).join('');
      if (!state.selectedPersona && effective) {
        state.selectedPersona = effective;
      } else if (effective) {
        state.selectedPersona = effective;
      }
      localStorage.setItem('daymind-selected-persona', state.selectedPersona || '');
      $('starPersonaLabel').textContent = state.selectedPersona || '未选择';
    }

    function renderStatus() {
      const s = state.status?.data || {};
      $('summaryReflection').textContent = `${s.today_reflections_count ?? 0}`;
      $('summaryDiary').textContent = s.enable_auto_diary ? '运行中' : '关闭';
      $('summaryLastReflection').textContent = s.last_reflection_time ? String(s.last_reflection_time) : '未记';
      renderPersonaSelector();
    }

    function renderList() {
      const kw = ($('searchInput').value || '').trim().toLowerCase();
      const items = getCurrentCollection().filter(item => {
        if (state.starredOnly && !item.starred) return false;
        return !kw || JSON.stringify(item).toLowerCase().includes(kw);
      });

      if (!items.length) {
        $('streamList').innerHTML = `<article class="entry"><div class="entry-title">暂无匹配内容</div></article>`;
        return;
      }

      $('streamList').innerHTML = items.map(item => {
        if (state.mode === 'diary') {
          return `
            <article class="entry ${state.selectedDate === item.date ? 'active' : ''}" data-date="${item.date}" data-persona="${esc(item.persona_name || '')}">
              <div class="entry-top">
                <div class="entry-title"><span class="numeric">${esc(item.date)}</span><span class="title-sep"> · </span>${esc(item.persona_name || '未知人格')} · 日记档案</div>
                <div class="entry-date">Diary</div>
              </div>
              <div class="entry-preview">${esc(item.preview || '（无预览）')}</div>
              <div class="chips">
                ${item.starred ? '<span class="chip starred">★ 已星标</span>' : ''}
                <span class="chip">${item.length} 字</span>
                <span class="chip">更新 · ${fmt(item.updated_at)}</span>
              </div>
            </article>`;
        }
        return `
          <article class="entry ${state.selectedDate === item.date ? 'active' : ''}" data-date="${item.date}" data-persona="${esc(item.persona_name || '')}">
            <div class="entry-top">
              <div class="entry-title"><span class="numeric">${esc(item.date)}</span><span class="title-sep"> · </span>${esc(item.persona_name || '未知人格')} · 思考脉冲</div>
              <div class="entry-date">Pulse</div>
            </div>
            <div class="entry-preview">${esc(item.preview || '（暂无预览）')}</div>
            <div class="chips">
              ${item.starred ? '<span class="chip starred">★ 已星标</span>' : ''}
              <span class="chip">${item.count} 条记录</span>
              <span class="chip">${esc(item.first_time || '--')} → ${esc(item.last_time || '--')}</span>
            </div>
          </article>`;
      }).join('');

      document.querySelectorAll('.entry[data-date]').forEach(el => {
        el.addEventListener('click', () => openDetail(el.dataset.date, false, false, el.dataset.persona || currentPersona()));
      });
    }

    function renderEmpty() {
      $('detailPanel').innerHTML = `<div class="empty">${state.mode === 'diary' ? '先从左侧选择一天，或切到星图模式随机进入。' : '先从左侧选择一天，或切到星图模式随机进入某天思考。'}</div>`;
    }

    function renderSidePanel() {
      const root = $('sidePanel');
      const cfg = state.config || {};
      const detail = state.activeDetail;
      const status = state.status?.data || {};

      const detailCard = detail ? `
        <section class="card">
          <h4>边注与操作</h4>
          <div class="detail-title"><span class="numeric">${esc(detail.date)}</span><span class="title-sep"> · </span>${esc(detail.persona_name || currentPersona() || '未知人格')}</div>
          <div class="side-row">
            <button id="toggleStarBtn" class="${detail.starred ? 'active' : 'btn-soft'}">${detail.starred ? '★ 已星标' : '☆ 添加星标'}</button>
          </div>
          <div class="stack" style="margin-top:12px">
            <div class="status ${detail.starred ? 'warn' : 'ok'}">${detail.starred ? '已星标：该内容不会参与自动轮换删除。' : '未星标：遵循当前保留策略。'}</div>
            <div class="field">
              <textarea id="noteInput" placeholder="给这一天写一条边注、补充印象或提醒…">${esc(detail.note || '')}</textarea>
            </div>
            <div class="side-row">
              <button id="saveNoteBtn">保存备注</button>
              <button id="clearNoteBtn" class="btn-soft">清空备注</button>
            </div>
          </div>
        </section>` : `
        <section class="card">
          <h4>边注与操作</h4>
          <div class="hint">这里会显示当前内容的星标、备注与状态。先从左侧打开一篇日记，或选择某天的思考流。</div>
        </section>`;

      root.innerHTML = `
        ${detailCard}
        <section class="card">
          <h4>内容状态</h4>
          <div class="stack">
            <div class="info-line"><span>当前人格</span><strong>${esc(currentPersona() || status.effective_persona || '未选择')}</strong></div>
            <div class="info-line"><span>今日思考</span><strong>${status.today_reflections_count ?? 0}</strong></div>
            <div class="info-line"><span>自动日记</span><strong>${status.enable_auto_diary ? '运行中' : '关闭'}</strong></div>
            <div class="info-line"><span>默认主题</span><strong>${esc(cfg.webui_default_theme || status.webui_default_theme || 'galaxy')}</strong></div>
            <div class="info-line"><span>默认模式</span><strong>${esc(cfg.webui_default_mode || status.webui_default_mode || 'overview')}</strong></div>
          </div>
        </section>

        <section class="card">
          <h4>保留与默认设置</h4>
          <div class="stack">
            <div class="field">
              <label class="hint">日记保留天数（-1 为无限）</label>
              <input id="cfgDiaryRetention" type="number" value="${Number(cfg.diary_retention_days ?? status.diary_retention_days ?? -1)}" />
            </div>
            <div class="field">
              <label class="hint">思考流保留天数（-1 为无限）</label>
              <input id="cfgReflectionRetention" type="number" value="${Number(cfg.reflection_retention_days ?? status.reflection_retention_days ?? 3)}" />
            </div>
            <div class="field">
              <label class="hint">默认主题</label>
              <select id="cfgTheme">
                <option value="galaxy" ${(cfg.webui_default_theme || status.webui_default_theme) === 'galaxy' ? 'selected' : ''}>galaxy</option>
              </select>
            </div>
            <div class="field">
              <label class="hint">默认进入模式</label>
              <select id="cfgMode">
                <option value="overview" ${(cfg.webui_default_mode || status.webui_default_mode) === 'overview' ? 'selected' : ''}>overview</option>
                <option value="star" ${(cfg.webui_default_mode || status.webui_default_mode) === 'star' ? 'selected' : ''}>star</option>
              </select>
            </div>
            <button id="saveConfigBtn" class="btn-block">保存设置</button>
          </div>
        </section>

        <section class="card">
          <h4>今日操作</h4>
          <div class="stack">
            <div class="hint">把今天的思考流重置为一张白纸。这个操作会清空当前人格今日本地思考记录。</div>
            <button id="resetTodayBtn" class="btn-danger btn-block">清除今日思考流</button>
          </div>
        </section>`;

      bindSideActions();
    }

    function bindSideActions() {
      const toggleStarBtn = $('toggleStarBtn');
      const saveNoteBtn = $('saveNoteBtn');
      const clearNoteBtn = $('clearNoteBtn');
      const saveConfigBtn = $('saveConfigBtn');
      const resetTodayBtn = $('resetTodayBtn');

      if (toggleStarBtn) toggleStarBtn.addEventListener('click', toggleStarred);
      if (saveNoteBtn) saveNoteBtn.addEventListener('click', saveNote);
      if (clearNoteBtn) clearNoteBtn.addEventListener('click', async () => {
        $('noteInput').value = '';
        await saveNote();
      });
      if (saveConfigBtn) saveConfigBtn.addEventListener('click', saveConfig);
      if (resetTodayBtn) resetTodayBtn.addEventListener('click', resetTodayReflections);
    }

    function galaxyBuildKey() {
      return `${currentPersona()}::${state.mode}::${state.days}::${state.starredOnly}::${currentItems().map(x => `${x.persona_name || ''}@${x.date}`).join('|')}`;
    }

    function updateGalaxySelection() {
      document.querySelectorAll('.planet').forEach(el => {
        el.classList.toggle('active', el.dataset.date === state.activeStarDate && (el.dataset.persona || '') === (currentPersona() || ''));
      });
    }

    function prepareJumpFromPlanet(planet) {
      const stage = $('starStage');
      const flash = $('jumpFlash');
      const stageRect = stage.getBoundingClientRect();
      const planetRect = planet.getBoundingClientRect();
      const cx = planetRect.left + planetRect.width / 2;
      const cy = planetRect.top + planetRect.height / 2;
      const stageCx = stageRect.left + stageRect.width / 2;
      const stageCy = stageRect.top + stageRect.height / 2;
      const tx = (stageCx - cx) * 0.14;
      const ty = (stageCy - cy) * 0.14;
      stage.style.setProperty('--tx', `${tx}px`);
      stage.style.setProperty('--ty', `${ty}px`);
      const x = ((cx - stageRect.left) / stageRect.width) * 100;
      const y = ((cy - stageRect.top) / stageRect.height) * 100;
      flash.style.setProperty('--x', `${x}%`);
      flash.style.setProperty('--y', `${y}%`);
      planet.classList.add('focused');
      stage.classList.add('jumping');
    }

    function resetJumpState() {
      const stage = $('starStage');
      const flash = $('jumpFlash');
      stage.classList.remove('jumping');
      stage.style.removeProperty('--tx');
      stage.style.removeProperty('--ty');
      flash.style.removeProperty('--x');
      flash.style.removeProperty('--y');
      document.querySelectorAll('.planet.focused').forEach(el => el.classList.remove('focused'));
    }

    function buildGalaxyIfNeeded(force = false) {
      const root = $('orbitField');
      const items = currentItems();
      $('starCountLabel').textContent = String(items.length);
      $('starPersonaLabel').textContent = currentPersona() || '未选择';
      if (!items.length) {
        root.innerHTML = '';
        state.galaxyBuiltFor = '';
        return;
      }

      const key = galaxyBuildKey();
      if (!force && state.galaxyBuiltFor === key) {
        updateGalaxySelection();
        return;
      }
      state.galaxyBuiltFor = key;

      const orbitCount = Math.min(5, Math.max(3, items.length));
      const baseSizes = [240, 380, 530, 700, 880];
      const baseSpeeds = [16, 22, 30, 40, 52];
      const orbitDefs = baseSizes.slice(0, orbitCount).map((size, i) => ({
        size,
        speed: baseSpeeds[i],
        reverse: i % 2 === 1,
        glow: i % 2 === 0,
      }));

      const groups = Array.from({ length: orbitDefs.length }, () => []);
      items.forEach((item, i) => groups[i % orbitDefs.length].push(item));

      const colors = ['var(--gold)', 'var(--cyan)', 'var(--violet)', 'var(--rose)'];
      let idx = 0;
      root.innerHTML = orbitDefs.map((orbit, orbitIdx) => {
        const rows = groups[orbitIdx];
        if (!rows.length) return '';
        const stars = rows.map((item, i) => {
          const angle = (360 / rows.length) * i + (orbitIdx * 11);
          const size = item.starred ? 52 + ((idx % 3) * 6) : 44 + ((idx % 4) * 6);
          const color = colors[idx % colors.length];
          const ringed = item.starred || idx % 4 === 0 ? 'ringed' : '';
          const starredClass = item.starred ? 'starred-planet' : '';
          const transform = `translate(-50%, -50%) rotate(${angle}deg) translateY(calc(var(--size) / -2)) rotate(${-angle}deg)`;
          idx += 1;
          return `<button class="planet ${ringed} ${starredClass}" data-date="${item.date}" data-persona="${esc(item.persona_name || currentPersona() || '')}" data-transform="${transform}" style="--planet-size:${size}px;--planet-color:${color};--planet-transform:${transform};transform:${transform};"></button>`;
        }).join('');
        return `<div class="orbit ${orbit.reverse ? 'reverse' : ''} ${orbit.glow ? 'glow' : ''}" style="--size:${orbit.size}px;--speed:${orbit.speed}s;">${stars}</div>`;
      }).join('');

      root.querySelectorAll('.planet').forEach(planet => {
        planet.addEventListener('click', () => enterFromStar(planet));
      });
      updateGalaxySelection();
    }

    async function loadConfig() {
      const res = await api('/api/config');
      state.config = res.data || {};
    }

    async function loadStatus() {
      state.status = await api(appendPersona('/api/status'));
      renderStatus();
    }

    async function loadStreams(keepSelection = false) {
      const selected = keepSelection ? state.selectedDate : null;
      const [diariesRes, reflectionsRes, statusRes, configRes] = await Promise.all([
        api(`/api/diaries?days=${state.days}&starred_only=${state.starredOnly}`),
        api(`/api/reflections?days=${state.days}&starred_only=${state.starredOnly}`),
        api(appendPersona('/api/status')),
        api('/api/config')
      ]);
      state.diaries = (diariesRes.data || []).filter(x => !currentPersona() || (x.persona_name || '') === currentPersona());
      state.reflections = (reflectionsRes.data || []).filter(x => !currentPersona() || (x.persona_name || '') === currentPersona());
      state.status = statusRes;
      state.config = configRes.data || {};
      renderStatus();
      renderList();
      buildGalaxyIfNeeded(true);
      renderSidePanel();

      if (selected) {
        const exists = getCurrentCollection().some(x => x.date === selected && (!currentPersona() || (x.persona_name || '') === currentPersona()));
        if (exists) {
          await openDetail(selected, false, true, currentPersona());
        } else {
          state.selectedDate = null;
          state.activeDetail = null;
          renderEmpty();
          renderSidePanel();
        }
      }
    }

    async function toggleStarred() {
      if (!state.activeDetail) return;
      const next = !state.activeDetail.starred;
      const base = state.mode === 'diary'
        ? `/api/diaries/${state.activeDetail.date}`
        : `/api/reflections/${state.activeDetail.date}`;
      const url = appendPersona(base);
      const res = await api(url, { method: 'PATCH', body: JSON.stringify({ starred: next }) });
      state.activeDetail = { ...state.activeDetail, ...(res.data || {}), starred: next };
      setToast(next ? '已星标' : '已取消');
      await loadStreams(true);
    }

    async function saveNote() {
      if (!state.activeDetail || state.savingNote) return;
      state.savingNote = true;
      try {
        const note = $('noteInput')?.value || '';
        const base = state.mode === 'diary'
          ? `/api/diaries/${state.activeDetail.date}`
          : `/api/reflections/${state.activeDetail.date}`;
        const url = appendPersona(base);
        const res = await api(url, { method: 'PATCH', body: JSON.stringify({ note }) });
        state.activeDetail = { ...state.activeDetail, ...(res.data || {}), note };
        setToast('已保存');
        await loadStreams(true);
      } finally {
        state.savingNote = false;
      }
    }

    async function saveConfig() {
      const payload = {
        diary_retention_days: Number($('cfgDiaryRetention').value || -1),
        reflection_retention_days: Number($('cfgReflectionRetention').value || 3),
        webui_default_theme: $('cfgTheme').value,
        webui_default_mode: $('cfgMode').value,
      };
      const res = await api('/api/config', { method: 'POST', body: JSON.stringify(payload) });
      state.config = res.data || payload;
      setToast('已保存');
      await loadStreams(true);
    }

    async function resetTodayReflections() {
      const ok = confirm('确定清除今天的思考流吗？这会把当前人格今日思考重置为空。');
      if (!ok) return;
      await api(appendPersona('/api/reflections/today/reset'), { method: 'POST' });
      setToast('已清空');
      await loadStreams(true);
    }

    async function openDetail(date, fromStar = false, silentRefresh = false, personaName = '') {
      state.selectedDate = date;
      if (personaName) {
        state.selectedPersona = personaName;
        localStorage.setItem('daymind-selected-persona', state.selectedPersona || '');
      }
      renderList();

      if (state.mode === 'diary') {
        const res = await api(appendPersona(`/api/diaries/${date}`));
        const d = res.data;
        state.activeDetail = d;
        $('detailPanel').innerHTML = `
          <article class="folio">
            <div class="folio-head">
              <div class="folio-tag">Diary</div>
              <div class="folio-date">${esc(d.date)} · ${esc(d.persona_name || currentPersona() || '未知人格')}</div>
              <div class="folio-title">${formatComposedTitle(d.date, `${d.persona_name || currentPersona() || '未知人格'} · 日记档案`)}</div>
              <div class="folio-subtitle">Diary</div>
              <div class="meta-grid">
                <div class="meta"><div class="k">日期</div><div class="v numeric">${esc(d.date)}</div></div>
                <div class="meta"><div class="k">更新时间</div><div class="v numeric">${fmt(d.updated_at)}</div></div>
              </div>
            </div>
            <div class="paper"><div class="content">${esc(d.content || '（空）')}</div></div>
          </article>`;
      } else {
        const res = await api(appendPersona(`/api/reflections/${date}`));
        const d = res.data;
        state.activeDetail = d;
        $('detailPanel').innerHTML = `
          <article class="folio">
            <div class="folio-head">
              <div class="folio-tag">Pulse</div>
              <div class="folio-date">${esc(d.date)} · ${esc(d.persona_name || currentPersona() || '未知人格')}</div>
              <div class="folio-title">${formatComposedTitle(d.date, `${d.persona_name || currentPersona() || '未知人格'} · 思考脉冲`)}</div>
              <div class="folio-subtitle">Pulse</div>
              <div class="meta-grid">
                <div class="meta"><div class="k">日期</div><div class="v numeric">${esc(d.date)}</div></div>
                <div class="meta"><div class="k">记录数量</div><div class="v">${d.count} 条</div></div>
              </div>
            </div>
            <div class="panel-body">
              <section class="timeline">
                ${(d.items || []).map((item, idx) => `
                  <article class="pulse" style="animation-delay:${idx * 20}ms">
                    <div class="pulse-time">${esc(item.time || '')}</div>
                    <div class="pulse-text">${esc(item.content || '')}</div>
                  </article>
                `).join('') || '<div class="empty">这一天没有可展示的思考脉冲。</div>'}
              </section>
            </div>
          </article>`;
      }

      renderSidePanel();
      state.activeStarDate = date;
      updateGalaxySelection();

      if (fromStar && !silentRefresh) {
        document.querySelector('.hero, .workspace')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    }

    async function enterFromStar(planet = null) {
      if (state.jumping) return;
      const items = currentItems();
      if (!items.length) return;
      const chosen = planet
        ? items.find(x => x.date === planet.dataset.date && (x.persona_name || currentPersona() || '') === (planet.dataset.persona || currentPersona() || ''))
        : pick(items);
      if (!chosen) return;

      state.jumping = true;
      state.activeStarDate = chosen.date;
      updateGalaxySelection();

      const fallbackPlanet = planet || document.querySelector(`.planet[data-date="${chosen.date}"][data-persona="${chosen.persona_name || currentPersona() || ''}"]`) || document.querySelector('.planet');
      if (fallbackPlanet) prepareJumpFromPlanet(fallbackPlanet);

      await new Promise(r => setTimeout(r, 320));
      setView('overview');
      await openDetail(chosen.date, true, false, chosen.persona_name || currentPersona());
      resetJumpState();
      state.jumping = false;
    }

    async function init() {
      await loadConfig();
      await loadStatus();
      setView(state.config?.webui_default_mode || state.view || 'overview');
      setDays(state.days);
      setMode('diary');
      renderSidePanel();
    }

    $('viewOverview').addEventListener('click', () => setView('overview'));
    $('viewStar').addEventListener('click', () => { setView('star'); buildGalaxyIfNeeded(true); });
    $('tabDiary').addEventListener('click', () => setMode('diary'));
    $('tabReflection').addEventListener('click', () => setMode('reflection'));
    $('randomEnter').addEventListener('click', () => enterFromStar());
    $('searchInput').addEventListener('input', renderList);
    $('starredOnlyBtn').addEventListener('click', async () => {
      state.starredOnly = !state.starredOnly;
      $('starredOnlyBtn').classList.toggle('active', state.starredOnly);
      await loadStreams(false);
    });
    $('personaSelect').addEventListener('change', async (e) => {
      state.selectedPersona = String(e.target.value || '').trim();
      localStorage.setItem('daymind-selected-persona', state.selectedPersona || '');
      state.selectedDate = null;
      state.activeDetail = null;
      state.activeStarDate = null;
      await loadStreams(false);
      renderEmpty();
      renderSidePanel();
    });
    document.querySelectorAll('[data-days]').forEach(btn => btn.addEventListener('click', () => setDays(btn.dataset.days)));

    init().catch(err => {
      console.error(err);
      $('detailPanel').innerHTML = `<div class="empty">页面初始化失败，请稍后重试。<br>${esc(err.message || String(err))}</div>`;
    });
  </script>
</body>
</html>'''
