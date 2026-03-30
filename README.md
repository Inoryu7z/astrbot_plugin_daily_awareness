[![DayMind Counter](https://count.getloli.com/get/@Inoryu7z.daymind?theme=miku)](https://github.com/Inoryu7z/astrbot_plugin_daymind)
# 🌙 DayMind · 心智手记

让 Bot 不只是会回答问题，也能带着一点真实的生活感，慢慢过完今天。

**DayMind** 是一个轻量的心智链路插件。它不会试图把 Bot 变成一个庞杂、沉重、无所不能的系统，而是专注于一件更温柔的事：

- 在一天之中持续感知自己此刻的状态
- 把零散的念头积累成今天的心路轨迹
- 在夜里写下一篇属于这一天的日记

它想做的，不是“更聪明地答题”，而是让 Bot 更像一个一直活在今天的人。

---

## ✨ 它能做什么

### 🧠 自动思考
DayMind 会按设定间隔生成当下思考，并把“本日状态”注入后续对话。

思考会综合参考：

- 当前时间
- 日程 / 穿着 / 生活状态
- 最近对话
- 当前人格设定
- 最近几条思考

这样回复会更像“今天此刻的它”，而不是每次都像刚开机一样没有状态。

### 📓 自动日记
DayMind 支持在指定时间自动生成今日日记。

日记会综合参考：

- 今日日程
- 当日思考流
- 当前人格设定
- 最近历史日记

写出来的内容更像一天结束时留下的记录，而不是机械地把信息拼在一起。

### 🧷 内容管理
DayMind 已支持基础内容管理能力：

- 日记星标
- 思考流星标（按日聚合）
- 日记备注
- 思考流备注（按日聚合）
- 保留策略控制
- 清除今日思考流

其中被星标的内容不会参与自动轮换删除。

---

## 🌼 适合什么样的使用场景

如果你希望 Bot：

- 回答时带着“今天”的状态
- 不只是会聊天，而是有一点连续的生活感
- 会在一天结束时留下些什么
- 能把重要日记和关键思考长期保留下来

那 DayMind 会很适合你。

---

## 🧩 推荐搭配插件

DayMind 可以独立运行，但若想获得更完整体验，推荐搭配：

| 插件 | 作用 |
|------|------|
| `astrbot_plugin_life_scheduler` | 提供天气、日程、穿着等现实轨迹，让思考与日记更贴近生活 |
| `astrbot_plugin_livingmemory` | 让日记进入长期记忆系统，支持后续召回与追踪 |

### 🗂️ LivingMemory 联动
如果启用了日记写入 LivingMemory：

- 生成的日记可进入长期记忆
- 当重复生成今日日记时，旧记录不会被物理删除
- 旧 diary memory 会被标记为 `deleted`，保留追踪痕迹

这样会更稳，也更方便后续管理与排查。

---

## 🎮 可用指令

| 指令 | 权限 | 说明 |
|------|------|------|
| `/daymind_status` | 所有人 | 查看当前状态、今日思考次数、WebUI 地址等信息 |
| `/手动思考` | 所有人 | 立即手动触发一次思考 |
| `/生成日记` | 管理员 | 立即手动生成今日日记 |
| `/清除今日思考` | 管理员 | 清空今日思考流、本地当天思考文件与当前状态 |

---

## ⚙️ 主要配置项

### 基础开关

- `enable_auto_reflection`：是否启用自动思考
- `enable_auto_diary`：是否启用自动日记
- `enable_webui`：是否启用 DayMind 自带 WebUI
- `debug_mode`：是否启用调试日志

### 思考相关

- `thinking_interval_minutes`：自动思考间隔
- `reflection_reference_count`：生成新思考时参考最近几条思考
- `context_rounds`：思考时参考最近多少轮对话
- `thinking_mode`：思考长度模式（简洁 / 适量 / 丰富）
- `thinking_provider_id`：思考使用的模型提供商
- `thinking_prompt_template`：思考提示词模板
- `reflection_dedupe_mode`：本地近似去重强度（不调用 LLM）

### 日记相关

- `diary_time`：自动日记生成时间
- `diary_mode`：日记长度模式（简洁 / 适量 / 丰富）
- `diary_reference_count`：参考历史日记篇数
- `diary_provider_id`：日记使用的模型提供商
- `diary_prompt_template`：日记提示词模板
- `store_diary_to_memory`：是否写入 LivingMemory
- `diary_push_targets`：日记主动推送目标列表
- `allow_overwrite_today_diary`：是否允许重复生成今日日记

### 静默时段

- `silent_hours_enabled`
- `silent_hours_start`
- `silent_hours_end`

---

## 📝 使用说明

1. 若未配置 `thinking_provider_id`，自动思考无法执行。
2. 若未配置 `diary_provider_id`，自动日记无法执行。
3. 历史日记参考当前仅读取本地 `diaries/` 目录，不从 memory 回捞。
4. 关闭自动思考 / 自动日记后，手动指令仍然可用。

---

## 📌 当前版本已具备

- 自动思考
- 自动日记
- 人格接入
- 最近对话 / 日程 / 思考共同影响生成
- 日记第二人称污染修正
- 日记 / 思考流星标
- 日记 / 思考流备注
- 思考流清空指令
- 重复生成今日日记时的 LivingMemory 标记删除逻辑
- DayMind WebUI 后端基础能力

---

## 🛠️ TODO

- 心情系统
- 允许在 WebUI 中管理日记，并在删除时同步将 LivingMemory 中对应日记标记为删除
- WebUI 加入可爱风主题
- 允许推送日记时渲染为精美图片，并兼容 Windows 与 Linux 平台
- WebUI 星系模式下继续优化星星表现：增加数量、亮度与动态效果

---

希望它能让你的 Bot，多一点体温，也多一点陪伴感。
