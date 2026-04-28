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

    SCHEDULE_SLOT_PATTERN = re.compile(
        r"──\s*第\s*\d+\s*项\s*──\s*\n"
        r"🕐\s*(\d{1,2}:\d{2})\s*[-–—]\s*(\d{1,2}:\d{2})\s*\n"
        r"📌\s*(.+?)\s*\n"
        r"📄\s*([\s\S]*?)(?=\n\n──\s*第\s*\d+\s*项\s*──|\n👗|\Z)",
    )

    def __init__(self, context, config: dict, dependency_manager: DependencyManager, message_cache: MessageCache):
        self.context = context
        self.config = config
        self.dependency_manager = dependency_manager
        self.message_cache = message_cache

    @staticmethod
    def _parse_time_to_minutes(time_str: str) -> int:
        try:
            parts = time_str.strip().split(":")
            return int(parts[0]) * 60 + int(parts[1])
        except Exception:
            return -1

    def _extract_current_schedule_slot(self, schedule_text: str, current_time_str: str) -> dict:
        if not schedule_text or not schedule_text.strip():
            return {"found": False, "slots_text": "", "fallback": True}
        slots = []
        for m in self.SCHEDULE_SLOT_PATTERN.finditer(schedule_text):
            start_str = m.group(1).strip()
            end_str = m.group(2).strip()
            title = m.group(3).strip()
            detail = m.group(4).strip()
            start_min = self._parse_time_to_minutes(start_str)
            end_min = self._parse_time_to_minutes(end_str)
            if start_min < 0 or end_min < 0:
                continue
            slots.append({
                "start_str": start_str,
                "end_str": end_str,
                "start_min": start_min,
                "end_min": end_min,
                "title": title,
                "detail": detail,
                "raw": f"🕐 {start_str}-{end_str}\n📌 {title}\n📄 {detail}",
            })
        if not slots:
            return {"found": False, "slots_text": schedule_text, "fallback": True}
        current_min = self._parse_time_to_minutes(current_time_str)
        if current_min < 0:
            return {"found": False, "slots_text": schedule_text, "fallback": True}
        current_idx = -1
        for i, slot in enumerate(slots):
            if slot["start_min"] <= current_min < slot["end_min"]:
                current_idx = i
                break
        if current_idx < 0:
            min_dist = float("inf")
            for i, slot in enumerate(slots):
                dist = min(abs(current_min - slot["start_min"]), abs(current_min - slot["end_min"]))
                if dist < min_dist:
                    min_dist = dist
                    current_idx = i
        start = max(0, current_idx - 1)
        end = min(len(slots), current_idx + 2)
        selected = slots[start:end]
        parts = []
        for s in selected:
            marker = "【当前时段】" if s is slots[current_idx] else ""
            parts.append(f"{marker}🕐 {s['start_str']}-{s['end_str']}\n📌 {s['title']}\n📄 {s['detail']}")
        return {
            "found": True,
            "slots_text": "\n\n".join(parts),
            "fallback": False,
            "current_slot": slots[current_idx] if current_idx >= 0 else None,
        }

    def _format_timeline_slot(self, slot: dict) -> str:
        start = str(slot.get("time_start", ""))
        end = str(slot.get("time_end", ""))
        title = str(slot.get("title", ""))
        detail = str(slot.get("detail", ""))
        text = f"{start}-{end} {title}"
        if detail:
            text += f"\n{detail}"
        return text

    def _extract_sub_events_text(self, sub_events, current_timeline_idx: int, current_min: int) -> str:
        if not sub_events or not isinstance(sub_events, list):
            return ""
        target_block = None
        for block in sub_events:
            if not isinstance(block, dict):
                continue
            if block.get("source_index") == current_timeline_idx:
                target_block = block
                break
        if not target_block:
            return ""
        items = target_block.get("items", [])
        if not items:
            return ""
        parts = []
        for item in items:
            if not isinstance(item, dict):
                continue
            start = str(item.get("time_start", ""))
            end = str(item.get("time_end", ""))
            title = str(item.get("title", ""))
            detail = str(item.get("detail", ""))
            item_start_min = self._parse_time_to_minutes(start)
            item_end_min = self._parse_time_to_minutes(end)
            is_current = item_start_min >= 0 and item_end_min >= 0 and item_start_min <= current_min < item_end_min
            marker = " ←此刻" if is_current else ""
            line = f"  {start}-{end} {title}{marker}"
            if detail:
                line += f"\n    {detail}"
            parts.append(line)
        return "\n".join(parts)

    def _build_enhanced_slot_text(self, schedule_data: dict, current_time_str: str) -> str:
        timeline = schedule_data.get("timeline")
        if not timeline or not isinstance(timeline, list):
            return ""
        current_min = self._parse_time_to_minutes(current_time_str)
        if current_min < 0:
            return ""
        current_idx = -1
        for i, slot in enumerate(timeline):
            if not isinstance(slot, dict):
                continue
            start_min = self._parse_time_to_minutes(str(slot.get("time_start", "")))
            end_min = self._parse_time_to_minutes(str(slot.get("time_end", "")))
            if start_min < 0 or end_min < 0:
                continue
            if start_min <= current_min < end_min:
                current_idx = i
                break
        if current_idx < 0:
            min_dist = float("inf")
            for i, slot in enumerate(timeline):
                if not isinstance(slot, dict):
                    continue
                start_min = self._parse_time_to_minutes(str(slot.get("time_start", "")))
                end_min = self._parse_time_to_minutes(str(slot.get("time_end", "")))
                if start_min < 0 or end_min < 0:
                    continue
                dist = min(abs(current_min - start_min), abs(current_min - end_min))
                if dist < min_dist:
                    min_dist = dist
                    current_idx = i
        if current_idx < 0:
            return ""
        parts = []
        if current_idx > 0:
            prev = timeline[current_idx - 1]
            if isinstance(prev, dict):
                parts.append(f"## 前一时段\n{self._format_timeline_slot(prev)}")
            else:
                parts.append("## 前一时段\n（今日起始时段）")
        else:
            parts.append("## 前一时段\n（今日起始时段）")
        curr = timeline[current_idx]
        parts.append(f"## 当前时段\n{self._format_timeline_slot(curr)}")
        sub_events = schedule_data.get("sub_events")
        sub_event_text = self._extract_sub_events_text(sub_events, current_idx, current_min)
        if sub_event_text:
            parts.append(f"## 当前时段细分\n{sub_event_text}")
        if current_idx < len(timeline) - 1:
            next_slot = timeline[current_idx + 1]
            if isinstance(next_slot, dict):
                parts.append(f"## 后一时段\n{self._format_timeline_slot(next_slot)}")
            else:
                parts.append("## 后一时段\n（已是今日最后时段）")
        else:
            parts.append("## 后一时段\n（已是今日最后时段）")
        return "\n\n".join(parts)

    def _parse_delta_and_body(self, result: str) -> str:
        delta_match = re.match(r"^【变】(.+?)(?:\n|\r\n)([\s\S]*)", result)
        if delta_match:
            body = delta_match.group(2).strip()
            if body:
                return body
        return result

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
                body = self._parse_delta_and_body(result)
                return self._format_result(current_time, body)
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
        template = self._ensure_current_slot_placeholder(template)
        template = self._ensure_delta_placeholder(template)

        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        weekday = weekday_names[datetime.datetime.now().weekday()]

        mode = self._persona_value(persona_name, "thinking_mode", "适量")
        if mode == "简洁":
            mode_desc = "简洁"
            length_hint = "- 控制在30字以内，只写此刻最核心的一个念头或感受，不重复当前活动名称"
            mode_definition = "- 简洁：一瞬间的意识截面（30字内）。只写此刻最核心的一个念头或感受，不重复当前活动名称（当前活动已在上下文中给出），只写活动中的内心落点。"
        elif mode == "适量":
            mode_desc = "适量"
            length_hint = "- 控制在80字以内，围绕当前细分活动，写出此刻在这个活动里的具体经历，不重复活动名称"
            mode_definition = "- 适量：一小段完整的当下体验（80字内）。围绕当前细分活动，写出此刻在这个活动里的具体经历，而非复述活动本身；不重复活动名称，不写成日程摘要。"
        else:
            mode_desc = "丰富"
            length_hint = "- 控制在120-150字，必须同时具备活动中的具体经历、环境感知、意识的自然偏移三个维度"
            mode_definition = "- 丰富：更有生活颗粒度的现场切片（120-150字）。在当前细分活动的基础上，必须同时具备以下三个维度，缺一不可：\n  1. 活动中的具体经历（非活动本身的复述）\n  2. 环境感知\n  3. 意识的自然偏移——从当前活动中自然生发，而非凭空插入\n不重复活动名称，不写成日程摘要。"

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

        sub_events = schedule_data.get("sub_events")
        timeline = schedule_data.get("timeline")
        has_valid_sub_events = isinstance(sub_events, list) and len(sub_events) > 0
        has_valid_timeline = isinstance(timeline, list) and len(timeline) > 0
        used_enhanced_format = False

        if has_valid_sub_events and has_valid_timeline:
            current_slot_text = self._build_enhanced_slot_text(schedule_data, current_time)
            if current_slot_text:
                used_enhanced_format = True
            else:
                slot_result = self._extract_current_schedule_slot(schedule, current_time)
                current_slot_text = slot_result["slots_text"] if slot_result["found"] else ""
                if not current_slot_text and schedule:
                    current_slot_text = schedule
        else:
            slot_result = self._extract_current_schedule_slot(schedule, current_time)
            current_slot_text = slot_result["slots_text"] if slot_result["found"] else ""
            if not current_slot_text and schedule:
                current_slot_text = schedule

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
                current_slot=current_slot_text,
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
                f"[ReflectionGenerator][debug] prompt state_info={state_info.strip()[:500]}, "
                f"used_enhanced_format={used_enhanced_format}, persona={persona_name_text}"
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

    def _ensure_current_slot_placeholder(self, template: str) -> str:
        if "{current_slot}" in template:
            return template
        marker = "## 当前互动背景"
        slot_block = "\n## 当前时段日程（必须以此为思考主线）\n{current_slot}\n\n"
        if marker in template:
            return template.replace(marker, slot_block + marker, 1)
        marker2 = "## 核心规则"
        if marker2 in template:
            return template.replace(marker2, slot_block + "\n" + marker2, 1)
        return slot_block + template

    def _ensure_delta_placeholder(self, template: str) -> str:
        if "【变】" in template:
            return template
        marker = "【模式定义】"
        delta_hint = "\n输出格式（严格遵守）：\n第一行：【变】用一句话概括本次思考与上一条思考的差异点\n第二行起：第一人称正文\n\n"
        if marker in template:
            return template.replace(marker, delta_hint + marker, 1)
        return template + delta_hint

    def _get_default_template(self) -> str:
        return """请你以第一人称，写出“此刻脑海里最鲜活的一小段意识切片”。

这不是在复述日程，不是在总结今天，也不是在对别人说话；而是在记录“我现在这一刻，正处于怎样的状态、注意力落在哪里、心里掠过了什么”。

## 当前身份
- 当前人格名称：{persona_name}
- 当前人格设定：
{persona_desc}

## 当前时空锚点
- 时间：{time}（{weekday}）
- 当前状态：
{state_info}

## 当前时段日程（必须以此为思考主线）
{current_slot}

## 当前互动背景（仅作情绪参考，禁止作为思考主体）
⚠️ 以下对话内容仅用于感知此刻是否有互动余温或情绪波动，绝对不可让对话内容主导思考正文。思考必须围绕【当前时段日程】展开。
{recent_messages}

## 最近思考
{recent_awareness}

## 核心规则（绝对不可违背）

1. 【活动锚定——细分定锚，骨架定向】
当前细分活动是此刻的精确锚点，当前日程骨架是今日的方向感知。
思考不重复活动名称，而是写出活动中的具体经历。
- 思考必须从当前细分活动出发，不得矛盾
- 思考可体现角色对今日轨迹的模糊感知，但这是自然感受而非日程复述
- 即使最近对话内容很吸引人，也只能作为情绪的轻微点缀，不能让对话事件成为思考的主体

2. 【思考不是复述日程】
日程只是现实骨架，不是正文模板。
- 不要把日程原文换一种说法重写一遍。
- 你要写的是“这一刻真正浮上来的意识内容”，而不是把今天安排重新描述。
- 在日程提供的场景框架内，写出真实的内心体验和意识流动。

3. 【信息选择顺序必须明确】
- 第一优先：当前细分活动中的具体经历
- 第二优先：从当前经历中自然生发的偏移
- 第三优先：此刻最突出的情绪
- 第四优先：对今日轨迹的模糊感知
- 第五优先：最近对话带来的轻微情绪残留（最多一句话带过，不可展开）
- 第六优先：最近思考提供的连续性线索
- 最低优先：穿搭、外貌、材质、配色、饰品等外观信息

4. 【对话内容严格降权】
最近对话只能提供极其轻微的情绪底色（如“被夸了一句，心里有点甜”），绝对不能：
- 让对话中的具体事件、措辞、人物成为思考的核心内容
- 反复围绕同一句对话内容展开（如反复想“他说了xxx”）
- 把思考写成对对话的回应或延续
- 若对话内容与当前日程场景无关，直接忽略，不要强行关联

5. 【服装只可一笔带过】
即使穿着信息里存在大量描写，正文也不得展开复述服装细节。
- 除非服装与当前行动直接相关，否则不要主动提及。
- 若确实必须提到，最多一句概括带过，禁止详细描述材质、款式、颜色、搭配、饰品。

6. 【必须写出与上一条思考的差异】
每次思考必须体现推进感，禁止重复上一条思考的情绪焦点、动作焦点或场景焦点。
- 如果上一条聚焦于某种情绪，这一条就必须转向不同的情绪或动作。
- 如果上一条在做某个动作，这一条就必须写出动作推进或场景变化。
- 差异可以很小，但必须真实存在且可感知。

7. 【最近思考只用于保持连续性，禁止复读】
{recent_awareness} 只用于帮助你知道“今天已经想过什么”，不是让你把它们换说法重写。
- 不允许只换词不换内容。

8. 【时间边界只限今天与此刻】
自动思考只允许落在“当前时刻”与“今天范围内”的体验。
- 不主动展开明天、后天、未来几天的计划或安排。
- 不得把正文重心写成未来打算。
- 如果出现轻微挂念，也要立刻收回到此刻。

9. 【不要强行凑完整结构】
每次思考不必机械包含“动作 + 身体感受 + 内心想法”三项。
- 有时只写一个最真实的念头就够了。
- 有时只写动作里的情绪就够了。
- 有时只写场景推进后留下的一点余韵就够了。
- 重点是自然、准确、有当下感，不是形式完整。

10. 【这是自我思考，不是对外说话】
- 正文不要直接使用“你、你们”去称呼聊天对象。
- 如果必须提到互动对象，只能用第三人称表达。
- 禁止写成打招呼、安慰、回应、汇报。

## 输出规范
请严格匹配当前模式定义，同时遵守{length_hint}的长度要求。

输出格式（严格遵守）：
第一行：【变】用一句话概括本次思考与上一条思考的差异点
第二行起：第一人称正文

示例：
【变】从[上一状态]推进到[当前状态]
[此刻正在做的事，以及心里最突出的感受或注意力落点]

【模式定义】
{mode_definition}

【兜底规则】
- 若【当前时段日程】为空，默认处于自然放松的普通日常状态，结合互动背景写此刻意识流。
- 若互动背景也为空，就聚焦当前场景中的身体状态、注意力落点和自然情绪。
- 若最近思考与当前时段高度重合，必须写出“这一刻和上一刻相比，哪里变了一点”。
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