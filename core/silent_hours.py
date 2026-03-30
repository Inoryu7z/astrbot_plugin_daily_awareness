"""
静默时段检查模块
负责判断当前是否处于静默时段（不进行思考的时间段）
"""

import datetime
from typing import Tuple


class SilentHoursChecker:
    """静默时段检查器"""
    
    def __init__(self, start_time: str = "00:00", end_time: str = "06:00", enabled: bool = True):
        """
        初始化静默时段检查器
        
        Args:
            start_time: 静默开始时间（HH:MM格式）
            end_time: 静默结束时间（HH:MM格式）
            enabled: 是否启用静默时段
        """
        self.enabled = enabled
        self.start_hour, self.start_minute = self._parse_time(start_time)
        self.end_hour, self.end_minute = self._parse_time(end_time)
    
    def _parse_time(self, time_str: str) -> Tuple[int, int]:
        """解析时间字符串"""
        try:
            parts = time_str.split(":")
            return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            return 0, 0
    
    def is_silent(self) -> bool:
        """
        检查当前是否处于静默时段
        
        Returns:
            bool: True 表示当前是静默时段，不应进行思考
        """
        if not self.enabled:
            return False
        
        now = datetime.datetime.now()
        current_minutes = now.hour * 60 + now.minute
        start_minutes = self.start_hour * 60 + self.start_minute
        end_minutes = self.end_hour * 60 + self.end_minute
        
        # 处理跨午夜的情况（如 22:00 - 06:00）
        if start_minutes > end_minutes:
            # 跨午夜：如 22:00 到次日 06:00
            return current_minutes >= start_minutes or current_minutes < end_minutes
        else:
            # 不跨午夜：如 00:00 到 06:00
            return start_minutes <= current_minutes < end_minutes
    
    def get_status(self) -> dict:
        """获取静默时段状态"""
        return {
            "enabled": self.enabled,
            "start": f"{self.start_hour:02d}:{self.start_minute:02d}",
            "end": f"{self.end_hour:02d}:{self.end_minute:02d}",
            "is_silent_now": self.is_silent()
        }
