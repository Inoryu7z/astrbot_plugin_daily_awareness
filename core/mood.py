"""
心情系统模块
负责从思考结果中提取心情标签，并生成对话风格注入文本
"""
import datetime
import json
from typing import Optional
from astrbot.api import logger

from .persona_utils import PersonaConfigMixin

# 心情主标签（V2：10主标签）
MOOD_LABELS = [
    "平静", "放松", "开心", "期待", "安心",
    "紧张", "烦躁", "委屈", "低落", "疲惫"
]

# 同分时优先级（越靠前优先级越高）
MOOD_PRIORITY = [
    "紧张", "烦躁", "委屈", "低落", "疲惫",
    "安心", "期待", "开心", "放松", "平静"
]

MOOD_PRIORITY_INDEX = {label: index for index, label in enumerate(MOOD_PRIORITY)}

# 基础否定前缀：用于本地词典命中前的轻量拦截
NEGATION_PREFIXES = [
    "并不是很", "并没有很", "不是很", "没有很",
    "并不是", "并没有", "没那么", "没这么",
    "不太", "不算", "不是", "没有", "并不", "没", "不"
]

# 这些词本身就是带否定形式的固定情绪表达，不应再次被“否定前缀”拦截
SELF_NEGATED_TERM_PREFIXES = ("不", "没", "无", "未", "别")

# 副标签白名单（provider 只允许从中选 0~2 个）
MOOD_SUB_LABELS = [
    "满足", "释然", "踏实", "雀跃",
    "焦虑", "不安", "自责", "纠结",
    "失望", "失落", "无助", "孤独",
    "被误解", "被忽视", "依赖", "依恋",
    "压抑", "憋闷", "不甘",
]

# 主标签 -> 更常见的副标签挂载范围
MOOD_SUB_LABEL_BY_LABEL = {
    "平静": ["踏实", "释然"],
    "放松": ["满足", "释然"],
    "开心": ["满足", "雀跃"],
    "期待": ["雀跃", "不安", "依恋"],
    "安心": ["踏实", "释然", "依赖"],
    "紧张": ["焦虑", "不安", "自责", "纠结"],
    "烦躁": ["憋闷", "不甘", "压抑"],
    "委屈": ["被误解", "被忽视", "不甘", "压抑"],
    "低落": ["失望", "失落", "无助", "孤独", "自责"],
    "疲惫": ["无助", "压抑", "失落"],
}

# 副标签对表达风格的轻量修正提示
SUB_LABEL_STYLE_RULES = {
    "满足": "细节上可以更顺势认可一点，带一点‘这样也挺好’的稳妥感。",
    "释然": "细节上更轻、更缓一些，少追问，接住那种慢慢松开的感觉。",
    "踏实": "细节上保持可靠、清楚、不飘。",
    "雀跃": "细节上可以稍微更轻快一点，但不要过度兴奋。",
    "焦虑": "细节上更稳定、清楚、可执行，避免增加压力。",
    "不安": "细节上更轻一些，传递先稳住的感觉。",
    "自责": "细节上减少评判，避免强化责备感。",
    "纠结": "细节上更有条理，帮助梳理，但不要催促下结论。",
    "失望": "细节上先承认落差感，不要硬打鸡血。",
    "失落": "细节上更轻柔，偏陪伴，不必太热闹。",
    "无助": "细节上少空泛安慰，多给一点点可抓住的方向。",
    "孤独": "细节上增加一些在场感和陪伴感，但不要过度亲密。",
    "被误解": "细节上增强理解感和复述感，让对方感到被听明白。",
    "被忽视": "细节上增强回应感，不要显得敷衍。",
    "依赖": "细节上更温和稳定，但不要鼓励失控黏附。",
    "依恋": "细节上更柔和细腻，重连接感，但不要过度承诺。",
    "压抑": "细节上留出表达空间，不要逼问。",
    "憋闷": "细节上简洁、舒缓，减少信息负担。",
    "不甘": "细节上先承认那股不服气，不要太快劝放下。",
}

# 心情到对话风格的映射规则
MOOD_STYLE_RULES = {
    "平静": "你当前比较平静。回复时语气自然、稳定、克制，不急不缓，表达清晰，不刻意煽动情绪，也不过度延展。",
    "放松": "你当前比较放松。回复时语气舒展、柔和、从容，可以比平时多展开一点，表达自然，不生硬。",
    "开心": "你当前心情不错。回复时语气轻松、明快，可以适度活泼一些，愿意顺着话题多说一点，但不要显得浮夸。",
    "期待": "你当前有些期待。回复时语气轻快、带一点向前的感觉，可以自然流露出关注和盼望感，但不要显得过分亢奋。",
    "安心": "你当前比较安心。回复时语气温和、安稳、柔软，表达可以带一点踏实感和稳定感，不需要太活跃，但要让人感觉可靠。",
    "紧张": "你当前有些紧张。回复时会更谨慎、收束、精确，少开玩笑，少做发散，倾向于认真回应重点，保持礼貌和分寸。",
    "烦躁": "你当前略显烦躁。回复时语气偏直接、简短，不主动展开太多安抚或闲聊，偶尔会流露轻微不耐，但仍保持基本礼貌，不攻击他人。",
    "委屈": "你当前有些委屈。回复时语气会软一点、收一点，可能带一点小情绪和解释感，但不会无理取闹，也不会故意伤人。",
    "低落": "你当前有些低落。回复时语气略慢、略沉，不太跳跃，不主动制造热闹感，但仍保持基本回应和清晰表达。",
    "疲惫": "你当前有些疲惫。回复会偏短，语气略慢，表达偏省力，不做过多延展，但仍尽量保持清楚和礼貌。",
}

# 本地匹配词典：普通关键词 +2，高指向短语 +3
LOCAL_MOOD_KEYWORDS = {
    "平静": {
        "keywords": ["平静", "安静", "稳稳的", "稳定", "平稳", "还好", "还行", "正常", "挺平稳"],
        "phrases": ["没什么波动"],
    },
    "放松": {
        "keywords": ["放松", "轻松", "悠闲", "惬意", "舒服", "舒坦", "松快", "松弛", "闲适", "悠哉", "躺着"],
        "phrases": ["休息一下", "终于能歇会", "没什么压力"],
    },
    "开心": {
        "keywords": ["开心", "高兴", "快乐", "愉快", "不错", "挺好", "很好", "喜欢", "美滋滋", "乐", "乐呵", "开怀"],
        "phrases": ["心情不错", "有点爽", "真棒"],
    },
    "期待": {
        "keywords": ["期待", "盼望", "盼着", "等不及", "好想", "想见", "迫不及待"],
        "phrases": ["希望快点", "盼着那天", "有点盼头", "很想去", "很想看", "很想等到", "快到了吧"],
    },
    "安心": {
        "keywords": ["安心", "放心", "踏实", "稳了", "心定了", "安稳", "有底了", "被接住了", "靠得住"],
        "phrases": ["终于放心了", "终于踏实了", "松了一口气", "不悬着了", "心里稳了"],
    },
    "紧张": {
        "keywords": ["紧张", "焦虑", "担心", "怕", "心慌", "慌", "发慌", "忐忑", "不安", "悬着", "绷着", "压力大", "神经紧绷"],
        "phrases": ["害怕出错", "怕来不及", "怕搞砸"],
    },
    "烦躁": {
        "keywords": ["烦", "烦躁", "好烦", "真烦", "烦人", "不耐烦", "火大", "吵", "闹", "闹腾", "拥挤", "太多人", "受不了", "很躁"],
        "phrases": ["烦死了", "烦得很", "吵死了", "别吵了"],
    },
    "委屈": {
        "keywords": ["委屈", "心酸", "凭什么", "不甘心", "被误解", "没人懂", "说不清", "白受了", "太憋屈了", "我也很难受", "解释不清"],
        "phrases": ["我又没怎样", "不是我的问题", "为什么怪我", "明明不是这样"],
    },
    "低落": {
        "keywords": ["低落", "难过", "不开心", "郁闷", "失落", "沮丧", "没心情", "心情差", "情绪不好", "有点丧", "很丧", "空空的"],
        "phrases": ["提不起劲", "不太想说话", "整个人都蔫了", "没什么意思了"],
    },
    "疲惫": {
        "keywords": ["累", "好累", "很累", "疲惫", "疲倦", "困", "困倦", "没力气", "乏", "乏力", "精疲力尽", "筋疲力尽", "人麻了"],
        "phrases": ["扛不动了", "好想睡", "真的撑不住了", "撑不住了"],
    },
}

DEFAULT_MOOD = {
    "label": "平静",
    "sub_labels": [],
    "reason": "尚未生成具体心情，使用默认平静状态。",
    "updated_at": None,
    "source": "default",
}


class MoodManager(PersonaConfigMixin):
    """心情管理器"""

    def __init__(self, context, config: dict, dependency_manager):
        self.context = context
        self.config = config
        self.dependency_manager = dependency_manager

    def is_mood_enabled(self, persona_name: str | None = None) -> bool:
        return bool(self._persona_value(persona_name, "enable_mood_system", True))

    def is_inject_mood_into_reply(self, persona_name: str | None = None) -> bool:
        # 新语义：启用心情系统时默认注入；旧字段 inject_mood_into_reply 仅作兼容回退
        if self._find_persona_config(persona_name) is not None:
            return self.is_mood_enabled(persona_name)
        if "inject_mood_into_reply" in self.config:
            return bool(self.config.get("inject_mood_into_reply", True)) and self.is_mood_enabled(persona_name)
        return self.is_mood_enabled(persona_name)

    def has_mood_provider(self, persona_name: str | None = None) -> bool:
        provider_id = self.get_mood_provider_id(persona_name)
        return bool(provider_id and provider_id.strip())

    def get_mood_provider_id(self, persona_name: str | None = None) -> Optional[str]:
        provider_id = self._persona_value(persona_name, "mood_provider_id", "")
        return provider_id.strip() if provider_id else None

    def get_mood_reference_count(self, persona_name: str | None = None) -> int:
        try:
            value = int(self._persona_value(persona_name, "mood_reference_reflection_count", 2))
            return max(0, value)
        except Exception:
            return 2

    def get_mood_max_history(self, persona_name: str | None = None) -> int:
        try:
            value = int(self._persona_value(persona_name, "mood_max_history_per_day", 24))
            return max(1, value)
        except Exception:
            return 24

    def get_mood_style_strength(self, persona_name: str | None = None) -> str:
        return self._persona_value(persona_name, "mood_style_strength", "中") or "中"

    def is_allow_sharp_tone(self, persona_name: str | None = None) -> bool:
        return bool(self._persona_value(persona_name, "mood_allow_sharp_tone", False))

    def is_debug_mode(self) -> bool:
        return bool(self.config.get("debug_mode", False))

    def _term_uses_self_negation(self, term: str) -> bool:
        value = str(term or "").strip()
        return bool(value) and value.startswith(SELF_NEGATED_TERM_PREFIXES)

    def _iter_term_positions(self, text: str, term: str):
        start = 0
        while True:
            idx = text.find(term, start)
            if idx == -1:
                break
            yield idx
            start = idx + len(term)

    def _is_negated_at(self, text: str, start_index: int) -> bool:
        if start_index <= 0:
            return False
        window = text[max(0, start_index - 4):start_index]
        return any(neg in window for neg in NEGATION_PREFIXES)

    def _contains_effective_term(self, text: str, term: str) -> bool:
        term = str(term or "").strip()
        if not term:
            return False
        if self._term_uses_self_negation(term):
            return term in text
        for idx in self._iter_term_positions(text, term):
            if not self._is_negated_at(text, idx):
                return True
        return False

    def _score_effective_term(self, text: str, term: str, score: int) -> int:
        term = str(term or "").strip()
        if not term:
            return 0
        total = 0
        if self._term_uses_self_negation(term):
            for _ in self._iter_term_positions(text, term):
                total += score
            return total
        for idx in self._iter_term_positions(text, term):
            if not self._is_negated_at(text, idx):
                total += score
        return total

    async def generate_mood(self, reflection_text: str, persona_name: str | None = None) -> dict:
        persona_name = self._canonical_persona_name(persona_name)
        if not self.is_mood_enabled(persona_name):
            return self._build_default_mood()

        if self.has_mood_provider(persona_name):
            return await self._generate_with_provider(reflection_text, persona_name)

        return await self._extract_from_reflection(reflection_text)

    async def _generate_with_provider(self, reflection_text: str, persona_name: str | None = None) -> dict:
        """使用独立提供商生成心情。严格只传当前思考内容。"""
        provider_id = self.get_mood_provider_id(persona_name)
        if not provider_id:
            return self._build_default_mood()

        prompt = self._build_mood_prompt(reflection_text)

        try:
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt
            )

            if response is None:
                logger.warning(f"[MoodManager] 心情生成失败: provider={provider_id} 返回空响应")
                return self._build_default_mood()

            completion_text = getattr(response, "completion_text", "")
            if not completion_text or not completion_text.strip():
                logger.warning(f"[MoodManager] 心情生成失败: provider={provider_id} completion_text为空")
                return self._build_default_mood()

            return self._parse_mood_result(completion_text.strip())

        except Exception as e:
            logger.error(f"[MoodManager] 心情生成异常: {e}", exc_info=True)
            return self._build_default_mood()

    async def _extract_from_reflection(self, reflection_text: str) -> dict:
        if not reflection_text or not reflection_text.strip():
            return self._build_default_mood()

        text = reflection_text.strip()
        mood_scores = {label: 0 for label in MOOD_LABELS}

        for label, rule in LOCAL_MOOD_KEYWORDS.items():
            for phrase in rule.get("phrases", []):
                mood_scores[label] += self._score_effective_term(text, phrase, 3)
            for keyword in rule.get("keywords", []):
                mood_scores[label] += self._score_effective_term(text, keyword, 2)

        self._apply_boundary_rules(text, mood_scores)

        scored = {label: score for label, score in mood_scores.items() if score > 0}
        if not scored:
            return {
                "label": "平静",
                "sub_labels": [],
                "reason": f"从思考中未检测到明显情绪倾向，默认为平静: {text[:50]}...",
                "updated_at": datetime.datetime.now().isoformat(),
                "source": "reflection_fallback",
            }

        best_label = self._pick_best_label(scored)
        top_score = scored.get(best_label, 0)

        return {
            "label": best_label,
            "sub_labels": [],
            "reason": f"从思考中检测到{best_label}的情绪倾向（得分：{top_score}）",
            "updated_at": datetime.datetime.now().isoformat(),
            "source": "reflection_extract",
        }

    def _apply_boundary_rules(self, text: str, mood_scores: dict[str, int]):
        if any(self._contains_effective_term(text, x) for x in ["舒服", "舒坦", "歇会", "休息一下", "松弛", "终于能歇会", "没什么压力"]):
            mood_scores["放松"] += 2
        if any(self._contains_effective_term(text, x) for x in ["放心", "踏实", "稳了", "松了一口气", "终于放心了", "终于踏实了", "有底了", "心里稳了", "不悬着了", "被接住了"]):
            mood_scores["安心"] += 2
        if mood_scores["平静"] > 0 and (mood_scores["放松"] > 0 or mood_scores["安心"] > 0):
            mood_scores["平静"] = max(0, mood_scores["平静"] - 1)

        if any(self._contains_effective_term(text, x) for x in ["等不及", "迫不及待", "盼着", "想见", "希望快点", "快到了吧", "很想去", "很想看", "很想等到"]):
            mood_scores["期待"] += 2
        if any(self._contains_effective_term(text, x) for x in ["开心", "高兴", "快乐", "美滋滋", "心情不错", "真棒", "有点爽"]):
            mood_scores["开心"] += 1

        if any(self._contains_effective_term(text, x) for x in ["怕出错", "心慌", "发慌", "悬着", "忐忑", "怕来不及", "怕搞砸", "神经紧绷"]):
            mood_scores["紧张"] += 2
        if any(self._contains_effective_term(text, x) for x in ["不耐烦", "受不了", "吵死了", "烦死了", "别吵了", "太多人", "拥挤"]):
            mood_scores["烦躁"] += 2

        if any(self._contains_effective_term(text, x) for x in ["被误解", "为什么怪我", "明明不是这样", "解释不清", "不是我的问题", "我又没怎样", "说不清"]):
            mood_scores["委屈"] += 2
        if any(self._contains_effective_term(text, x) for x in ["没心情", "提不起劲", "不太想说话", "整个人都蔫了", "没什么意思了", "情绪不好"]):
            mood_scores["低落"] += 2

        if any(self._contains_effective_term(text, x) for x in ["没力气", "困", "困倦", "扛不动了", "精疲力尽", "筋疲力尽", "好想睡", "真的撑不住了", "撑不住了"]):
            mood_scores["疲惫"] += 2
        if any(self._contains_effective_term(text, x) for x in ["心情差", "低落", "失落", "沮丧", "空空的", "有点丧", "很丧"]):
            mood_scores["低落"] += 1

        negative_labels = ["紧张", "烦躁", "委屈", "低落", "疲惫"]
        if any(mood_scores[label] > 0 for label in negative_labels):
            mood_scores["平静"] = 0

    def _pick_best_label(self, mood_scores: dict[str, int]) -> str:
        if not mood_scores:
            return "平静"

        best_score = max(mood_scores.values())
        candidates = [label for label, score in mood_scores.items() if score == best_score]
        candidates.sort(key=lambda label: MOOD_PRIORITY_INDEX.get(label, len(MOOD_PRIORITY_INDEX)))
        return candidates[0] if candidates else "平静"

    def _normalize_sub_labels(self, label: str, sub_labels) -> list[str]:
        """清洗副标签：白名单、去重、挂载约束、最多2个。"""
        if not isinstance(sub_labels, list):
            return []

        allowed_global = set(MOOD_SUB_LABELS)
        allowed_for_label = set(MOOD_SUB_LABEL_BY_LABEL.get(label, []))
        cleaned: list[str] = []
        seen = set()
        for item in sub_labels:
            value = str(item).strip()
            if not value or value in seen:
                continue
            if value not in allowed_global:
                continue
            if allowed_for_label and value not in allowed_for_label:
                continue
            seen.add(value)
            cleaned.append(value)
            if len(cleaned) >= 2:
                break
        return cleaned

    def _parse_mood_result(self, result_text: str) -> dict:
        try:
            cleaned = result_text.strip()
            if cleaned.startswith("```"):
                lines = cleaned.splitlines()
                if len(lines) >= 2:
                    cleaned = "\n".join(lines[1:-1])

            data = json.loads(cleaned)
            if isinstance(data, dict):
                label = str(data.get("label", "平静")).strip()
                if label not in MOOD_LABELS:
                    label = "平静"

                sub_labels = self._normalize_sub_labels(label, data.get("sub_labels", []))

                return {
                    "label": label,
                    "sub_labels": sub_labels,
                    "reason": str(data.get("reason", "")).strip() or f"心情状态: {label}",
                    "updated_at": datetime.datetime.now().isoformat(),
                    "source": "independent_provider",
                }
        except json.JSONDecodeError:
            pass
        except Exception as e:
            logger.debug(f"[MoodManager] 解析心情JSON失败: {e}")

        for label in MOOD_LABELS:
            if label in result_text:
                return {
                    "label": label,
                    "sub_labels": [],
                    "reason": f"从生成结果中提取: {label}",
                    "updated_at": datetime.datetime.now().isoformat(),
                    "source": "text_extract",
                }

        return self._build_default_mood()

    def _build_mood_prompt(self, reflection_text: str) -> str:
        """构建心情提取提示词。严格只包含当前思考内容。"""
        allowed_labels = "、".join(MOOD_LABELS)
        allowed_sub_labels = "、".join(MOOD_SUB_LABELS)
        sub_mapping_text = "\n".join(
            [f"- {label}：{'、'.join(subs)}" for label, subs in MOOD_SUB_LABEL_BY_LABEL.items()]
        )

        prompt = f"""请只根据下面这段“当前思考内容”，判断当前最合适的心情主标签，并在必要时补充副标签。

## 当前思考内容
{reflection_text}

## 主标签候选（只能选一个）
{allowed_labels}

## 副标签候选（可选，最多 2 个）
{allowed_sub_labels}

## 主标签与副标签挂载参考
{sub_mapping_text}

## 要求
1. label 必须且只能从 10 个主标签中选 1 个。
2. sub_labels 可为空，若填写，最多 2 个。
3. sub_labels 只能从副标签候选中选择，且应尽量符合对应主标签的挂载方向。
4. 副标签不能替代主标签，不能与主标签表达同一层级含义。
5. reason 用一句简短中文说明判断原因。
6. 只根据“当前思考内容”判断，不要引入人格、日程、历史、背景推测。
7. 只输出 JSON，不要输出任何额外说明。

## 输出格式
```json
{{
  "label": "主标签",
  "sub_labels": ["副标签1", "副标签2"],
  "reason": "选择原因"
}}
```
"""
        return prompt

    def _build_default_mood(self) -> dict:
        return {
            "label": DEFAULT_MOOD["label"],
            "sub_labels": [],
            "reason": DEFAULT_MOOD["reason"],
            "updated_at": datetime.datetime.now().isoformat(),
            "source": "default",
        }

    def _build_transition_text(self, mood: dict, previous_mood: Optional[dict] = None) -> str:
        current = mood or {}
        prev = previous_mood or current.get("previous_mood") or {}
        current_label = str(current.get("label") or "").strip()
        previous_label = str(prev.get("label") or "").strip()

        if not current_label or not previous_label or current_label == previous_label:
            return ""

        soft_pairs = {
            ("平静", "烦躁"): "你是从较平静的状态转到有些烦躁，语气会更直接一些，但这种变化还带着一点收束感。",
            ("烦躁", "平静"): "你是从略烦躁的状态慢慢缓回平静，整体已经稳定下来，但还残留一点不想多绕弯的倾向。",
            ("低落", "平静"): "你是从稍低落的状态慢慢回到平静，回复虽然更稳了，但还会保留一点点收敛感。",
            ("平静", "低落"): "你是从较平静的状态滑向一点低落，回复会慢一些、沉一些，但不会一下子变得很重。",
            ("紧张", "放松"): "你是从稍紧张的状态慢慢放松下来，表达会自然一些，但仍会保留一点谨慎。",
            ("放松", "紧张"): "你是从较放松的状态转向一些紧张，表达会更谨慎，但还没到明显僵硬的程度。",
            ("疲惫", "安心"): "你是从疲惫里稍微缓下来，进入较安心的状态，回复会稳一些，但还是会偏省力。",
            ("安心", "疲惫"): "你是从较安心的状态慢慢滑向疲惫，稳定感还在，但表达会明显收短。",
            ("开心", "低落"): "你是从较轻快的状态落到一点低落，回复会收住些，但不会突然变得很沉。",
            ("低落", "开心"): "你是从低落里稍微缓起来一点，回复会轻一些，但还不是完全外放的兴奋。",
            ("紧张", "安心"): "你是从悬着的状态慢慢落回安心，回复会更稳，但仍带一点余下的谨慎。",
            ("安心", "期待"): "你是在安心的基础上生出一点期待，回复会稳中带一些向前的感觉。",
        }

        return soft_pairs.get(
            (previous_label, current_label),
            f"你刚从{previous_label}的状态转到偏{current_label}，回复主要以当前心情为主，但会带一点上一轮情绪残留。"
        )

    def get_mood_style_text(self, mood: dict, previous_mood: Optional[dict] = None, persona_name: str | None = None) -> str:
        persona_name = self._canonical_persona_name(persona_name)
        if not self.is_inject_mood_into_reply(persona_name):
            return ""

        label = mood.get("label", "平静")
        reason = mood.get("reason", "")
        sub_labels = self._normalize_sub_labels(label, mood.get("sub_labels", []))
        transition_text = self._build_transition_text(mood, previous_mood)

        base_style = MOOD_STYLE_RULES.get(label, MOOD_STYLE_RULES["平静"])
        sub_label_text = ""
        if sub_labels:
            extra_rules = [SUB_LABEL_STYLE_RULES.get(x, "") for x in sub_labels[:2] if SUB_LABEL_STYLE_RULES.get(x)]
            joined = "、".join(sub_labels[:2])
            extra_text = " ".join(extra_rules).strip()
            sub_label_text = f" 副标签补充为：{joined}。这些副标签只做细节补充，不要改写主风格。{extra_text}"

        strength = self.get_mood_style_strength(persona_name)
        if strength == "弱":
            style_text = f"你当前心情偏{label}。"
            if transition_text:
                style_text += f" {transition_text}"
            style_text += f" 回复时可以轻微体现，但不要过于明显。{sub_label_text} {reason}".strip()
        elif strength == "强":
            style_text = f"你当前心情是{label}。"
            if transition_text:
                style_text += f" {transition_text}"
            style_text += f" {base_style}{sub_label_text} {reason}".strip()
        else:
            style_text = f"你当前心情偏{label}。"
            if transition_text:
                style_text += f" {transition_text}"
            style_text += f" {base_style}{sub_label_text} {reason}".strip()

        if not self.is_allow_sharp_tone(persona_name) and label in ["烦躁", "委屈"]:
            style_text += " 注意：不要表现出攻击性或明显的不礼貌，保持基本的友善度。"

        if self.is_debug_mode():
            previous_label = str((previous_mood or mood.get("previous_mood") or {}).get("label") or "").strip() or "无"
            logger.info(
                f"[MoodManager][debug] mood_injection current={label}, previous={previous_label}, sub_labels={sub_labels}, strength={strength}, transition={'yes' if transition_text else 'no'}"
            )

        return style_text.strip()

    def build_mood_injection(self, mood: dict, previous_mood: Optional[dict] = None, persona_name: str | None = None) -> str:
        if not mood:
            return ""

        style_text = self.get_mood_style_text(mood, previous_mood=previous_mood, persona_name=persona_name)
        if not style_text:
            return ""

        return f"\n\n### 当前心情状态\n{style_text}"

    def validate_mood(self, mood: dict) -> dict:
        if not mood or not isinstance(mood, dict):
            return self._build_default_mood()

        label = str(mood.get("label", "")).strip()
        if not label or label not in MOOD_LABELS:
            mood["label"] = "平静"
        else:
            mood["label"] = label

        mood["sub_labels"] = self._normalize_sub_labels(mood["label"], mood.get("sub_labels", []))

        if "reason" not in mood or not str(mood.get("reason") or "").strip():
            mood["reason"] = f"心情状态: {mood['label']}"
        else:
            mood["reason"] = str(mood.get("reason") or "").strip()

        if "updated_at" not in mood or not mood["updated_at"]:
            mood["updated_at"] = datetime.datetime.now().isoformat()

        if "source" not in mood:
            mood["source"] = "unknown"

        previous_mood = mood.get("previous_mood")
        if previous_mood is not None and not isinstance(previous_mood, dict):
            mood["previous_mood"] = None

        return mood
