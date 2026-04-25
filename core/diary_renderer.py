"""
日记图片渲染模块
信纸典雅风 — 精致、有仪式感的设计
"""

import datetime
import io
import os
import re
from pathlib import Path
from typing import Optional

from astrbot.api import logger

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False


class DiaryRenderer:
    """日记图片渲染器 — 信纸典雅风"""

    FONT_DOWNLOAD_URLS = [
        "https://github.com/AkisAya/NotoSerifSC-Regular/raw/main/NotoSerifSC-Regular.ttf",
        "https://cdn.jsdelivr.net/gh/AkisAya/NotoSerifSC-Regular@main/NotoSerifSC-Regular.ttf",
    ]
    FONT_FILENAME = "NotoSerifSC-Regular.ttf"

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir) if isinstance(data_dir, str) else data_dir
        self.fonts_dir = self.data_dir / "fonts"
        self._title_font: Optional[ImageFont.FreeTypeFont] = None
        self._body_font: Optional[ImageFont.FreeTypeFont] = None
        self._date_font: Optional[ImageFont.FreeTypeFont] = None
        self._small_font: Optional[ImageFont.FreeTypeFont] = None
        self._initialized = False

    def _ensure_fonts(self) -> bool:
        if not HAS_PILLOW:
            logger.error("[DiaryRenderer] Pillow 未安装，无法渲染日记图片")
            return False

        if self._initialized:
            return self._title_font is not None

        font_path = self._find_or_download_font()
        if not font_path:
            logger.error("[DiaryRenderer] 无法获取字体文件，日记图片渲染不可用")
            return False

        try:
            self._title_font = ImageFont.truetype(str(font_path), 32)
            self._body_font = ImageFont.truetype(str(font_path), 20)
            self._date_font = ImageFont.truetype(str(font_path), 16)
            self._small_font = ImageFont.truetype(str(font_path), 13)
            self._initialized = True
            return True
        except Exception as e:
            logger.error(f"[DiaryRenderer] 加载字体失败: {e}")
            return False

    def _find_or_download_font(self) -> Optional[Path]:
        cached = self.fonts_dir / self.FONT_FILENAME
        if cached.exists() and cached.stat().st_size > 100_000:
            return cached

        system_font = self._find_system_font()
        if system_font:
            return system_font

        return self._download_font()

    def _find_system_font(self) -> Optional[Path]:
        candidates = []

        if os.name == "nt":
            windir = os.environ.get("WINDIR", r"C:\Windows")
            font_dir = Path(windir) / "Fonts"
            candidates = [
                font_dir / "msyh.ttc",
                font_dir / "msyhbd.ttc",
                font_dir / "simhei.ttf",
                font_dir / "simsun.ttc",
                font_dir / "simfang.ttf",
            ]
        else:
            candidates = [
                Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                Path("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"),
                Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
                Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"),
                Path("/usr/share/fonts/noto-cjk/NotoSerifCJK-Regular.ttc"),
                Path("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"),
                Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
                Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
            ]

        for path in candidates:
            if path.exists() and path.stat().st_size > 100_000:
                logger.info(f"[DiaryRenderer] 使用系统字体: {path}")
                return path

        return None

    def _download_font(self) -> Optional[Path]:
        import urllib.request
        self.fonts_dir.mkdir(parents=True, exist_ok=True)
        target = self.fonts_dir / self.FONT_FILENAME

        for url in self.FONT_DOWNLOAD_URLS:
            tmp = target.with_suffix(".tmp")
            try:
                logger.info(f"[DiaryRenderer] 正在下载字体: {url}")
                urllib.request.urlretrieve(url, str(tmp))

                if tmp.exists() and tmp.stat().st_size > 100_000:
                    tmp.replace(target)
                    logger.info(f"[DiaryRenderer] 字体下载完成: {target}")
                    return target
                else:
                    tmp.unlink(missing_ok=True)
                    logger.warning(f"[DiaryRenderer] 下载的字体文件过小，尝试下一个源: {url}")
            except Exception as e:
                tmp.unlink(missing_ok=True)
                logger.warning(f"[DiaryRenderer] 从 {url} 下载字体失败: {e}")

        logger.error("[DiaryRenderer] 所有字体下载源均失败")
        return None

    def render(self, diary_text: str, date_str: str = "", persona_name: str = "") -> Optional[bytes]:
        if not self._ensure_fonts():
            return None

        try:
            diary_text = self._preprocess_text(diary_text)
            if not diary_text.strip():
                return None

            lines = self._wrap_text(diary_text)
            img_width = 600
            padding_x = 70
            padding_top = 80
            padding_bottom = 70
            line_height = 34
            title_area_height = 100

            max_lines = (self.MAX_IMAGE_HEIGHT - padding_top - title_area_height - padding_bottom) // line_height
            if len(lines) > max_lines:
                lines = lines[:max_lines]
                if lines and lines[-1] != "":
                    lines[-1] = lines[-1].rstrip() + "……"
                logger.warning(f"[DiaryRenderer] 日记内容过长，截断至 {max_lines} 行")

            content_height = len(lines) * line_height
            img_height = padding_top + title_area_height + content_height + padding_bottom
            img_height = max(img_height, 500)

            img = self._create_paper_background(img_width, img_height)
            draw = ImageDraw.Draw(img)

            y = padding_top
            y = self._draw_header(draw, date_str, persona_name, y, img_width, padding_x)
            y += 30
            y = self._draw_body(draw, lines, y, padding_x, line_height)
            self._draw_footer(draw, img_height, img_width, padding_x)

            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            return buf.getvalue()
        except Exception as e:
            logger.error(f"[DiaryRenderer] 渲染日记图片失败: {e}", exc_info=True)
            return None

    def _preprocess_text(self, text: str) -> str:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    MAX_IMAGE_HEIGHT = 4096

    def _create_paper_background(self, width: int, height: int) -> Image.Image:
        # 温暖的象牙白底色
        bg_color = (252, 250, 245)
        img = Image.new("RGB", (width, height), bg_color)
        draw = ImageDraw.Draw(img)

        # 顶部装饰边线（双线设计）
        top_line_y = 25
        draw.line([(40, top_line_y), (width - 40, top_line_y)], fill=(200, 185, 165), width=1)
        draw.line([(40, top_line_y + 4), (width - 40, top_line_y + 4)], fill=(220, 205, 185), width=1)

        # 底部装饰边线
        bottom_line_y = height - 30
        draw.line([(40, bottom_line_y), (width - 40, bottom_line_y)], fill=(200, 185, 165), width=1)
        draw.line([(40, bottom_line_y - 4), (width - 40, bottom_line_y - 4)], fill=(220, 205, 185), width=1)

        # 左侧装饰竖线（淡色）
        draw.line([(35, 45), (35, height - 45)], fill=(235, 225, 210), width=1)

        return img

    def _draw_header(self, draw: ImageDraw.Draw, date_str: str, persona_name: str, y: int, img_width: int, padding_x: int) -> int:
        # 日期 — 大字号，优雅
        title_text = self._format_date_title(date_str)
        bbox = draw.textbbox((0, 0), title_text, font=self._title_font)
        tw = bbox[2] - bbox[0]
        tx = (img_width - tw) // 2
        draw.text((tx, y), title_text, fill=(60, 50, 40), font=self._title_font)

        y += 48

        # 分隔装饰线（中间有菱形装饰）
        line_y = y
        line_color = (190, 175, 155)
        center_x = img_width // 2
        left_end = center_x - 60
        right_start = center_x + 60

        draw.line([(padding_x, line_y), (left_end, line_y)], fill=line_color, width=1)
        draw.line([(right_start, line_y), (img_width - padding_x, line_y)], fill=line_color, width=1)

        # 中间菱形装饰
        diamond_size = 4
        draw.polygon([
            (center_x, line_y - diamond_size),
            (center_x + diamond_size, line_y),
            (center_x, line_y + diamond_size),
            (center_x - diamond_size, line_y),
        ], fill=(180, 165, 145))

        y += 20

        if persona_name:
            sub_text = f"{persona_name}"
            bbox_s = draw.textbbox((0, 0), sub_text, font=self._date_font)
            sw = bbox_s[2] - bbox_s[0]
            sx = (img_width - sw) // 2
            draw.text((sx, y), sub_text, fill=(130, 120, 105), font=self._date_font)
            y += 28

        return y

    def _format_date_title(self, date_str: str) -> str:
        if not date_str:
            return datetime.datetime.now().strftime("%Y年%m月%d日")
        try:
            dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
            weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
            weekday = weekdays[dt.weekday()]
            return f"{dt.year}年{dt.month}月{dt.day}日  {weekday}"
        except Exception:
            return date_str

    def _draw_body(self, draw: ImageDraw.Draw, lines: list[str], y: int, padding_x: int, line_height: int) -> int:
        text_color = (65, 55, 45)
        for line in lines:
            if line == "":
                y += line_height // 2
                continue
            draw.text((padding_x, y), line, fill=text_color, font=self._body_font)
            y += line_height
        return y

    def _draw_footer(self, draw: ImageDraw.Draw, img_height: int, img_width: int, padding_x: int):
        # 左下角品牌标识
        footer_y = img_height - 45
        brand_text = "DayMind"
        draw.text((padding_x, footer_y), brand_text, fill=(190, 175, 155), font=self._small_font)

    def _wrap_text(self, text: str) -> list[str]:
        max_width = 460
        result: list[str] = []

        for paragraph in text.split("\n"):
            if not paragraph.strip():
                result.append("")
                continue

            current_line = ""
            for char in paragraph:
                test_line = current_line + char
                try:
                    bbox = self._body_font.getbbox(test_line)
                    w = bbox[2] - bbox[0]
                except Exception:
                    w = len(test_line) * 20

                if w > max_width:
                    if current_line:
                        result.append(current_line)
                    current_line = char
                else:
                    current_line = test_line

            if current_line:
                result.append(current_line)

        return result
