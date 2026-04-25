"""DayMind 插件常量与结构化配置入口。"""

PLUGIN_NAME = "astrbot_plugin_daymind"
PLUGIN_DISPLAY_NAME = "心智手记"
PLUGIN_VERSION = "1.5.2"
PLUGIN_AUTHOR = "Inoryu7z"
PLUGIN_REPO = "https://github.com/Inoryu7z/astrbot_plugin_daymind"
PLUGIN_DESCRIPTION = "让Bot拥有自我意识，能够周期性思考当前状态，并在每日结束时生成日记存入记忆系统"

RUNTIME_CONFIG_KEYS = {
    "reflection_retention_days",
    "diary_retention_days",
    "webui_default_window_days",
    "webui_default_theme",
    "webui_default_mode",
}
