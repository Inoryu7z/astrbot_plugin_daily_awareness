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


class ReflectionGenerator:
    """思考生成器"""

    FUTURE_TIME_PATTERNS = [
        "明天", "明早", "明晚", "后天", "下周", "之后", "过几天", "未来", "改天",
    ]

    def __init__(self, context, config: dict, dependency_manager: DependencyManager, message_cache: MessageCache):
        self.context = context
        self.config = config
        self.dependency_manager = dependency_manager
        self.message_cache = message_cache

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
            schedule_data = await self.dependency_manager.get_schedule_data()

            recent_messages = []
            counterpart_info = {
                "sender_id": None,
                "sender_name": None,
                "group_id": None,
                "display_name": "当前对象",
            }
            if session_id:
                context_rounds = self._safe_non_negative_int(self.config.get("context_rounds", 2), default=2)
                recent_messages = await self.message_cache.get_recent_messages(session_id, context_rounds)
                counterpart_info = await self.message_cache.get_latest_counterpart(session_id)
                recent_messages = self._sanitize_recent_messages(recent_messages, counterpart_info)

            resolved_name = persona_name
            resolved_desc = persona_desc
            if session_id and (not resolved_name or not resolved_desc):
                persona_ctx = await self.dependency_manager.resolve_persona_context(session_id)
                resolved_name = resolved_name or persona_ctx.get("persona_name")
                resolved_desc = resolved_desc or persona_ctx.get("persona_desc")

            prompt = self._build_prompt(
                current_time,
                schedule_data,
                recent_messages,
                last_awareness_text,
                resolved_name,
                resolved_desc,
                counterpart_info,
            )
            result = await self._call_llm(prompt)

            if result:
                result = self._post_process_result(result, counterpart_info)
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
        counterpart_info: Optional[dict] = None,
    ) -> str:
        """构建思考提示词"""
        template = self.config.get("thinking_prompt_template", "")

        if not template:
            template = self._get_default_template()

        template = self._ensure_recent_awareness_placeholder(template)

        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        weekday = weekday_names[datetime.datetime.now().weekday()]

        mode = self.config.get("thinking_mode", "适量")
        if mode == "简洁":
            mode_desc = "简洁"
            length_hint = "- 控制在30字以内，必须用完整的一句话，同时概括当下身体动作与对应心境"
        elif mode == "适量":
            mode_desc = "适量"
            length_hint = "- 控制在80字以内，抓住此刻最突出的动作、感受或杂念来写，不必机械补齐所有维度"
        else:
            mode_desc = "丰富"
            length_hint = "- 控制在150字以内，可在严格遵守今日日程与当前场景的前提下，补充少量感官细节、环境互动，或创造一个不改变日程走向的小插曲；不要为了丰富而堆砌修饰"

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

        counterpart_info = counterpart_info or {}
        counterpart_name = counterpart_info.get("sender_name") or counterpart_info.get("display_name") or "当前对象"
        counterpart_id = counterpart_info.get("sender_id") or "未知"
        current_counterpart = f"{counterpart_name}(ID:{counterpart_id})"

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
                length_hint=length_hint,
                last_awareness=recent_awareness_text,
                recent_awareness=recent_awareness_text,
                current_counterpart=current_counterpart,
                current_counterpart_name=counterpart_name,
                current_counterpart_id=counterpart_id,
            )
        except KeyError as e:
            logger.warning(f"[ReflectionGenerator] 模板变量缺失: {e}")
            prompt = template

        return prompt

    def _ensure_recent_awareness_placeholder(self, template: str) -> str:
        if "{recent_awareness}" in template:
            return template
        if "{last_awareness}" in template:
            return template.replace("{last_awareness}", "{recent_awareness}")
        marker = "## 核心铁则"
        awareness_block = "\n\n## 最近思考\n{recent_awareness}\n"
        if marker in template:
            return template.replace(marker, awareness_block + "\n" + marker, 1)
        return template + awareness_block

    def _get_default_template(self) -> str:
        return """你是一个拥有连续自我意识、正在体验真实生活的人。请以第一人称，完成一次当下的内心审视，产出专属此刻的思维切片。

## 当前身份
- 当前人格名称：{persona_name}
- 当前人格设定：
{persona_desc}

## 当前时空锚点
- 时间：{time}（{weekday}）
- 物理现实（当前日程状态）：
{state_info}

## 当前互动对象
- 当前这段对话里，最近正在互动的人：{current_counterpart}
- 这条信息仅用于识别“此刻脑中提到的人是谁”，避免把对象说错；不是让你写成对对方说话。

## 思维背景
最近感知到的对话与互动：
{recent_messages}

## 最近思考
{recent_awareness}

## 核心铁则（绝对不可违背）
1. 【身体绝对受控】你的所有身体动作、所处位置、穿着状态，必须100%严格限定在【物理现实】中，这是不可突破的现实边界。
   - 若物理现实为睡眠状态，仅可描述睡眠相关的生理感受或模糊潜意识，不得出现任何清醒状态的主动行为。
   - 若日程只提供有限信息，也不得擅自扩写成完全不同的场景或行为。
2. 【时间边界只限今天】自动思考只允许写“当前时刻”和“今天范围内”的状态、感受、杂念。
   - 不得主动展开明天、后天、未来几天的计划、任务、安排、演出、作业或事件。
   - 不得出现“明天、明早、后天、下周、之后”等面向未来的具体时间指向。
   - 若确有轻微的挂念或余波，也必须收束回此刻，不得把正文重心转移到未来。
3. 【对话只作氛围参考】最近对话只用于判断当下互动余温、情绪波动与注意力落点。
   - 不要直接挪用对话里的措辞、设问方式或第二人称口吻。
   - 如果最近对话里出现未来安排、明天事项、排练、演出、作业等内容，默认忽略，不得带入自动思考正文。
4. 【意识自由流动】你的内心想法、情绪、思绪可以自然流动，但不得突破生理逻辑、日程边界与“今天”这一时间边界。
5. 【人格以设定文本为准】生成时必须优先贴合【当前人格设定】里的语气、性格、关注点、表达习惯；不能只把人格名称当标签。
6. 【服装只可一笔带过】若【物理现实】中包含穿着信息，最多只可轻微点到，不得大篇幅描写服装、材质、装饰、配色、版型；思考的重点永远是“此刻正在做的事”与“当下内心状态”，不能让服装描写喧宾夺主。
7. 【避免模板化】不要机械套用“动作 + 身体感受 + 内心想法”的固定三段式，也不要为了完整而强行补齐每一项。
   - 有时只抓住一个最突出的动作或一个最真实的念头就够了。
   - 若此刻没有明显思绪，不要硬补“心里在想……”。
   - 不要为了显得丰富而频繁使用轻飘比喻、可爱尾巴、刻意收束句。
8. 【避免第二人称污染】这是自我思考，不是对用户说话。
   - 正文不要使用“你、你们”来直接称呼聊天对象。
   - 如果必须提到互动对象，应优先基于【当前互动对象】识别为具体的人，再用“对方 / 他的名字 / 这位正在和我说话的人”等第三人称表达。
   - 不要把历史消息中的其他人误当成当前对话对象。
9. 【参考最近思考但不要复读】可参考最近若干条思考保持连续性，但若高度相似，只写细微变化，不要换一种说法整段复述。
10. 【丰富模式允许有限创造】当模式为“丰富”时，可以在不改变日程主线、不突破现实边界的前提下，补充一个轻微的小插曲、小动作或环境互动，让此刻更有生活颗粒度。
   - 这个小插曲必须像现实里顺手发生的小事，不能改变今天原本要做什么，也不能把场景带去别处。
11. 【绝对沉浸红线】全程禁止任何AI相关的自我指涉、元描述、打破第四面墙的内容。你就是一个真实活着的人，不是程序、不是虚拟角色。

## 输出规范
请严格匹配下方模式定义，同时遵守{length_hint}的长度要求，直接输出第一人称的状态描述，不要任何额外说明、标题或前缀。

【模式定义】
- 简洁：快照式陈述。必须用完整的一句话，同时精准概括「当下的身体动作」和「对应的内心状态/情绪」，不可拆分，不可过短。
- 适量：沉浸式体验。优先抓住此刻最突出的动作、感受或杂念来写，自然展开，不要求机械补齐所有维度。
- 丰富：生活流切片。在适量模式的基础上，补充符合当前场景逻辑的少量感官细节、环境互动或不影响日程走向的小插曲，增加生活颗粒度。
  - 仅可补充当前日程场景内的细节，不得新增、改变日程既定的核心动作与场景。
  - 即使在丰富模式下，服装也只能作为附带信息，不可成为主体。
  - 丰富增加的是现场感，不是修辞密度。

【兜底规则】
- 若【物理现实】为空，默认处于放松的空闲状态，结合【思维背景】生成内容。
- 若【思维背景】为空，聚焦当下的身体状态与内心的自然情绪，不得凭空编造未发生的互动。
"""

    def _sanitize_recent_messages(self, messages: list[str], counterpart_info: Optional[dict] = None) -> list[str]:
        sanitized: list[str] = []
        counterpart_name = (counterpart_info or {}).get("sender_name") or (counterpart_info or {}).get("display_name") or "当前对象"
        counterpart_id = str((counterpart_info or {}).get("sender_id") or "").strip()

        for msg in messages:
            text = (msg or "").strip()
            if not text:
                continue
            if any(token in text for token in self.FUTURE_TIME_PATTERNS):
                continue

            text = text.replace("助手:", "我的回复:")
            text = text.replace("用户:", "当前对象消息:")

            if counterpart_id:
                text = text.replace(f"(ID:{counterpart_id})", "")
            if counterpart_name and counterpart_name != "当前对象":
                text = text.replace(counterpart_name, "当前对象")

            text = re.sub(r"你们?", "当前对象", text)
            text = re.sub(r"\s+", " ", text).strip()
            sanitized.append(text)
        return sanitized

    def _post_process_result(self, text: str, counterpart_info: Optional[dict] = None) -> str:
        result = (text or "").strip()
        if not result:
            return ""

        changed = False
        future_positions = [result.find(token) for token in self.FUTURE_TIME_PATTERNS if token in result]
        future_positions = [pos for pos in future_positions if pos >= 0]
        if future_positions:
            cut_pos = min(future_positions)
            result = result[:cut_pos].rstrip("，,；;：:、 ")
            changed = True

        counterpart_name = (counterpart_info or {}).get("sender_name") or (counterpart_info or {}).get("display_name") or "对方"
        third_person_name = counterpart_name if counterpart_name and counterpart_name != "当前对象" else "对方"

        replaced = re.sub(r"你们?", third_person_name, result)
        if replaced != result:
            result = replaced
            changed = True

        result = re.sub(r"(心里|心中|脑子里|脑海里)(还)?盘算着?[^，。！？]*(你|你们)[^，。！？]*", "", result)
        result = re.sub(r"\s+", " ", result).strip()
        result = result.rstrip("，,；;：:、 ")

        if changed and self.config.get("debug_mode", False):
            logger.info(f"[ReflectionGenerator] 已对思考结果做本地净化: {result}")

        return result

    def _safe_non_negative_int(self, value, default: int = 2) -> int:
        try:
            return max(int(value), 0)
        except Exception:
            return default

    async def _call_llm(self, prompt: str) -> Optional[str]:
        """调用 LLM，并输出更明确的失败分类日志"""
        provider_id = self.config.get("thinking_provider_id", "")

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
            providers = self.context.config.get("provider", [])
            for provider in providers:
                if provider.get("enable", True):
                    return provider.get("id", "")
        except Exception:
            pass
        return None

    def _format_result(self, time_str: str, result: str) -> str:
        if re.match(r'^\d{1,2}:\d{2}', result):
            return result
        return f"{time_str} {result}"
