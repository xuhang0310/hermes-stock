"""技能：PPT 生成 —— 根据结构化内容生成专业演示文稿。

基于 python-pptx，支持多种配色方案和页面类型。
agent 负责收集需求、联网补充内容、构建页面结构；本模块负责渲染 .pptx。
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from pptx import Presentation
from pptx.dml.color import RGBColor as RgbColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt


# ---------------------------------------------------------------------------
# 配色方案
# ---------------------------------------------------------------------------

COLOR_PALETTES = {
    "midnight-executive": {
        "name": "午夜商务",
        "primary": (30, 39, 97),
        "secondary": (202, 220, 252),
        "accent": (255, 255, 255),
        "bg": (255, 255, 255),
        "text": (30, 39, 97),
        "text_light": (107, 114, 128),
    },
    "tech-dark": {
        "name": "科技深空",
        "primary": (13, 17, 23),
        "secondary": (22, 27, 34),
        "accent": (88, 166, 255),
        "bg": (255, 255, 255),
        "text": (240, 246, 252),
        "text_light": (139, 148, 158),
    },
    "coral-energy": {
        "name": "珊瑚活力",
        "primary": (249, 97, 103),
        "secondary": (249, 231, 149),
        "accent": (47, 60, 126),
        "bg": (255, 255, 255),
        "text": (31, 41, 55),
        "text_light": (107, 114, 128),
    },
    "forest-green": {
        "name": "森林绿",
        "primary": (34, 87, 62),
        "secondary": (180, 220, 190),
        "accent": (255, 255, 255),
        "bg": (255, 255, 255),
        "text": (34, 87, 62),
        "text_light": (107, 128, 114),
    },
    "royal-purple": {
        "name": "皇家紫",
        "primary": (75, 30, 110),
        "secondary": (200, 180, 230),
        "accent": (255, 255, 255),
        "bg": (255, 255, 255),
        "text": (55, 30, 80),
        "text_light": (120, 110, 140),
    },
}


# ---------------------------------------------------------------------------
# 幻灯片构建器
# ---------------------------------------------------------------------------

def _rgb(t: tuple[int, int, int]) -> RgbColor:
    return RgbColor(*t)


def _blank_slide(prs: Presentation) -> Any:
    return prs.slides.add_slide(prs.slide_layouts[6])


def _rect(slide, x, y, w, h, fill: tuple, line_none: bool = True):
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y, w, h)
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(fill)
    if line_none:
        shape.line.fill.background()
    return shape


def _oval(slide, x, y, w, h, fill: tuple):
    shape = slide.shapes.add_shape(MSO_SHAPE.OVAL, x, y, w, h)
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(fill)
    shape.line.fill.background()
    return shape


def _rounded_rect(slide, x, y, w, h, fill: tuple):
    shape = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h)
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(fill)
    shape.line.fill.background()
    return shape


def _text(slide, x, y, w, h, text: str, size: int, color: tuple,
          bold: bool = False, align=PP_ALIGN.LEFT) -> Any:
    box = slide.shapes.add_textbox(x, y, w, h)
    tf = box.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = text
    p.font.size = Pt(size)
    p.font.bold = bold
    p.font.color.rgb = _rgb(color)
    p.alignment = align
    return box


def _bullets(slide, x, y, w, h, items: list[str], size: int, color: tuple, space: int = 14):
    box = slide.shapes.add_textbox(x, y, w, h)
    tf = box.text_frame
    tf.word_wrap = True
    for i, item in enumerate(items):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = f"• {item}"
        p.font.size = Pt(size)
        p.font.color.rgb = _rgb(color)
        p.space_after = Pt(space)
    return box


def build_cover(prs: Presentation, colors: dict, title: str, subtitle: str | None = None):
    slide = _blank_slide(prs)
    _rect(slide, 0, 0, prs.slide_width, prs.slide_height, colors["primary"])
    _oval(slide, Inches(9.5), Inches(-1.5), Inches(5), Inches(5), colors["secondary"])
    _oval(slide, Inches(-1), Inches(5), Inches(4), Inches(4), colors["secondary"])
    _text(slide, Inches(0.8), Inches(2.8), Inches(11.5), Inches(1.5),
          title, 48, colors["accent"], bold=True, align=PP_ALIGN.CENTER)
    if subtitle:
        _text(slide, Inches(0.8), Inches(4.5), Inches(11.5), Inches(0.8),
              subtitle, 20, colors["secondary"], align=PP_ALIGN.CENTER)
    _rect(slide, Inches(5.5), Inches(5.8), Inches(2.5), Inches(0.05), colors["accent"])


def build_toc(prs: Presentation, colors: dict, items: list[str]):
    slide = _blank_slide(prs)
    _rect(slide, 0, 0, prs.slide_width, prs.slide_height, colors["bg"])
    _text(slide, Inches(0.8), Inches(0.5), Inches(11.5), Inches(1),
          "目录", 36, colors["primary"], bold=True)
    _rect(slide, Inches(0.8), Inches(1.6), Inches(0.1), Inches(5), colors["primary"])
    for i, item in enumerate(items[:8]):
        _text(slide, Inches(1.2), Inches(1.8 + i * 0.9), Inches(0.6), Inches(0.6),
              f"0{i + 1}", 24, colors["primary"], bold=True)
        _text(slide, Inches(2.0), Inches(1.8 + i * 0.9), Inches(9), Inches(0.6),
              item, 18, colors["text"])
        if i < len(items) - 1:
            _rect(slide, Inches(2.0), Inches(2.5 + i * 0.9), Inches(9), Inches(0.01), (229, 229, 229))


def build_content(prs: Presentation, colors: dict, title: str, bullets_items: list[str]):
    slide = _blank_slide(prs)
    _rect(slide, 0, 0, prs.slide_width, prs.slide_height, colors["bg"])
    _rect(slide, 0, 0, prs.slide_width, Inches(0.15), colors["primary"])
    _text(slide, Inches(0.8), Inches(0.5), Inches(11.5), Inches(1),
          title, 32, colors["primary"], bold=True)
    _bullets(slide, Inches(0.8), Inches(1.8), Inches(11.5), Inches(5),
             bullets_items, 18, colors["text"])


def build_big_numbers(prs: Presentation, colors: dict, title: str, numbers: list[tuple[str, str]]):
    """numbers: [(value_str, label), ...] 最多 4 组。"""
    slide = _blank_slide(prs)
    _rect(slide, 0, 0, prs.slide_width, prs.slide_height, colors["bg"])
    _rect(slide, 0, 0, prs.slide_width, Inches(0.15), colors["primary"])
    _text(slide, Inches(0.8), Inches(0.5), Inches(11.5), Inches(1),
          title, 32, colors["primary"], bold=True)

    n = min(len(numbers), 4)
    card_w = Inches(2.8)
    gap = Inches(0.4)
    total_w = n * 2.8 + (n - 1) * 0.4
    start_x = (13.333 - total_w) / 2
    start_y = Inches(2.2)
    card_h = Inches(2.8)

    for i, (value, label) in enumerate(numbers[:4]):
        x = Inches(start_x + i * (2.8 + 0.4))
        _rounded_rect(slide, x, start_y, card_w, card_h, colors["primary"])
        _text(slide, x, start_y + Inches(0.6), card_w, Inches(1.2),
              value, 42, colors["accent"], bold=True, align=PP_ALIGN.CENTER)
        _text(slide, x, start_y + Inches(1.9), card_w, Inches(0.6),
              label, 16, colors["secondary"], align=PP_ALIGN.CENTER)


def build_two_column(prs: Presentation, colors: dict, title: str,
                     left_title: str, left_items: list[str],
                     right_title: str, right_items: list[str]):
    slide = _blank_slide(prs)
    _rect(slide, 0, 0, prs.slide_width, prs.slide_height, colors["bg"])
    _rect(slide, 0, 0, prs.slide_width, Inches(0.15), colors["primary"])
    _text(slide, Inches(0.8), Inches(0.5), Inches(11.5), Inches(1),
          title, 32, colors["primary"], bold=True)

    col_w = Inches(5.5)
    _text(slide, Inches(0.8), Inches(1.7), col_w, Inches(0.6),
          left_title, 20, colors["primary"], bold=True)
    _bullets(slide, Inches(0.8), Inches(2.3), col_w, Inches(4.5), left_items, 16, colors["text"])

    _text(slide, Inches(6.8), Inches(1.7), col_w, Inches(0.6),
          right_title, 20, colors["primary"], bold=True)
    _bullets(slide, Inches(6.8), Inches(2.3), col_w, Inches(4.5), right_items, 16, colors["text"])


def build_summary(prs: Presentation, colors: dict, points: list[str]):
    slide = _blank_slide(prs)
    _rect(slide, 0, 0, prs.slide_width, prs.slide_height, colors["primary"])
    _text(slide, Inches(0.8), Inches(0.5), Inches(11.5), Inches(1),
          "总结", 36, colors["accent"], bold=True)

    for i, point in enumerate(points[:5]):
        y = Inches(1.8 + i * 1.1)
        _oval(slide, Inches(0.8), y + Inches(0.1), Inches(0.4), Inches(0.4), colors["accent"])
        _text(slide, Inches(0.8), y + Inches(0.05), Inches(0.4), Inches(0.4),
              str(i + 1), 14, colors["primary"], bold=True, align=PP_ALIGN.CENTER)
        _text(slide, Inches(1.4), y, Inches(10.5), Inches(0.8),
              point, 20, colors["accent"])


def build_end(prs: Presentation, colors: dict, text: str = "谢谢观看"):
    slide = _blank_slide(prs)
    _rect(slide, 0, 0, prs.slide_width, prs.slide_height, colors["primary"])
    _oval(slide, Inches(4.5), Inches(2), Inches(4.5), Inches(4.5), colors["secondary"])
    _text(slide, Inches(0), Inches(3.2), prs.slide_width, Inches(1.5),
          text, 48, colors["accent"], bold=True, align=PP_ALIGN.CENTER)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _sanitize_filename(name: str) -> str:
    safe = re.sub(r'[\\/:*?"<>|]', "_", name)
    return safe[:80] or "演示文稿"


def generate_ppt(
    title: str,
    pages: list[dict],
    subtitle: str | None = None,
    palette: str = "midnight-executive",
    output_filename: str | None = None,
) -> dict:
    """根据结构化页面内容生成 PPT 文件。

    Args:
        title: PPT 主标题（用于封面）
        pages: 页面列表，每个页面是 dict，字段：
            - type: cover | toc | content | big_numbers | two_column | summary | end
            - title: 页面标题（content/big_numbers/two_column 必填）
            - items: 正文列表（content/summary/toc 使用）
            - numbers: [(value, label), ...]（big_numbers 使用）
            - left_title / left_items / right_title / right_items（two_column 使用）
            - text: 结束页文字（end 可选，默认"谢谢观看"）
        subtitle: 封面副标题
        palette: 配色方案名
        output_filename: 输出文件名（不含扩展名），默认用 title
    """
    colors = COLOR_PALETTES.get(palette, COLOR_PALETTES["midnight-executive"])
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    for page in pages:
        ptype = (page.get("type") or "content").strip()
        try:
            if ptype == "cover":
                build_cover(prs, colors, page.get("title") or title,
                            page.get("subtitle") or subtitle)
            elif ptype == "toc":
                build_toc(prs, colors, page.get("items") or [])
            elif ptype == "content":
                build_content(prs, colors, page.get("title") or "内容",
                              page.get("items") or [])
            elif ptype == "big_numbers":
                build_big_numbers(prs, colors, page.get("title") or "数据",
                                  page.get("numbers") or [])
            elif ptype == "two_column":
                build_two_column(prs, colors, page.get("title") or "对比分析",
                                 page.get("left_title") or "左侧",
                                 page.get("left_items") or [],
                                 page.get("right_title") or "右侧",
                                 page.get("right_items") or [])
            elif ptype == "summary":
                build_summary(prs, colors, page.get("items") or [])
            elif ptype == "end":
                build_end(prs, colors, page.get("text") or "谢谢观看")
            else:
                # 未知类型降级为 content
                build_content(prs, colors, page.get("title") or ptype,
                              page.get("items") or ["（无内容）"])
        except Exception as exc:
            # 单页失败不中断整个生成
            build_content(prs, colors, f"页面生成异常：{ptype}",
                          [f"错误：{exc}"])

    fname = _sanitize_filename(output_filename or title)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{fname}_{ts}.pptx"

    # 保存到 agent workspace（与 ws_write_file 同一监狱目录）
    from backend.app.skills.agent_workspace import _WORKSPACE_ROOT
    _WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    out_dir = _WORKSPACE_ROOT / "ppt"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / filename
    prs.save(str(out_path))

    return {
        "status": "ok",
        "file_path": str(out_path),
        "filename": filename,
        "page_count": len(prs.slides),
        "palette": colors["name"],
        "title": title,
    }


# ---------------------------------------------------------------------------
# Skill 注册
# ---------------------------------------------------------------------------

SKILL = {
    "name": "ppt-generator",
    "display_name": "PPT 生成",
    "description": "根据主题和结构化内容生成专业演示文稿（.pptx），支持多种配色方案和页面类型。",
    "version": "1.0.0",
    "chat_modes": ["stock", "research"],
    "roles": [],
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "generate_ppt",
                "description": (
                    "生成 PPT 文件。接受结构化页面内容（标题、要点列表、数据卡片等），"
                    "输出 .pptx 文件到工作区。支持配色：midnight-executive（商务）、"
                    "tech-dark（科技）、coral-energy（活力）、forest-green（清新）、royal-purple（高端）。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "PPT 主标题"},
                        "subtitle": {"type": "string", "description": "封面副标题（可选）"},
                        "palette": {
                            "type": "string",
                            "enum": ["midnight-executive", "tech-dark", "coral-energy", "forest-green", "royal-purple"],
                            "description": "配色方案，默认 midnight-executive",
                        },
                        "pages": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "type": {
                                        "type": "string",
                                        "enum": ["cover", "toc", "content", "big_numbers", "two_column", "summary", "end"],
                                        "description": "页面类型",
                                    },
                                    "title": {"type": "string", "description": "页面标题"},
                                    "items": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "要点列表（content/summary/toc 用）",
                                    },
                                    "numbers": {
                                        "type": "array",
                                        "items": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                            "minItems": 2,
                                            "maxItems": 2,
                                        },
                                        "description": "数据卡片 [[value, label], ...]（big_numbers 用，最多 4 组）",
                                    },
                                    "left_title": {"type": "string", "description": "左栏标题（two_column 用）"},
                                    "left_items": {"type": "array", "items": {"type": "string"}},
                                    "right_title": {"type": "string", "description": "右栏标题（two_column 用）"},
                                    "right_items": {"type": "array", "items": {"type": "string"}},
                                    "text": {"type": "string", "description": "结束页文字（end 用，默认'谢谢观看'）"},
                                },
                                "required": ["type"],
                            },
                            "description": "页面列表，按顺序生成",
                        },
                        "output_filename": {"type": "string", "description": "输出文件名（不含扩展名）"},
                    },
                    "required": ["title", "pages"],
                },
            },
        },
    ],
    "executors": {
        "generate_ppt": lambda a: generate_ppt(
            title=str(a.get("title") or "演示文稿"),
            pages=list(a.get("pages") or []),
            subtitle=a.get("subtitle"),
            palette=str(a.get("palette") or "midnight-executive"),
            output_filename=a.get("output_filename"),
        ),
    },
    "prompt_instructions": [
        "PPT 生成规则：",
        "1. 收到 PPT 需求后，先确认主题、行业、页数、风格。用户未提供的可合理推断。",
        "2. 内容不足时，主动使用 search_comprehensive_intel 或 search_stock_news 检索补充行业数据、趋势、案例。",
        "3. 页面结构建议：封面(cover) → 目录(toc) → 内容页(content/big_numbers/two_column) → 总结(summary) → 结束(end)。",
        "4. 每页内容要点 3-5 条，每条至少 1 句完整说明；数据页用 big_numbers 展示关键数字。",
        "5. 调用 generate_ppt 时 pages 参数为 JSON 数组，每个元素包含 type 和对应字段。",
        "6. 生成成功后告知用户文件路径，提示可在工作区下载。",
    ],
}
