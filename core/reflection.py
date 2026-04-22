"""
思考生成模块
负责生成Bot的自我思考内容
"""

import re
import datetime
from typing import Optional
from astrbot.api import logger

from .dependency import DependencyManager
from .message_cache import MessageCache
from .persona_utils import PersonaConfigMixin


class ReflectionGenerator(PersonaConfigMixin):
    """思考生成器"""

    FUTURE_TIME_PATTERNS = [
        "明天",
        "明早",
        "明晚",
        "后天",
        "下周",
        "之后",
        "过几天",
        "未来",
        "改天",
    ]

    def __init__(self, context, config: dict, dependency_manager: DependencyManager, message_cache: MessageCache):
        self.context = context
        self.config = config
        self.dependency_manager = dependency_manager
        self.message_cache = message_cache

    def _get_thinking_template(self, persona_name: str | None = None) -> str:
        override = str(self._persona_value(persona_name, "thinking_prompt_template_override", "") or "").strip()
        if override:
            return override
        default_template = str(self.config.get("default_thinking_prompt_template", "") or "").strip()
        if default_template:
            return default_template
        return self._get_default_template()

    async def generate(
        self,
        current_time: str,
        session_id: Optional[str] = None,
        last_awareness_text: Optional[str] = None,
        persona_name: Optional[str] = None,
        persona_desc: Optional[str] = None,
    ) -> Optional[str]:
        """生成思考内容"""
        try:
            canonical_persona = self._canonical_persona_name(persona_name)
            auto_ensure_schedule = bool(self._persona_value(canonical_persona, "reflection_auto_ensure_today_schedule", True))
            today_str = datetime.datetime.now().strftime("%Y-%m-%d")
            if auto_ensure_schedule:
                schedule_result = await self.dependency_manager.ensure_today_schedule(
                    session_id=session_id,
                    persona_name=canonical_persona,
                    persona_desc=persona_desc,
                    target_date=today_str,
                    debug=bool(self.config.get("debug_mode", False)),
                )
                schedule_data = schedule_result.get("data") or {}
                if schedule_result.get("status") == "failed":
                    if self.config.get("debug_mode", False):
                        logger.info(
                            f"[ReflectionGenerator][debug] ensure_today_schedule failed for reflection: "
                            f"persona={canonical_persona}, session={session_id}, target_date={today_str}, reason={schedule_result.get('message', '')}"
                        )
                    schedule_data = {}
            else:
                schedule_data = await self.dependency_manager.get_schedule_data(
                    session_id=session_id,
                    persona_name=canonical_persona,
                    target_date=today_str,
                    debug=bool(self.config.get("debug_mode", False)),
                )

            recent_messages = []
            counterpart_info = {
                "sender_id": None,
                "sender_name": None,
                "group_id": None,
                "display_name": "当前对象",
            }
            if session_id:
                context_rounds = self._safe_non_negative_int(self._persona_value(canonical_persona, "context_rounds", 2), default=2)
                recent_messages = await self.message_cache.get_recent_messages(session_id, context_rounds)
                counterpart_info = await self.message_cache.get_latest_counterpart(session_id)
                recent_messages = self._sanitize_recent_messages(recent_messages, counterpart_info)

            resolved_name = canonical_persona
            resolved_desc = persona_desc
            if session_id and (not resolved_name or not resolved_desc):
                persona_ctx = await self.dependency_manager.resolve_persona_context(session_id)
                resolved_name = resolved_name or self._canonical_persona_name(persona_ctx.get("persona_name") or persona_ctx.get("persona_id"))
                resolved_desc = resolved_desc or persona_ctx.get("persona_desc")

            if self.config.get("debug_mode", False):
                logger.info(
                    f"[ReflectionGenerator][debug] generate params: session={session_id}, persona={resolved_name}, "
                    f"recent_messages={len(recent_messages)}, last_awareness_len={len(last_awareness_text or '')}, "
                    f"schedule_outfit={str(schedule_data.get('outfit', ''))[:120]}, schedule={str(schedule_data.get('schedule', ''))[:300]}, "
                    f"auto_ensure_schedule={auto_ensure_schedule}, target_date={today_str}"
                )

            prompt = self._build_prompt(
                current_time,
                schedule_data,
                recent_messages,
                last_awareness_text,
                resolved_name,
                resolved_desc,
            )
            result = await self._call_llm(prompt, resolved_name)
            if result:
                return self._format_result(current_time, result)
            return None
        except Exception as e:
            logger.error(f"[ReflectionGenerator] 生成思考失败: {e}", exc_info=True)
            return None

    def _build_prompt(
        self,
        current_time: str,
        schedule_data: dict,
        recent_messages: list[str],
        last_awareness_text: Optional[str] = None,
        persona_name: Optional[str] = None,
        persona_desc: Optional[str] = None,
    ) -> str:
        """构建思考提示词"""
        template = self._get_thinking_template(persona_name)
        template = self._ensure_recent_awareness_placeholder(template)
        template = self._ensure_mode_definition_placeholder(template)

        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        weekday = weekday_names[datetime.datetime.now().weekday()]

        mode = self._persona_value(persona_name, "thinking_mode", "适量")
        if mode == "简洁":
            mode_desc = "简洁"
            length_hint = "- 控制在30字以内，只写此刻最核心的一点状态或念头，不要求补全所有维度"
            mode_definition = "- 简洁：一瞬间的意识截面（30字内）。只写此刻最核心的一点状态或念头，不要求补全所有维度；允许只落一个动作、一个感受或一个注意力焦点，但必须自然完整，不能空泛。"
        elif mode == "适量":
            mode_desc = "适量"
            length_hint = "- 控制在80字以内，围绕当前主活动，写出此刻在做什么与心里最突出的落点，必要时可带一个轻微环境细节或互动余温"
            mode_definition = "- 适量：一小段完整的当下体验（80字内）。围绕当前主活动，写出“此刻在做什么”与“心里最突出的落点”，必要时可带一个轻微环境细节或互动余温。重点是有当下推进感，不要写成日程摘要。"
        else:
            mode_desc = "丰富"
            length_hint = "- 控制在150字以内，在不改变现实主线的前提下，可补充1—2个符合当前场景的小细节、小动作、环境互动或额外的小插曲，增强真实生活感"
            mode_definition = "- 丰富：更有生活颗粒度的现场切片（150字内）。在不改变现实主线的前提下，可补充1—2个符合当前场景的小细节、小动作、环境互动或额外的小插曲，增强真实生活感。"

        recent_messages_str = "\n".join(recent_messages) if recent_messages else "（暂无最近对话）"

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
        recent_awareness_text = last_awareness_text or "（暂无最近思考）"

        try:
            prompt = template.format(
                time=current_time,
                weekday=weekday,
                persona_name=persona_name_text,
                persona_desc=persona_desc_text,
                state_info=state_info.strip(),
                recent_messages=recent_messages_str,
                mode_desc=mode_desc,
                mode_definition=mode_definition,
                length_hint=length_hint,
                recent_awareness=recent_awareness_text,
            )
        except KeyError as e:
            logger.warning(f"[ReflectionGenerator] 模板变量缺失: {e}")
            prompt = template

        if self.config.get("debug_mode", False):
            logger.info(
                f"[ReflectionGenerator][debug] prompt state_info={state_info.strip()[:500]}, persona={persona_name_text}"
            )
        return prompt

    def _ensure_recent_awareness_placeholder(self, template: str) -> str:
        if "{recent_awareness}" in template:
            return template
        marker = "## 核心规则"
        awareness_block = "\n\n## 最近思考\n{recent_awareness}\n"
        if marker in template:
            return template.replace(marker, awareness_block + "\n" + marker, 1)
        return template + awareness_block

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
        return """请你以第一人称，写出“此刻脑海里最鲜活的一小段意识切片”。

这不是在复述日程，不是在总结今天，也不是在对别人说话；而是在记录“我现在这一刻，正处于怎样的状态、注意力落在哪里、心里掠过了什么”。

## 当前身份
- 当前人格名称：{persona_name}
- 当前人格设定：
{persona_desc}

## 当前时空锚点
- 时间：{time}（{weekday}）
- 当前现实状态：
{state_info}

## 当前互动背景
最近感知到的对话与互动：
{recent_messages}

## 最近思考
{recent_awareness}

## 核心规则（绝对不可违背）

1. 【现实边界绝对优先】
你此刻的身体状态、所在场景、正在做的事，必须100%以【当前现实状态】为准。
- 不得脱离今日日程擅自新增完全不同的地点、事件、行为。
- 若当前时段信息有限，也只能在该场景内做轻微自然的补足，不能跳场景、跳主线。
- 若现实状态处于睡眠、休息、发呆、独处等低活动状态，就写与之匹配的意识流，不能硬编高强度行动。

2. 【思考不是复述日程】
日程只是现实骨架，不是正文模板。
- 不要把日程原文换一种说法重写一遍。
- 你要写的是“这一刻真正浮上来的意识内容”，而不是把今天安排重新描述。

3. 【信息选择顺序必须明确】
当输入信息很多时，按以下优先级取材：
- 第一优先：这一刻正在发生的核心活动 / 处境
- 第二优先：此刻最突出的情绪、注意力落点、心理余波
- 第三优先：最近对话带来的轻微情绪残留或关注点
- 第四优先：最近思考提供的连续性线索
- 最低优先：穿搭、外貌、材质、配色、饰品等外观信息

4. 【服装只可一笔带过】
即使【当前现实状态】里存在大量穿搭描写，正文也不得展开复述服装细节。
- 除非服装与当前行动直接相关，否则不要主动提及。
- 若确实必须提到，最多一句概括带过，禁止详细描述材质、款式、颜色、搭配、饰品。

5. 【最近思考只用于保持连续性，禁止复读】
{recent_awareness} 只用于帮助你知道“今天已经想过什么”，不是让你把它们换说法重写。
- 如果最近思考与当前场景高度相似，必须写出“此刻的新变化”。
- 这个新变化可以很小，但必须真实存在，例如：动作推进了一点、情绪浓淡变了、注意力转移了、互动对象变化了、环境状态变化了。
- 不允许只换词不换内容。

6. 【最近对话只作气氛参考】
最近对话只用于判断此刻有没有互动余温、情绪波动、注意力牵引。
- 不要直接照搬对话措辞。
- 不要写成对话回复。
- 若最近对话中包含未来安排、明天事项等内容，默认降权处理，不得让正文重心滑向未来。

7. 【时间边界只限今天与此刻】
自动思考只允许落在“当前时刻”与“今天范围内”的体验。
- 不主动展开明天、后天、未来几天的计划或安排。
- 不得把正文重心写成未来打算。
- 如果出现轻微挂念，也要立刻收回到此刻。

8. 【不要强行凑完整结构】
每次思考不必机械包含“动作 + 身体感受 + 内心想法”三项。
- 有时只写一个最真实的念头就够了。
- 有时只写动作里的情绪就够了。
- 有时只写场景推进后留下的一点余韵就够了。
- 重点是自然、准确、有当下感，不是形式完整。

9. 【这是自我思考，不是对外说话】
- 正文不要直接使用“你、你们”去称呼聊天对象。
- 如果必须提到互动对象，只能用第三人称表达。
- 禁止写成打招呼、安慰、回应、汇报。

## 输出规范
请严格匹配当前模式定义，同时遵守{length_hint}的长度要求。
直接输出第一人称正文，不要任何标题、说明、前缀、括号注释。

【模式定义】
{mode_definition}

- 简洁：一瞬间的意识截面（30字内）。
  只写此刻最核心的一点状态或念头，不要求补全所有维度；允许只落一个动作、一个感受或一个注意力焦点，但必须自然完整，不能空泛。

- 适量：一小段完整的当下体验（80字内）。
  围绕当前主活动，写出“此刻在做什么”与“心里最突出的落点”，必要时可带一个轻微环境细节或互动余温。重点是有当下推进感，不要写成日程摘要。

- 丰富：更有生活颗粒度的现场切片（150字内）。
  在不改变现实主线的前提下，可补充1—2个符合当前场景的小细节、小动作、环境互动或额外的小插曲，增强真实生活感。

【兜底规则】
- 若【当前现实状态】为空，默认处于自然放松的普通日常状态，结合【当前互动背景】写此刻意识流。
- 若【当前互动背景】为空，就聚焦当前场景中的身体状态、注意力落点和自然情绪。
- 若【最近思考】与当前时段高度重合，优先写“这一刻和上一刻相比，哪里变了一点”。
"""

    def _normalize_recent_role_prefix(self, text: str) -> str:
        normalized = text
        normalized = re.sub(r"^\s*我的回复\s*[:：]\s*", "我的上一轮回复：", normalized)
        normalized = re.sub(r"^\s*助手\s*[:：]\s*", "我的上一轮回复：", normalized)
        normalized = re.sub(r"^\s*用户\s*[:：]\s*", "当前对话对象消息：", normalized)
        normalized = re.sub(r"^\s*当前对象消息\s*[:：]\s*", "当前对话对象消息：", normalized)
        return normalized

    def _sanitize_recent_messages(self, messages: list[str], counterpart_info: Optional[dict] = None) -> list[str]:
        sanitized: list[str] = []
        counterpart_id = str((counterpart_info or {}).get("sender_id") or "").strip()
        for msg in messages:
            text = (msg or "").strip()
            if not text:
                continue
            if any(token in text for token in self.FUTURE_TIME_PATTERNS):
                continue
            text = self._normalize_recent_role_prefix(text)
            if counterpart_id:
                text = text.replace(f"(ID:{counterpart_id})", "")
            text = re.sub(r"\(ID:[^)]+\)", "", text)
            text = re.sub(r"\s+", " ", text).strip()
            sanitized.append(text)
        return sanitized

    def _safe_non_negative_int(self, value, default: int = 2) -> int:
        try:
            return max(int(value), 0)
        except Exception:
            return default

    async def _call_llm(self, prompt: str, persona_name: str | None = None) -> Optional[str]:
        """调用 LLM，并输出更明确的失败分类日志"""
        provider_id = self._persona_value(persona_name, "thinking_provider_id", "")
        try:
            if not provider_id:
                provider_id = await self._get_default_provider_id()
            if not provider_id:
                logger.error("[ReflectionGenerator] 思考失败[provider_missing]: 没有配置思考模型提供商")
                return None
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt
            )
            if response is None:
                logger.error(f"[ReflectionGenerator] 思考失败[empty_response]: provider={provider_id} 返回空响应对象")
                return None
            completion_text = getattr(response, "completion_text", None)
            if completion_text and completion_text.strip():
                return completion_text.strip()
            logger.error(f"[ReflectionGenerator] 思考失败[empty_completion]: provider={provider_id} completion_text为空")
            return None
        except Exception as e:
            err_text = str(e)
            if "no choices" in err_text.lower():
                logger.error(f"[ReflectionGenerator] 思考失败[provider_no_choices]: provider={provider_id}, error={e}")
            else:
                logger.error(f"[ReflectionGenerator] 思考失败[provider_exception]: provider={provider_id}, error={e}")
            return None

    async def _get_default_provider_id(self) -> Optional[str]:
        try:
            if hasattr(self.context, "get_using_provider"):
                provider = self.context.get_using_provider()
                if provider:
                    meta = provider.meta()
                    if meta and getattr(meta, "id", None):
                        return str(meta.id).strip() or None
            if hasattr(self.context, "provider_manager"):
                pm = self.context.provider_manager
                if hasattr(pm, "get_using_provider"):
                    provider = pm.get_using_provider()
                    if provider:
                        meta = provider.meta()
                        if meta and getattr(meta, "id", None):
                            return str(meta.id).strip() or None
        except Exception:
            pass
        return None

    def _format_result(self, time_str: str, result: str) -> str:
        if re.match(r'^\d{1,2}:\d{2}', result):
            return result
        return f"{time_str} {result}"