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


class DiaryGenerator:
    """日记生成器"""

    SECOND_PERSON_PATTERNS = [
        r"不知道你在做什么[^。！？!?.]*",
        r"希望你也[^。！？!?.]*",
        r"你也睡个好觉[^。！？!?.]*",
        r"晚安[^。！？!?.]*",
        r"明天见[^。！？!?.]*",
        r"回头见[^。！？!?.]*",
        r"等你[^。！？!?.]*",
        r"给你[^。！？!?.]*",
        r"对你说[^。！？!?.]*",
    ]

    def __init__(self, context, config: dict, dependency_manager: DependencyManager):
        self.context = context
        self.config = config
        self.dependency_manager = dependency_manager
        self.data_dir = Path(getattr(context, "get_data_dir", lambda: "")() or "")
        if not str(self.data_dir):
            try:
                from astrbot.core.star.star_tools import StarTools
                self.data_dir = Path(StarTools.get_data_dir())
            except Exception:
                self.data_dir = Path(".")

    async def generate(
        self,
        date_str: str,
        reflections: list[str],
        session_id: str | None = None,
        persona_name: str | None = None,
        persona_desc: str | None = None,
    ) -> Optional[str]:
        """生成日记（即使没有思考记录也能基于日程生成）"""
        try:
            schedule_data = await self.dependency_manager.get_schedule_data()
            resolved_name = persona_name
            resolved_desc = persona_desc
            if session_id and (not resolved_name or not resolved_desc):
                persona_ctx = await self.dependency_manager.resolve_persona_context(session_id)
                resolved_name = resolved_name or persona_ctx.get("persona_name")
                resolved_desc = resolved_desc or persona_ctx.get("persona_desc")

            recent_diaries = self._load_recent_diaries(date_str)
            prompt = self._build_prompt(
                date_str,
                schedule_data,
                reflections,
                resolved_name,
                resolved_desc,
                recent_diaries,
            )
            result = await self._call_llm(prompt)
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
        template = self.config.get("diary_prompt_template", "")

        if not template:
            template = self._get_default_template()

        template = self._ensure_recent_diaries_placeholder(template)

        mode = self.config.get("diary_mode", "适量")
        if mode == "简洁":
            mode_desc = "简洁"
            length_hint = "- 简练记录今日核心经历与整体心境，有清晰的情绪落点，不堆砌细节；控制在300字左右"
        elif mode == "适量":
            mode_desc = "适量"
            length_hint = "- 完整还原今日的核心活动，同步记录关键节点的真实感受、思考与情绪变化，做到事与情结合；控制在500字左右"
        else:
            mode_desc = "丰富"
            length_hint = "- 以现实轨迹为核心，整合心路碎片，补充符合场景逻辑的感官细节、环境氛围与微小插曲，形成连贯流畅的生活记叙；控制在1000字左右"

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
                f"[DiaryGenerator] 历史日记参考: count={history_count}, length={len(recent_diaries_text)}, date={date_str}"
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

    def _get_default_template(self) -> str:
        return """你是一个细腻敏感、习惯记录生活与心境的人，正在写一篇只属于自己的私密日记。请以第一人称，基于今日的经历与心路历程完成写作，保持与你日常思考一致的口吻与性格。

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
1. 【现实为绝对骨架】今日所有经历、活动、行为，必须100%以【今日现实轨迹】为唯一基准。
   - 若【心路碎片】与现实轨迹出现冲突，必须以现实轨迹为准进行修正，绝对不得出现与日程冲突的内容。
   - 衣着仅在外出、特殊场景时简要提及，不得占用大量篇幅。
2. 【人格以设定文本为准】你的叙述习惯、情绪表达、关注重点，必须优先贴合【当前人格设定】本体，不能只把人格名称当标签。
3. 【情绪为贯穿主线】请提炼出今日完整的情绪起伏与变化，用情绪线串联起一天的经历与心路碎片，绝对禁止机械罗列时间点与日程事项。
   - 可呈现一天内不同时段的情绪变化，而非全程单一固定的情绪描述。
4. 【历史日记只作辅助】最近历史日记只用于保持叙事连续性、情绪延续和生活节奏一致。
   - 不得机械复述历史日记，不得让历史内容盖过今天的经历。
   - 今日日程与今日思考永远优先于历史日记。
5. 【时间表达要落具体】若内容涉及“今天、昨晚、凌晨、明早、傍晚、今晚”等相对时间，写作时要结合【当前日期时间】和【日期】转换成自然且明确的具体日期/时段表达，不要含混。
6. 【日期抬头必须明确】正文开头必须先写明记录日期，例如“2026年3月30日 星期一”。
7. 【日记不是写给别人看的】这是一篇私人日记，不是写给当前聊天对象的信，也不是聊天窗口里的回复。
   - 禁止直接对任何人说话，禁止使用“你、你们、晚安、明天见、希望你也……”这类面向读者的表达。
   - 若确实提到互动对象，只能以第三人称或中性称呼带过，不得形成对话口吻。
   - 结尾必须收束在“我此刻的状态 / 今日总结 / 心情落点”，不能收束成对外问候。
8. 【绝对沉浸红线】全程禁止任何AI相关的自我指涉、元描述。这是你的私人日记，不是生成任务、不是程序输出。

## 输出规范
请严格匹配下方模式定义，同时遵守{length_hint}的长度要求，直接输出第一人称的日记正文，不要任何额外说明、标题或前缀。

【模式定义】
- 简洁：简练记录今日核心经历与整体心境，有清晰的情绪落点，不堆砌细节。
- 适量：完整还原今日的核心活动，同步记录每个关键节点当下的真实感受、思考与情绪变化，做到事与情结合。
- 丰富：沉浸式还原今日的生活现场。以现实轨迹为核心，整合心路碎片，补充符合场景逻辑的感官细节、环境氛围与微小插曲，形成一篇有画面感、连贯流畅的生活记叙文。
  - 仅可补充日程既定场景内的细节，不得新增、改变日程核心的活动与时间线。

【兜底规则】
- 若【心路碎片】为空，基于【今日现实轨迹】，合理还原每个场景下的真实心境与感受，不得凭空编造日程外的经历。
- 若【今日现实轨迹】为空，以日常的生活节奏为基础，记录今日的闲散状态与内心感悟，不得编造离谱的特殊经历。
"""

    def _load_recent_diaries(self, date_str: str) -> str:
        reference_count = self._safe_reference_count(self.config.get("diary_reference_count", 2), default=2)
        if reference_count == 0:
            return "无历史日记参考"

        diaries_dir = self.data_dir / "diaries"
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

        original = result
        for pattern in self.SECOND_PERSON_PATTERNS:
            result = re.sub(pattern, "", result, flags=re.IGNORECASE)

        result = re.sub(r"你们?", "对方", result)
        result = re.sub(r"(晚安|明天见|回见|回聊吧|早点睡)[。！？!?.]*$", "", result)
        result = re.sub(r"\s+", " ", result).strip()
        result = result.rstrip("，,；;：:、 ")

        if not re.search(r"[。！？!?]$", result) and result:
            result += "。"

        if self.config.get("debug_mode", False) and result != original:
            logger.info(f"[DiaryGenerator] 已对日记结果做第二人称净化: {result}")

        return result

    async def _call_llm(self, prompt: str) -> Optional[str]:
        provider_id = self.config.get("diary_provider_id", "")

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
