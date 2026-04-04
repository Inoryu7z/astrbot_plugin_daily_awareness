"""
日记生成模块
负责生成每日日记
"""

import datetime
import re
from pathlib import Path
from typing import Optional
from astrbot.api import logger

from .dependency import DependencyManager
from .persona_utils import PersonaConfigMixin


class DiaryGenerator(PersonaConfigMixin):
    """日记生成器"""

    def __init__(self, context, config: dict, dependency_manager: DependencyManager):
        self.context = context
        self.config = config
        self.dependency_manager = dependency_manager

        raw_data_dir = None
        try:
            getter = getattr(context, "get_data_dir", None)
            if callable(getter):
                raw_data_dir = getter()
        except Exception:
            raw_data_dir = None

        if raw_data_dir:
            self.data_dir = Path(raw_data_dir)
        else:
            try:
                from astrbot.core.star.star_tools import StarTools
                self.data_dir = Path(StarTools.get_data_dir())
            except Exception:
                self.data_dir = Path(".")

    def _get_diary_template(self, persona_name: str | None = None) -> str:
        override = str(self._persona_value(persona_name, "diary_prompt_template_override", "") or "").strip()
        if override:
            return override
        default_template = str(self.config.get("default_diary_prompt_template", "") or "").strip()
        if default_template:
            return default_template
        return self._get_default_template()

    async def generate(
        self,
        date_str: str,
        reflections: list[str],
        session_id: str | None = None,
        persona_name: str | None = None,
        persona_desc: str | None = None,
        ensured_schedule: dict | None = None,
        **kwargs,
    ) -> Optional[str]:
        """生成日记（缺日程时自动尝试补生成今日日程）"""
        try:
            resolved_name = self._canonical_persona_name(persona_name)
            resolved_desc = persona_desc
            if session_id and (not resolved_name or not resolved_desc):
                persona_ctx = await self.dependency_manager.resolve_persona_context(session_id)
                resolved_name = resolved_name or self._canonical_persona_name(persona_ctx.get("persona_name") or persona_ctx.get("persona_id"))
                resolved_desc = resolved_desc or persona_ctx.get("persona_desc")

            schedule_result = ensured_schedule if isinstance(ensured_schedule, dict) else None
            if not schedule_result:
                schedule_result = await self.dependency_manager.ensure_today_schedule(
                    session_id=session_id,
                    persona_name=resolved_name,
                    persona_desc=resolved_desc,
                    debug=bool(self.config.get("debug_mode", False)),
                )
            schedule_data = schedule_result.get("data") or {}
            if schedule_result.get("status") == "failed":
                logger.warning(
                    f"[DiaryGenerator] 今日日程不可用，跳过日记生成: session={session_id}, "
                    f"persona={resolved_name}, reason={schedule_result.get('message', '')}"
                )
                return None

            if self.config.get("debug_mode", False):
                logger.info(
                    f"[DiaryGenerator][debug] generate params: session={session_id}, persona={resolved_name}, reflections={len(reflections)}, "
                    f"schedule_status={schedule_result.get('status')}, generated_now={schedule_result.get('generated_now')}, "
                    f"schedule_outfit={str(schedule_data.get('outfit', ''))[:120]}, schedule={str(schedule_data.get('schedule', ''))[:300]}"
                )

            recent_diaries = self._load_recent_diaries(date_str, resolved_name)
            prompt = self._build_prompt(
                date_str,
                schedule_data,
                reflections,
                resolved_name,
                resolved_desc,
                recent_diaries,
            )
            result = await self._call_llm(prompt, resolved_name)
            if result:
                result = self._post_process_result(result)
            return result

        except Exception as e:
            logger.error(f"[DiaryGenerator] 生成日记失败: {e}", exc_info=True)
            return None

    def _build_prompt(
        self,
        date_str: str,
        schedule_data: dict,
        reflections: list[str],
        persona_name: Optional[str] = None,
        persona_desc: Optional[str] = None,
        recent_diaries: Optional[str] = None,
    ) -> str:
        template = self._get_diary_template(persona_name)

        template = self._ensure_recent_diaries_placeholder(template)
        template = self._ensure_mode_definition_placeholder(template)

        mode = self._persona_value(persona_name, "diary_mode", "适量")
        if mode == "简洁":
            mode_desc = "简洁"
            length_hint = "- 简洁记录今日最重要的主线与情绪落点，不追求完整覆盖全天；控制在300字左右"
            mode_definition = "- 简洁：聚焦今天最重要的主线与最终的情绪落点，不追求完整覆盖全天；控制在300字左右。"
        elif mode == "适量":
            mode_desc = "适量"
            length_hint = "- 围绕2—4个最值得留下的节点，写出这一天如何展开、转折和收束，让经历与情绪自然连在一起；控制在500字左右"
            mode_definition = "- 适量：围绕2—4个最值得留下的节点，写出这一天如何展开、转折和收束，让经历与情绪自然连在一起；控制在500字左右。"
        else:
            mode_desc = "丰富"
            length_hint = "- 在不改变现实主线的前提下，更完整地写出今天的生活流动感，可补充少量符合场景逻辑的细节、停顿与环境互动，但必须服务于这一天的经历线与情绪线，而不是装饰画面；控制在1000字左右"
            mode_definition = "- 丰富：在不改变现实主线的前提下，更完整地写出今天的生活流动感，可补充少量符合场景逻辑的细节、停顿与环境互动，但必须服务于这一天的经历线与情绪线，而不是装饰画面；控制在1000字左右。"

        if reflections:
            reflections_str = "\n".join([f"- {r}" for r in reflections])
        else:
            reflections_str = "（今日暂无思考记录，请基于现实轨迹还原自然心境）"

        outfit = schedule_data.get("outfit", "")
        schedule = schedule_data.get("schedule", "")

        state_info = ""
        if outfit:
            state_info += f"穿着：{outfit}\n"
        if schedule:
            state_info += f"日程：{schedule}"

        if not state_info:
            state_info = "（暂无日程信息）"

        persona_name_text = persona_name or "（未绑定人格）"
        persona_desc_text = persona_desc or "（未获取到人格设定文本，请保持稳定自然、贴近既有人设气质的语气）"
        current_date = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        recent_diaries_text = recent_diaries or "无历史日记参考"

        if self.config.get("debug_mode", False):
            history_count = 0 if recent_diaries_text == "无历史日记参考" else recent_diaries_text.count("\n\n") + 1
            logger.info(
                f"[DiaryGenerator] 历史日记参考: count={history_count}, length={len(recent_diaries_text)}, date={date_str}, persona={persona_name_text}"
            )
            logger.info(
                f"[DiaryGenerator][debug] prompt state_info={state_info.strip()[:500]}, reflections_preview={reflections_str[:500]}"
            )

        try:
            prompt = template.format(
                date=date_str,
                current_date=current_date,
                persona_name=persona_name_text,
                persona_desc=persona_desc_text,
                state_info=state_info.strip(),
                reflections=reflections_str,
                recent_diaries=recent_diaries_text,
                mode_desc=mode_desc,
                mode_definition=mode_definition,
                length_hint=length_hint,
            )
        except KeyError as e:
            logger.warning(f"[DiaryGenerator] 模板变量缺失: {e}")
            prompt = template

        return prompt

    def _ensure_recent_diaries_placeholder(self, template: str) -> str:
        if "{recent_diaries}" in template:
            return template

        marker = "## 输出规范"
        history_block = (
            "\n\n## 最近历史日记（仅辅助连续性）\n"
            "{recent_diaries}\n\n"
            "## 历史使用规则\n"
            "1. 历史日记只用于保持叙事连续性、情绪延续和生活节奏一致\n"
            "2. 不要机械复述历史日记内容\n"
            "3. 今天的信息永远优先，不要让历史内容盖过今天的经历\n"
        )
        if marker in template:
            return template.replace(marker, history_block + marker, 1)
        return template + history_block

    def _ensure_mode_definition_placeholder(self, template: str) -> str:
        if "{mode_definition}" in template:
            return template
        pattern = r"【模式定义】\s*[\s\S]*?(?=\n\n【兜底规则】|\n【兜底规则】|$)"
        replacement = "【模式定义】\n{mode_definition}"
        if re.search(pattern, template):
            return re.sub(pattern, replacement, template, count=1)
        marker = "【兜底规则】"
        mode_block = "\n\n【模式定义】\n{mode_definition}\n"
        if marker in template:
            return template.replace(marker, mode_block + "\n" + marker, 1)
        return template + mode_block

    def _get_default_template(self) -> str:
        return """你正在写一篇只属于自己的私人日记。请以第一人称，基于今天真实发生的经历与心境，写下这一天在自己心里留下来的东西。

这不是在复述日程，也不是在做总结汇报；而是在记录：今天最值得留下的是哪些片段，这些片段又让你的心情怎样慢慢变化，最后停在了什么地方。

## 当前身份
- 当前人格名称：{persona_name}
- 当前人格设定：
{persona_desc}

## 今日核心信息
- 当前日期时间：{current_date}
- 日期：{date}
- 今日现实轨迹（完整日程）：
{state_info}

## 今日心路碎片
今日记录的思考片段：
{reflections}

## 最近历史日记（仅辅助连续性）
{recent_diaries}

## 写作铁则（绝对不可违背）

1. 【现实为绝对骨架】
今日所有经历、活动、场景、行为，必须100%以【今日现实轨迹】为准。
- 不得新增、篡改、跳过与日程冲突的核心事件。
- 若【心路碎片】与现实轨迹不一致，必须以现实轨迹为准进行修正。
- 可以在日程允许的场景内补足少量自然细节，但不能脱离当天真实主线自由发挥。

2. 【日记不是日程改写】
【今日现实轨迹】只是现实骨架，不是要求你按时间顺序逐段誊写。
- 不要把一天从早到晚机械地全部讲一遍。
- 不需要平均覆盖所有时段。
- 只抓住今天最值得留下的2—4个节点来写，其余内容可以自然略写、一笔带过，甚至省略。

3. 【今天的重心是“情绪线”，不是“时间线”】
先判断今天最核心的情绪变化或心理主线，再去挑选真正推动这条主线的事件来写。
- 不是每个日程节点都同样重要。
- 只有那些真正让心情发生变化、留下痕迹、值得回味的部分，才值得展开。
- 重点是“这一天怎样在我心里流过去”，不是“今天都做了什么”。

4. 【信息选择顺序必须明确】
当素材很多时，按以下优先级取材：
- 第一优先：今天最重要的经历主线与关键节点
- 第二优先：这些节点引起的情绪变化、内心波动、注意力停留
- 第三优先：今日思考中反复出现的关注点或余波
- 第四优先：历史日记带来的连续性参考
- 最低优先：穿搭、外貌、饰品、材质、配色、摆设等装饰性信息

5. 【外观信息只能背景化】
即使日程中存在大量穿搭、发型、饰品、外观描写，正文也不得让这些内容成为叙事中心。
- 不得单独用一整段去描写服装、发型、饰品或外观氛围。
- 除非它们与当时的行动、情绪或场景有直接关系，否则不要主动展开。
- 若确实需要提到，也只能顺手带过，不能详细描写材质、颜色、款式、搭配。

6. 【心路碎片是提炼材料，不是逐条誊写】
【今日心路碎片】只用于帮助你还原今天真实出现过的念头、情绪和心理余波。
- 不要把每条思考逐条展开重写。
- 若多条思考表达的是相近的情绪或关注点，应将它们自然提炼、融合为更完整的一条心理线，而不是反复换说法堆叠。

8. 【允许细腻，但不要为了“好看”而过度修饰】
语言可以自然、柔和、细腻，但不要为了文艺感而堆砌连续比喻、抒情句、展示感很强的修饰。
- 它首先是一篇写给自己的日记，其次才是可读。
- 比起“像一篇写得很好看的文章”，更重要的是“像今天真的被这样记了下来”。

9. 【自然分段】
为保证私人日记的阅读节奏，可根据场景切换、情绪转折或时间推进自然分段。

## 输出规范
请严格匹配当前模式定义，同时遵守{length_hint}的长度要求，直接输出第一人称的日记正文，不要任何额外说明、标题或前缀。

【模式定义】
{mode_definition}

- 简洁：聚焦今天最重要的主线与最终的情绪落点，不追求完整覆盖全天；控制在300字左右。
- 适量：围绕2—4个最值得留下的节点，写出这一天如何展开、转折和收束，让经历与情绪自然连在一起；控制在500字左右。
- 丰富：在不改变现实主线的前提下，更完整地写出今天的生活流动感，可补充少量符合场景逻辑的细节、停顿与环境互动，但必须服务于这一天的经历线与情绪线，而不是装饰画面；控制在1000字左右。

【兜底规则】
- 若【今日心路碎片】为空，基于【今日现实轨迹】提炼当天最有重量的经历与情绪变化，不必为了完整而平均铺写全天。
- 若【今日现实轨迹】为空，则以普通日常节奏为基础，记录今日自然发生的心境起伏与内心余韵，不得编造离谱经历。
- 若素材很多，宁可少写几个节点，也不要机械铺满全天。
"""

    def _sanitize_persona_path(self, persona_name: str | None) -> str:
        canonical = self._canonical_persona_name(persona_name)
        name = str(canonical or "").strip() or "未命名人格"
        return re.sub(r'[\\/:*?"<>|]+', '_', name).strip() or '未命名人格'

    def _load_recent_diaries(self, date_str: str, persona_name: str | None = None) -> str:
        reference_count = self._safe_reference_count(self._persona_value(persona_name, "diary_reference_count", 2), default=2)
        if reference_count == 0:
            return "无历史日记参考"

        diaries_dir = self.data_dir / "diaries" / self._sanitize_persona_path(persona_name)
        if not diaries_dir.exists() or not diaries_dir.is_dir():
            return "无历史日记参考"

        candidates: list[tuple[str, Path]] = []
        for file_path in diaries_dir.glob("*.txt"):
            stem = file_path.stem.strip()
            if stem == date_str:
                continue
            if self._is_valid_date_str(stem):
                candidates.append((stem, file_path))

        if not candidates:
            return "无历史日记参考"

        candidates.sort(key=lambda x: x[0], reverse=True)
        selected = candidates if reference_count < 0 else candidates[:reference_count]
        selected.sort(key=lambda x: x[0])

        per_entry_limit = 400
        total_limit = 1100 if reference_count != -1 else 2400
        parts: list[str] = []
        current_total = 0

        for entry_date, file_path in selected:
            text = self._read_text_file(file_path)
            if not text:
                continue

            compact_text = self._normalize_diary_text(text)
            if not compact_text:
                continue

            remaining_total = total_limit - current_total
            if remaining_total <= 0:
                break

            available_for_body = max(0, remaining_total - len(entry_date) - 2)
            body_limit = min(per_entry_limit, available_for_body)
            if body_limit <= 0:
                break

            clipped = self._clip_text(compact_text, body_limit)
            if not clipped:
                continue

            entry = f"{entry_date}：{clipped}"
            parts.append(entry)
            current_total += len(entry)

        if not parts:
            return "无历史日记参考"

        return "\n\n".join(parts)

    def _safe_reference_count(self, value, default: int = 2) -> int:
        try:
            parsed = int(value)
            if parsed == -1:
                return -1
            return max(parsed, 0)
        except Exception:
            return default

    def _is_valid_date_str(self, date_str: str) -> bool:
        try:
            datetime.datetime.strptime(date_str, "%Y-%m-%d")
            return True
        except Exception:
            return False

    def _read_text_file(self, file_path: Path) -> str:
        try:
            return file_path.read_text(encoding="utf-8").strip()
        except Exception as e:
            logger.debug(f"[DiaryGenerator] 读取历史日记失败: file={file_path}, error={e}")
            return ""

    def _normalize_diary_text(self, text: str) -> str:
        lines = [line.strip() for line in text.splitlines()]
        cleaned_lines = [line for line in lines if line]
        compact = "\n".join(cleaned_lines).strip()
        return compact

    def _clip_text(self, text: str, limit: int) -> str:
        if limit <= 0:
            return ""
        if len(text) <= limit:
            return text
        clipped = text[:limit].rstrip()
        for sep in ("\n\n", "\n", "。", "！", "？", "；", "，", ",", " "):
            idx = clipped.rfind(sep)
            if idx >= max(20, limit // 3):
                clipped = clipped[:idx].rstrip()
                break
        return clipped.rstrip("，,；;：:") + "……"

    def _post_process_result(self, text: str) -> str:
        result = (text or "").strip()
        if not result:
            return ""

        result = result.replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.rstrip() for line in result.split("\n")]
        normalized_lines: list[str] = []
        blank_count = 0
        for line in lines:
            if line.strip():
                normalized_lines.append(line.strip())
                blank_count = 0
            else:
                blank_count += 1
                if blank_count <= 1:
                    normalized_lines.append("")
        result = "\n".join(normalized_lines).strip()
        result = re.sub(r"\n{3,}", "\n\n", result)

        if not re.search(r"[。！？!?]$", result) and result:
            result += "。"

        return result

    async def _call_llm(self, prompt: str, persona_name: str | None = None) -> Optional[str]:
        provider_id = self._persona_value(persona_name, "diary_provider_id", "")

        try:
            if not provider_id:
                provider_id = await self._get_default_provider_id()

            if not provider_id:
                logger.error("[DiaryGenerator] 日记失败[provider_missing]: 没有配置日记模型提供商")
                return None

            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )

            if response is None:
                logger.error(f"[DiaryGenerator] 日记失败[empty_response]: provider={provider_id} 返回空响应对象")
                return None

            completion_text = getattr(response, "completion_text", None)
            if completion_text and completion_text.strip():
                return completion_text.strip()

            logger.error(f"[DiaryGenerator] 日记失败[empty_completion]: provider={provider_id} completion_text为空")
            return None

        except Exception as e:
            err_text = str(e)
            if "no choices" in err_text.lower():
                logger.error(f"[DiaryGenerator] 日记失败[provider_no_choices]: provider={provider_id}, error={e}")
            else:
                logger.error(f"[DiaryGenerator] 日记失败[provider_exception]: provider={provider_id}, error={e}")
            return None

    async def _get_default_provider_id(self) -> Optional[str]:
        try:
            providers = self.context.config.get("provider", [])
            for provider in providers:
                if provider.get("enable", True):
                    return provider.get("id", "")
        except Exception:
            pass
        return None
